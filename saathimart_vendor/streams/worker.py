"""
Stream Worker - Background task for consuming events from hub.

This task is executed by Frappe's RQ workers via the scheduler.

Setup in hooks.py:
    scheduler_events = {
        "all": [  # Runs every 4 minutes
            "saathimart_vendor.streams.worker.consume_hub_events"
        ]
    }

Or use frappe.enqueue() for on-demand processing:
    frappe.enqueue(
        "saathimart_vendor.streams.worker.consume_hub_events",
        queue="short",
        timeout=300
    )
"""
import frappe
import json
import time
from frappe import _
from typing import Dict, Any
from saathimart_vendor.streams.consumer import StreamConsumer
from saathimart_vendor.streams.processor import EventProcessor


def consume_hub_events(batch_size: int = 10, block_ms: int = 5000) -> Dict[str, int]:
    """
    Background task to consume and process events from hub stream.
    
    This is the main entry point for vendor-side event processing.
    Called by Frappe's scheduler every 4 minutes (configurable).
    
    Args:
        batch_size: Maximum number of messages to fetch per call
        block_ms: Block for N milliseconds waiting for new messages
    
    Returns:
        Dict with processing statistics:
        {
            "processed": 10,
            "failed": 2,
            "retried": 3,
            "pending": 5
        }
    
    Example:
        >>> # Via scheduler
        >>> # In hooks.py:
        >>> scheduler_events = {
        ...     "all": ["saathimart_vendor.streams.worker.consume_hub_events"]
        ... }
        
        >>> # Or on-demand
        >>> frappe.enqueue(
        ...     "saathimart_vendor.streams.worker.consume_hub_events",
        ...     queue="short"
        ... )
    """
    # Get vendor config
    config = frappe.get_single("Vendor Config")
    
    if not config or not config.vendor_id:
        return {"error": "Vendor not configured"}
    
    vendor_id = config.vendor_id
    
    # Initialize consumer
    consumer = StreamConsumer(vendor_id)
    consumer.ensure_group()  # Create group if needed
    
    stats = {
        "processed": 0,
        "failed": 0,
        "retried": 0,
        "pending": 0,
    }
    
    try:
        # 1. Process NEW messages
        messages = consumer.consume(count=batch_size, block_ms=block_ms)
        
        for msg_id, event in messages:
            success = EventProcessor.process(event)
            
            if success:
                consumer.acknowledge([msg_id])
                stats["processed"] += 1
            else:
                # Will be retried via XCLAIM
                stats["failed"] += 1
                frappe.logger().warning(
                    f"Failed to process event {msg_id}: {event.get('event_type')}"
                )
        
        # 2. Process STUCK messages (retry logic)
        # Messages idle > 60 seconds are considered stuck
        stuck_messages = consumer.claim_pending(min_idle_ms=60000, count=5)
        
        for msg_id, event in stuck_messages:
            success = EventProcessor.process(event)
            
            if success:
                consumer.acknowledge([msg_id])
                stats["retried"] += 1
            else:
                # Still failing, will be retried again
                stats["failed"] += 1
                
                # Check if max retries exceeded (optional)
                # If so, move to dead letter queue
        
        # 3. Get pending count
        pending_info = consumer.get_pending_info()
        stats["pending"] = pending_info.get("count", 0)
        
        # Log stats
        if stats["processed"] > 0 or stats["failed"] > 0:
            frappe.logger().info(
                f"Stream consumer stats: {stats}"
            )
        
        return stats
        
    except Exception as e:
        frappe.log_error(
            frappe.get_traceback(),
            f"Stream consumer error for vendor {vendor_id}"
        )
        return {"error": str(e)}


def get_stream_stats() -> Dict[str, Any]:
    """
    Get statistics about the stream for monitoring.
    
    Returns:
        Dict with stream length, pending count, consumer info
    
    Example:
        >>> stats = get_stream_stats()
        >>> print(f"Pending: {stats['pending']}")
    """
    config = frappe.get_single("Vendor Config")
    
    if not config or not config.vendor_id:
        return {"error": "Vendor not configured"}
    
    consumer = StreamConsumer(config.vendor_id)
    
    return {
        "stream_length": consumer.get_stream_length(),
        "pending": consumer.get_pending_info(),
        "vendor_id": config.vendor_id,
    }


def purge_stuck_messages(max_idle_hours: int = 24) -> int:
    """
    Purge messages that have been stuck for too long.
    
    Use with caution - this will acknowledge and remove stuck messages.
    Should only be used for messages that are impossible to process.
    
    Args:
        max_idle_hours: Messages idle > this many hours will be purged
    
    Returns:
        Number of messages purged
    """
    config = frappe.get_single("Vendor Config")
    
    if not config or not config.vendor_id:
        return 0
    
    consumer = StreamConsumer(config.vendor_id)
    
    # Claim very old messages
    max_idle_ms = max_idle_hours * 60 * 60 * 1000
    stuck = consumer.claim_pending(min_idle_ms=max_idle_ms, count=100)
    
    # Acknowledge them (remove from stream)
    if stuck:
        msg_ids = [msg_id for msg_id, _ in stuck]
        consumer.acknowledge(msg_ids)
        
        frappe.logger().warning(
            f"Purged {len(msg_ids)} stuck messages (idle > {max_idle_hours}h)"
        )
        
        return len(msg_ids)
    
    return 0
