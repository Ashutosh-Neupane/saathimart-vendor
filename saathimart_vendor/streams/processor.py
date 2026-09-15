"""
Event Processor - Routes and processes events from hub stream.

Each event type has a dedicated handler. Handlers are idempotent
(can safely be called multiple times for the same event).
"""
import frappe
import json
from frappe import _
from typing import Dict, Any, Callable


class EventProcessor:
    """
    Routes events to appropriate handlers.
    
    Event handlers must be idempotent because:
    1. Messages may be delivered more than once (at-least-once semantics)
    2. Stuck messages are retried via XCLAIM
    3. Worker may crash and restart
    
    Event deduplication is done via event_id in the payload.
    """
    
    # Event type → handler mapping
    HANDLERS = {}
    
    @classmethod
    def register_handler(cls, event_type: str, handler: Callable):
        """
        Register a handler for an event type.
        
        Args:
            event_type: Event type (e.g., "order.new")
            handler: Callable that takes payload dict and returns bool (success)
        
        Example:
            >>> @EventProcessor.register_handler("order.new")
            ... def handle_order_new(payload):
            ...     # Process order
            ...     return True
        """
        cls.HANDLERS[event_type] = handler
    
    @classmethod
    def process(cls, event: Dict[str, Any]) -> bool:
        """
        Process a single event.
        
        Args:
            event: Event dict with keys: event_type, event_id, payload, timestamp
        
        Returns:
            True if successful (will be acknowledged)
            False if failed (will be retried)
        
        Example:
            >>> event = {
            ...     "event_type": "order.new",
            ...     "event_id": "1234567890-abc123",
            ...     "payload": '{"order_id": "ORD-001"}',
            ...     "timestamp": "2024-01-01T12:00:00"
            ... }
            >>> success = EventProcessor.process(event)
        """
        event_type = event.get("event_type", "")
        event_id = event.get("event_id", "")
        payload_str = event.get("payload", "{}")
        
        # Parse payload
        try:
            payload = json.loads(payload_str) if isinstance(payload_str, str) else payload_str
        except json.JSONDecodeError as e:
            frappe.log_error(frappe.get_traceback(), f"Invalid JSON payload for event {event_id}")
            return True  # Acknowledge to avoid retry loop
        
        # Check idempotency (has this event been processed before?)
        if cls._is_duplicate(event_id, event_type):
            frappe.logger().info(f"Skipping duplicate event {event_id}")
            return True  # Acknowledge duplicate
        
        # Get handler
        handler = cls.HANDLERS.get(event_type)
        if not handler:
            # Throttle: an unknown type arriving in bulk (a misconfigured
            # burst, a hub feature the vendor hasn't upgraded to yet) must
            # not write one Error Log row per message — 10k messages would
            # create 10k rows. Log once per type per day instead; the
            # messages themselves are still consumed (return True).
            seen_key = f"sm_stream_no_handler:{event_type}"
            if not frappe.cache().get(seen_key):
                frappe.cache().setex(seen_key, 86400, "1")
                frappe.log_error(
                    f"No handler for event type: {event_type} "
                    f"(further occurrences suppressed for 24h)",
                    "Stream Event",
                )
            return True  # Acknowledge unknown events to avoid retry loop
        
        # Execute handler
        try:
            success = handler(payload)
            
            if success:
                # Mark as processed
                cls._mark_processed(event_id, event_type)
            
            return success
            
        except Exception as e:
            frappe.log_error(
                frappe.get_traceback(),
                f"Event handler failed: {event_type} (event_id={event_id})"
            )
            return False  # Will be retried
    
    @staticmethod
    def _is_duplicate(event_id: str, event_type: str) -> bool:
        """
        Check if event has already been processed.
        
        Uses a simple cache key with TTL of 24 hours.
        """
        cache_key = f"event:processed:{event_id}"
        return bool(frappe.cache().get(cache_key))
    
    @staticmethod
    def _mark_processed(event_id: str, event_type: str):
        """Mark event as processed (24-hour TTL)."""
        cache_key = f"event:processed:{event_id}"
        frappe.cache().setex(cache_key, 86400, "1")  # 24 hours


# ── Event Handlers ─────────────────────────────────────────────────────
#
# Single source of truth: every handler delegates to the SAME functions the
# live webhook uses (saathimart_vendor.api.receive.dispatch_event), so an
# event that arrives via the Redis Stream behaves identically to one pushed
# via webhook — same idempotency, same accounting (commission, TDS,
# reimbursements), same status transitions. Previously this module had its
# own stub handlers that created the order and flipped payment WITHOUT
# booking any accounting, and the two transports could drift apart.

def handle_order_new(payload: Dict[str, Any]) -> bool:
    """Stream-path order.new → identical handler to the webhook path."""
    from saathimart_vendor.api.receive import dispatch_event
    dispatch_event("order.new", payload)
    return True


def handle_payment_received(payload: Dict[str, Any]) -> bool:
    """Stream-path payment.received → identical handler to the webhook path."""
    from saathimart_vendor.api.receive import dispatch_event
    dispatch_event("payment.received", payload)
    return True


def handle_order_cancelled(payload: Dict[str, Any]) -> bool:
    """Stream-path order.cancel → identical handler to the webhook path."""
    from saathimart_vendor.api.receive import dispatch_event
    dispatch_event("order.cancel", payload)
    return True


def handle_stock_snapshot(payload: Dict[str, Any]) -> bool:
    """Stream-path stock.snapshot → identical handler to the webhook path."""
    from saathimart_vendor.api.receive import dispatch_event
    dispatch_event("stock.snapshot", payload)
    return True


def handle_product_new(payload: Dict[str, Any]) -> bool:
    """Stream-path product.new → identical handler to the webhook path."""
    from saathimart_vendor.api.receive import dispatch_event
    dispatch_event("product.new", payload)
    return True


def handle_settlement(payload: Dict[str, Any]) -> bool:
    """Stream-path settlement.completed → identical handler to the webhook path."""
    from saathimart_vendor.api.receive import dispatch_event
    dispatch_event("settlement.completed", payload)
    return True


# Register handlers — every type the webhook dispatch map supports, so the
# stream path is a full superset fallback, not a partial mirror.
EventProcessor.register_handler("order.new", handle_order_new)
EventProcessor.register_handler("payment.received", handle_payment_received)
EventProcessor.register_handler("order.cancel", handle_order_cancelled)
EventProcessor.register_handler("product.new", handle_product_new)
EventProcessor.register_handler("stock.snapshot", handle_stock_snapshot)
EventProcessor.register_handler("settlement.completed", handle_settlement)
