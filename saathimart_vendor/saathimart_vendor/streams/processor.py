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
            frappe.log_error(
                f"No handler for event type: {event_type}",
                "Stream Event"
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

def handle_order_new(payload: Dict[str, Any]) -> bool:
    """
    Handle order.new event from hub.
    
    Creates Vendor Order in ERPNext.
    Idempotent: checks if order already exists.
    """
    order_id = payload.get("order_id")
    
    if not order_id:
        frappe.log_error("Missing order_id in order.new payload", "Stream Event")
        return False
    
    # Check if already exists (idempotency)
    if frappe.db.exists("Vendor Order", {"hub_order_id": order_id}):
        frappe.logger().info(f"Vendor Order already exists for {order_id}")
        return True
    
    try:
        from saathimart_vendor.saathimart_vendor.doctype.vendor_order.vendor_order import create_vendor_order
        create_vendor_order(payload)
        return True
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"Failed to create Vendor Order {order_id}")
        return False


def handle_payment_received(payload: Dict[str, Any]) -> bool:
    """
    Handle payment.received event from hub.
    
    Updates Vendor Order payment status.
    """
    order_id = payload.get("order_id")
    amount = payload.get("amount")
    gateway = payload.get("gateway")
    
    if not order_id:
        return False
    
    try:
        # Find vendor order
        vo_name = frappe.db.get_value("Vendor Order", {"hub_order_id": order_id}, "name")
        
        if not vo_name:
            frappe.logger().warning(f"Vendor Order not found for {order_id}")
            return True  # Acknowledge (order may not be for this vendor)
        
        # Update payment status
        vo = frappe.get_doc("Vendor Order", vo_name)
        vo.payment_status = "Paid"
        vo.payment_method = gateway
        vo.save(ignore_permissions=True)
        
        frappe.logger().info(f"Updated payment status for {vo_name}")
        return True
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"Failed to update payment for {order_id}")
        return False


def handle_order_cancelled(payload: Dict[str, Any]) -> bool:
    """
    Handle order.cancel event from hub.
    
    Cancels Vendor Order if not yet delivered.
    """
    order_id = payload.get("order_id")
    reason = payload.get("reason", "Cancelled by hub")
    
    if not order_id:
        return False
    
    try:
        vo_name = frappe.db.get_value("Vendor Order", {"hub_order_id": order_id}, "name")
        
        if not vo_name:
            return True  # Not for this vendor
        
        vo = frappe.get_doc("Vendor Order", vo_name)
        
        # Check if can cancel
        if vo.status in ("Delivered", "Cancelled"):
            return True  # Already in terminal state
        
        # Cancel via controller method
        vo.cancel_order(reason=reason)
        
        return True
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"Failed to cancel order {order_id}")
        return False


def handle_stock_update(payload: Dict[str, Any]) -> bool:
    """
    Handle stock.update event from hub.
    
    Syncs stock quantities.
    """
    product_id = payload.get("product_id")
    qty_change = payload.get("qty_change")
    
    if not product_id or qty_change is None:
        return False
    
    try:
        # Find product mapping
        mapping = frappe.db.get_value(
            "Product Mapping",
            {"hub_product_id": product_id, "is_active": 1},
            ["name", "item_code"],
            as_dict=True
        )
        
        if not mapping:
            frappe.logger().warning(f"No mapping for product {product_id}")
            return True  # Not our product
        
        # Update ERPNext stock
        # (Implementation depends on your stock sync logic)
        frappe.logger().info(f"Stock update for {mapping.item_code}: {qty_change}")
        
        return True
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"Failed to update stock for {product_id}")
        return False


def handle_price_update(payload: Dict[str, Any]) -> bool:
    """
    Handle price.update event from hub.
    
    Updates item price in ERPNext.
    """
    product_id = payload.get("product_id")
    new_price = payload.get("new_price")
    
    if not product_id or new_price is None:
        return False
    
    try:
        # Find product mapping
        mapping = frappe.db.get_value(
            "Product Mapping",
            {"hub_product_id": product_id, "is_active": 1},
            ["name", "item_code"],
            as_dict=True
        )
        
        if not mapping:
            return True  # Not our product
        
        # Update item price
        # (Implementation depends on your price sync logic)
        frappe.logger().info(f"Price update for {mapping.item_code}: {new_price}")
        
        return True
        
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"Failed to update price for {product_id}")
        return False


# Register handlers
EventProcessor.register_handler("order.new", handle_order_new)
EventProcessor.register_handler("payment.received", handle_payment_received)
EventProcessor.register_handler("order.cancel", handle_order_cancelled)
EventProcessor.register_handler("stock.update", handle_stock_update)
EventProcessor.register_handler("price.update", handle_price_update)
