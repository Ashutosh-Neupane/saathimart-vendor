"""
Stream Consumer - Vendor-side event consumption using Redis Streams.

Uses consumer groups for parallel processing and automatic failover.
If a worker crashes mid-processing, another worker can claim the message.

Architecture:
  Hub publishes → Redis Stream → Consumer Group → Multiple workers process in parallel
  
Features:
  - Consumer groups (parallel processing)
  - Automatic retry via XCLAIM (if worker crashes)
  - Monitoring via XPENDING/XINFO
  - No message loss (persisted in Redis)
"""
import frappe
import json
import time
from frappe import _
from frappe.utils import now_datetime
from typing import Dict, Any, List, Optional, Callable
import traceback


class StreamConsumer:
    """
    Consumes events from hub's Redis Stream using consumer groups.
    
    Each vendor has its own consumer group. Multiple workers can join
    the same group for parallel processing. If a worker crashes, its
    pending messages are claimed by other workers.
    
    Usage:
        consumer = StreamConsumer("VENDOR001", "worker-1")
        messages = consumer.consume(count=10)
        for msg_id, event in messages:
            # Process event
            process_event(event)
            # Acknowledge
            consumer.acknowledge([msg_id])
    """
    
    GROUP_NAME = "vendor-workers"  # Consumer group name
    STREAM_PREFIX = "hub:vendor"
    
    def __init__(self, vendor_id: str, consumer_id: Optional[str] = None):
        self.vendor_id = vendor_id
        self.stream_name = f"{self.STREAM_PREFIX}:{vendor_id}:events"
        self.consumer_id = consumer_id or f"worker-{frappe.generate_hash(length=8)}"
        self._redis = None
    
    @property
    def redis(self):
        """Lazy-load Redis client from Frappe's cache."""
        if self._redis is None:
            self._redis = frappe.cache()
        return self._redis
    
    def ensure_group(self):
        """
        Create consumer group if it doesn't exist.
        
        Safe to call multiple times (idempotent).
        Must be called before consuming messages.
        """
        try:
            self.redis.execute_command(
                "XGROUP", "CREATE", self.stream_name,
                self.GROUP_NAME, "$", "MKSTREAM"
            )
            frappe.logger().info(f"Created consumer group {self.GROUP_NAME} for stream {self.stream_name}")
        except Exception as e:
            # BUSYGROUP = group already exists (ok)
            if "BUSYGROUP" not in str(e):
                frappe.log_error(frappe.get_traceback(), f"Failed to create consumer group: {str(e)}")
                raise
    
    def consume(self, count: int = 10, block_ms: int = 5000) -> List[tuple]:
        """
        Read new messages from stream (blocking).
        
        Args:
            count: Maximum number of messages to fetch
            block_ms: Block for N milliseconds waiting for new messages (0 = don't block)
        
        Returns:
            List of (msg_id, event_dict) tuples
        
        Example:
            >>> messages = consumer.consume(count=10, block_ms=5000)
            >>> for msg_id, event in messages:
            ...     print(f"Processing {msg_id}: {event['event_type']}")
        """
        try:
            # XREADGROUP reads from consumer group
            # ">" means read only NEW messages (not pending)
            messages = self.redis.execute_command(
                "XREADGROUP", "GROUP", self.GROUP_NAME, self.consumer_id,
                "COUNT", str(count),
                "BLOCK", str(block_ms),
                "STREAMS", self.stream_name, ">"
            )
            
            if not messages:
                return []
            
            # Parse response: [[stream_name, [(msg_id, [k, v, k, v, ...]), ...]]]
            events = []
            for stream_data in messages:
                stream_name, msg_list = stream_data
                for msg_id, fields in msg_list:
                    # Convert [k1, v1, k2, v2, ...] to {k1: v1, k2: v2, ...}
                    event_dict = {}
                    for i in range(0, len(fields), 2):
                        key = fields[i].decode() if isinstance(fields[i], bytes) else fields[i]
                        value = fields[i + 1].decode() if isinstance(fields[i + 1], bytes) else fields[i + 1]
                        event_dict[key] = value
                    
                    events.append((msg_id.decode() if isinstance(msg_id, bytes) else msg_id, event_dict))
            
            return events
            
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), f"Stream consume error: {str(e)}")
            return []
    
    def acknowledge(self, msg_ids: List[str]):
        """
        Mark messages as processed (remove from pending list).
        
        Must be called after successfully processing a message.
        If not called, the message stays in pending list and will
        be retried via XCLAIM.
        
        Args:
            msg_ids: List of message IDs to acknowledge
        """
        if not msg_ids:
            return
        
        try:
            self.redis.execute_command(
                "XACK", self.stream_name, self.GROUP_NAME, *msg_ids
            )
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), f"Failed to acknowledge messages: {str(e)}")
    
    def claim_pending(self, min_idle_ms: int = 60000, count: int = 10) -> List[tuple]:
        """
        Claim messages that have been idle too long (stuck workers).
        
        This implements automatic retry logic. If a worker crashes
        while processing a message, it stays in the pending list.
        After min_idle_ms, another worker can claim it.
        
        Args:
            min_idle_ms: Minimum idle time in milliseconds (default: 60 seconds)
            count: Maximum number of messages to claim
        
        Returns:
            List of (msg_id, event_dict) tuples
        
        Example:
            >>> # Claim messages idle > 60 seconds
            >>> stuck = consumer.claim_pending(min_idle_ms=60000)
            >>> for msg_id, event in stuck:
            ...     retry_processing(event)
            ...     consumer.acknowledge([msg_id])
        """
        try:
            # XPENDING returns: [[msg_id, consumer, idle_time, deliveries], ...]
            pending = self.redis.execute_command(
                "XPENDING", self.stream_name, self.GROUP_NAME,
                "-", "+", str(count)
            )
            
            if not pending:
                return []
            
            # Find messages idle > min_idle_ms
            idle_ids = []
            for msg in pending:
                msg_id, consumer, idle_time, deliveries = msg
                if isinstance(msg_id, bytes):
                    msg_id = msg_id.decode()
                if idle_time >= min_idle_ms:
                    idle_ids.append(msg_id)
            
            if not idle_ids:
                return []
            
            # XCLAIM transfers ownership to this consumer
            claimed = self.redis.execute_command(
                "XCLAIM", self.stream_name, self.GROUP_NAME, self.consumer_id,
                str(min_idle_ms), *idle_ids
            )
            
            if not claimed:
                return []
            
            # Parse claimed messages
            events = []
            for msg_id, fields in claimed:
                event_dict = {}
                for i in range(0, len(fields), 2):
                    key = fields[i].decode() if isinstance(fields[i], bytes) else fields[i]
                    value = fields[i + 1].decode() if isinstance(fields[i + 1], bytes) else fields[i + 1]
                    event_dict[key] = value
                
                events.append((msg_id.decode() if isinstance(msg_id, bytes) else msg_id, event_dict))
            
            return events
            
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), f"Failed to claim pending messages: {str(e)}")
            return []
    
    def get_pending_info(self) -> Dict[str, Any]:
        """
        Get information about pending messages.
        
        Returns:
            Dict with pending count, min/max IDs, consumer details
        
        Example:
            >>> info = consumer.get_pending_info()
            >>> print(f"Pending: {info['count']}")
        """
        try:
            pending = self.redis.execute_command(
                "XPENDING", self.stream_name, self.GROUP_NAME
            )
            
            if not pending:
                return {"count": 0}
            
            # XPENDING returns: [count, min_id, max_id, [consumer1, count1, ...]]
            count, min_id, max_id, consumers = pending
            
            return {
                "count": count,
                "min_id": min_id.decode() if isinstance(min_id, bytes) else min_id,
                "max_id": max_id.decode() if isinstance(max_id, bytes) else max_id,
                "consumers": [
                    {
                        "consumer": c.decode() if isinstance(c, bytes) else c,
                        "count": cnt
                    }
                    for c, cnt in (consumers or [])
                ]
            }
        except Exception:
            return {"count": 0}
    
    def get_stream_length(self) -> int:
        """Get total number of messages in stream."""
        try:
            info = self.redis.execute_command("XLEN", self.stream_name)
            return info or 0
        except Exception:
            return 0
