"""
Auth failure rate limiter — blocks brute-force attacks on webhook endpoints.

Tracks failed authentication attempts per IP using Redis. After
MAX_FAILURES failures within the WINDOW, the IP is blocked for BLOCK_DURATION.
"""
import frappe
from frappe import _

MAX_FAILURES = 10
WINDOW_SECONDS = 300
BLOCK_DURATION = 900


def check_rate_limit(key, max_failures=None, window=None):
    max_failures = max_failures or MAX_FAILURES
    window = window or WINDOW_SECONDS
    cache = frappe.cache()
    block_key = f"sm_auth_block:{key}"
    if cache.get_value(block_key):
        return False
    count_key = f"sm_auth_count:{key}"
    count = cache.get_value(count_key) or 0
    if count >= max_failures:
        cache.set_value(block_key, 1, expires_in_sec=BLOCK_DURATION)
        cache.delete_key(count_key)
        _log_rate_limit_event(key, "blocked")
        return False
    return True


def record_failure(key, window=None):
    window = window or WINDOW_SECONDS
    cache = frappe.cache()
    count_key = f"sm_auth_count:{key}"
    count = cache.get_value(count_key) or 0
    cache.set_value(count_key, count + 1, expires_in_sec=window)
    if count + 1 >= MAX_FAILURES:
        _log_rate_limit_event(key, "threshold_reached")


def clear_failures(key):
    cache = frappe.cache()
    cache.delete_key(f"sm_auth_count:{key}")
    cache.delete_key(f"sm_auth_block:{key}")


def is_blocked(key):
    return bool(frappe.cache().get_value(f"sm_auth_block:{key}"))


def _log_rate_limit_event(key, event_type):
    try:
        frappe.log_error(
            title=f"Auth Rate Limit — {event_type}",
            message=f"Key: {key}, Event: {event_type}, Max failures: {MAX_FAILURES}",
        )
    except Exception:
        pass
