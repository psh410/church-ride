# Sends the SMS confirmation a rider gets when they sign up for a ride.
#
# Triggered by the Apps Script attached to the signup form, which hands
# over the row it just wrote (see the /confirm-rider-signup endpoint in
# cloud_app.py). Nothing here trusts a caller-supplied phone number: the
# row is read straight from the sheet, so the only numbers reachable are
# ones that actually signed up.
#
# What a rider gets depends on what the sheet says, because not everyone
# who signs up is getting a shuttle seat:
#
#   - a recognized stop        -> confirmed for pickup, with the time
#   - stop flagged "/driver"   -> shuttle was full, personal driver coming
#   - an off-route address     -> not on a route, personal driver coming
#   - stop flagged "/duplicate"-> nothing, they were already told
#   - consent box unchecked    -> nothing, ever
#
# The last two are the ones to be careful about. Texting someone who
# declined SMS breaks what the A2P campaign registration promises, and
# the "/driver" case matters because telling someone they're "confirmed
# for pickup at SDRP at 9:10" when the shuttle is full would be a
# straightforwardly false statement.

from __future__ import annotations

import logging

from db.firestore_client import record_rider_confirmed, was_rider_confirmed
from functions.read_riders_sheet import (
    get_next_sunday_date,
    get_signup_row,
    get_stop_to_shuttle_map,
)
from functions.read_sheets import get_routes
from functions.send_sms import BRAND_PREFIX, OPT_OUT_NOTICE, normalize_to_e164, send_sms

logger = logging.getLogger(__name__)

# Flags the Apps Script appends to the campus address cell as
# "<stop>/duplicate" and "<stop>/driver" (see
# apps_script/rider_signup_flags.gs, appendFlagToAddress).
DUPLICATE_FLAG = "/duplicate"
CAPACITY_FLAG = "/driver"


def confirm_signup(row_number: int) -> dict:
    """Send the right confirmation for one signup row, if any is due.

    Args:
        row_number: 1-indexed row in the "Form Responses 1" tab, as the
            Apps Script sees it. Row 1 is the header.

    Returns:
        dict: {"status": "sent"} when a text went out, {"status":
            "skipped", "reason": str} when one deliberately didn't, or
            {"status": "failed", "reason": str} on an error.
    """
    try:
        signup = get_signup_row(row_number)
        if signup is None:
            return {"status": "failed", "reason": f"no signup at row {row_number}"}

        name = signup.get("name") or ""
        phone = signup.get("phone") or ""
        raw_stop = (signup.get("stop") or "").strip()

        if not signup.get("sms_consent"):
            logger.info(
                "Row %s (%r): SMS consent not given; sending nothing.",
                row_number,
                name,
            )
            return {"status": "skipped", "reason": "no sms consent"}

        if not phone.strip():
            logger.info("Row %s (%r): no phone number on file.", row_number, name)
            return {"status": "skipped", "reason": "no phone number"}

        if raw_stop.lower().endswith(DUPLICATE_FLAG):
            logger.info(
                "Row %s (%r): duplicate signup; already confirmed.", row_number, name
            )
            return {"status": "skipped", "reason": "duplicate signup"}

        try:
            normalized = normalize_to_e164(phone)
        except ValueError as exc:
            logger.warning("Row %s (%r): %s", row_number, name, exc)
            return {"status": "skipped", "reason": "unusable phone number"}

        sunday_date = get_next_sunday_date()

        # Belt and braces against a retried call or a second submission
        # the Apps Script didn't catch.
        try:
            if was_rider_confirmed(normalized, sunday_date):
                logger.info(
                    "Row %s (%r): already confirmed for %s.",
                    row_number,
                    name,
                    sunday_date,
                )
                return {"status": "skipped", "reason": "already confirmed this week"}
        except RuntimeError as exc:
            # Fail open on the lookup: a Firestore hiccup shouldn't cost
            # a rider their confirmation. Worst case is a duplicate text.
            logger.warning(
                "Row %s: could not check prior confirmation (%s); sending anyway.",
                row_number,
                exc,
            )

        message = build_confirmation_message(name, raw_stop)
        if message is None:
            return {"status": "skipped", "reason": "no message for this signup"}

        if not send_sms(normalized, message):
            logger.error("Row %s (%r): SMS send failed.", row_number, name)
            return {"status": "failed", "reason": "sms send failed"}

        try:
            record_rider_confirmed(
                normalized,
                sunday_date,
                {"row": row_number, "stop": raw_stop, "name": name},
            )
        except RuntimeError as exc:
            logger.warning("Row %s: confirmation sent but not recorded: %s", row_number, exc)

        logger.info("Row %s (%r): confirmation sent for %s.", row_number, name, sunday_date)
        return {"status": "sent"}
    except Exception as exc:
        logger.error("Rider confirmation failed for row %s: %s", row_number, exc)
        return {"status": "failed", "reason": str(exc)}


def build_confirmation_message(name: str, raw_stop: str) -> str | None:
    """Build the confirmation text for a signup, or None if none applies.

    Args:
        name: The rider's name as submitted. Only the first word is used.
        raw_stop: The campus address cell exactly as it appears in the
            sheet, flags included.

    Returns:
        str or None: The SMS body, or None for a duplicate signup.
    """
    first_name = _first_name(name)
    stop = raw_stop.strip()

    if stop.lower().endswith(DUPLICATE_FLAG):
        return None

    if stop.lower().endswith(CAPACITY_FLAG):
        return (
            f"{BRAND_PREFIX} Hi {first_name}, thanks for signing up. The "
            f"shuttle is full this Sunday, so we're arranging a personal "
            f"driver and will follow up. {OPT_OUT_NOTICE}"
        )

    shuttle_id = _lookup_shuttle_id(stop, get_stop_to_shuttle_map())
    if shuttle_id is None:
        return (
            f"{BRAND_PREFIX} Hi {first_name}, thanks for signing up. Your "
            f"address isn't on a shuttle route, so we're arranging a "
            f"personal driver and will follow up. {OPT_OUT_NOTICE}"
        )

    pickup_time = _lookup_pickup_time(stop)
    if not pickup_time:
        # The stop is on a route but has no time. Don't invent one.
        logger.error("No pickup_time for stop=%r (shuttle_id=%r).", stop, shuttle_id)
        return (
            f"{BRAND_PREFIX} Hi {first_name}, you're confirmed for a ride "
            f"from {stop} this Sunday. {OPT_OUT_NOTICE}"
        )

    return (
        f"{BRAND_PREFIX} Hi {first_name}, you're confirmed for pickup at "
        f"{stop} at {pickup_time} this Sunday. {OPT_OUT_NOTICE}"
    )


def send_rider_confirmation(name: str, phone: str, stop: str) -> dict:
    """Send a confirmation directly, without going through the sheet.

    Kept for manual use and tests. The live path is confirm_signup(),
    which reads the row itself and enforces consent. This function does
    NOT check consent, so don't wire it to anything automatic.

    Args:
        name: Rider's name. Only the first word is used in the message.
        phone: Rider's phone number, any format.
        stop: The campus stop or address, flags included.

    Returns:
        dict: {"status": "sent"}, or {"status": "skipped"/"failed",
            "reason": str}.
    """
    try:
        message = build_confirmation_message(name, stop)
        if message is None:
            return {"status": "skipped", "reason": "duplicate signup"}

        if send_sms(phone, message):
            logger.info("Sent rider confirmation to %r for stop=%r.", name, stop)
            return {"status": "sent"}

        logger.error("SMS send failed for rider=%r stop=%r.", name, stop)
        return {"status": "failed", "reason": "sms send failed"}
    except Exception as exc:
        logger.error(
            "Rider confirmation failed for name=%r stop=%r: %s", name, stop, exc
        )
        return {"status": "failed", "reason": str(exc)}


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------
def _first_name(name: str) -> str:
    """Return the rider's first name, or "there" if we don't have one."""
    cleaned = (name or "").strip()
    return cleaned.split()[0] if cleaned else "there"


def _lookup_shuttle_id(stop: str, shuttle_map: dict) -> str | None:
    """Return the shuttle_id serving stop, or None if it isn't a stop."""
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
    """Return the pickup time for stop by walking get_routes()."""
    stop_lower = stop.lower()
    for route in get_routes():
        for route_stop in route.get("stops", []):
            stop_name = (route_stop.get("stop_name") or "").strip()
            if stop_name == stop or stop_name.lower() == stop_lower:
                return (route_stop.get("pickup_time") or "").strip() or None
    return None
