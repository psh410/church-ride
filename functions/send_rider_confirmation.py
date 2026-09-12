# Sends an SMS pickup confirmation to a rider who just signed up.

from __future__ import annotations

import logging

from functions.read_riders_sheet import get_stop_to_shuttle_map
from functions.read_sheets import get_routes
from functions.send_sms import BRAND_PREFIX, OPT_OUT_NOTICE, send_sms

logger = logging.getLogger(__name__)


def send_rider_confirmation(name: str, phone: str, stop: str) -> dict:
    """Send an SMS confirmation to a rider who just signed up
    for a shuttle ride.

    Args:
        name: Rider's first name (or full name, will use first
            word only in the message)
        phone: Rider's phone number, any format
        stop: The campus stop they selected (e.g. "SDRP", "ISR")

    Returns:
        dict: {"status": "sent" or "failed", "reason": str if failed}
            or {"status": "skipped", "reason": "not a shuttle stop"}.
    """
    try:
        stop_name = (stop or "").strip()
        shuttle_map = get_stop_to_shuttle_map()
        shuttle_id = _lookup_shuttle_id(stop_name, shuttle_map)
        if shuttle_id is None:
            logger.info(
                "Skipping rider confirmation for %r: %r is not a shuttle stop.",
                name,
                stop_name,
            )
            return {"status": "skipped", "reason": "not a shuttle stop"}

        pickup_time = _lookup_pickup_time(stop_name)
        if not pickup_time:
            logger.error(
                "No pickup_time found for stop=%r (shuttle_id=%r).",
                stop_name,
                shuttle_id,
            )
            return {"status": "failed", "reason": f"no pickup time for stop {stop_name!r}"}

        first_name = (name or "").strip().split()[0] if (name or "").strip() else "there"
        message = (
            f"{BRAND_PREFIX} Hi {first_name}, you're confirmed for pickup at "
            f"{stop_name} at {pickup_time} this Sunday. {OPT_OUT_NOTICE}"
        )

        sent = send_sms(phone, message)
        if sent:
            logger.info(
                "Sent rider confirmation to %r at stop=%r pickup=%r.",
                first_name,
                stop_name,
                pickup_time,
            )
            return {"status": "sent"}

        logger.error(
            "SMS send failed for rider=%r phone=%r stop=%r.",
            name,
            phone,
            stop_name,
        )
        return {"status": "failed", "reason": "sms send failed"}
    except Exception as exc:
        logger.error(
            "Rider confirmation failed for name=%r phone=%r stop=%r: %s",
            name,
            phone,
            stop,
            exc,
        )
        return {"status": "failed", "reason": str(exc)}


def _lookup_shuttle_id(stop: str, shuttle_map: dict) -> str | None:
    """Return the shuttle_id for stop, or None if it is not a shuttle stop."""
    if not stop:
        return None
    if stop in shuttle_map:
        return shuttle_map[stop]
    stop_lower = stop.lower()
    for name, shuttle_id in shuttle_map.items():
        if str(name).lower() == stop_lower:
            return shuttle_id
    return None


def _lookup_pickup_time(stop: str) -> str | None:
    """Return pickup_time for stop by walking get_routes() stops."""
    stop_lower = stop.lower()
    for route in get_routes():
        for route_stop in route.get("stops", []):
            stop_name = (route_stop.get("stop_name") or "").strip()
            if stop_name == stop or stop_name.lower() == stop_lower:
                pickup_time = (route_stop.get("pickup_time") or "").strip()
                return pickup_time or None
    return None
