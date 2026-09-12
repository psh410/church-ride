# Sends Sunday-morning SMS reminders to the assigned shuttle drivers
# (pickup, return, and backup) using the semester schedule in Firestore.

from __future__ import annotations

import logging
from datetime import datetime

from db.firestore_client import get_semester_schedule, is_phone_opted_out
from functions.read_riders_sheet import get_next_sunday_date
from functions.read_sheets import get_all_drivers_with_history, get_routes
from functions.send_sms import (
    BRAND_PREFIX,
    OPT_OUT_NOTICE,
    normalize_to_e164,
    send_sms,
)

logger = logging.getLogger(__name__)

_SHUTTLE_LABELS = {
    "shuttle_1": "Shuttle 1 (Gray Van)",
    "shuttle_2": "Shuttle 2 (Silver Van)",
}


def _format_stops_for_sms(shuttle_id: str, routes: list[dict]) -> str:
    """Return a short "FAR 9:05, SDRP 9:10" stop list for a shuttle.

    Drops AM/PM from pickup times to keep the SMS short. Stops stay in
    Routes-tab order.

    Args:
        shuttle_id: e.g. "shuttle_1".
        routes: Output of get_routes().

    Returns:
        str: Comma-separated "stop time" pairs, or "" if no route
            matches shuttle_id.
    """
    route = next((r for r in routes if r.get("shuttle_id") == shuttle_id), None)
    if route is None:
        return ""

    parts: list[str] = []
    for stop in route.get("stops", []):
        name = (stop.get("stop_name") or "").strip()
        time = _strip_ampm(stop.get("pickup_time") or "")
        if not name:
            continue
        parts.append(f"{name} {time}".strip())
    return ", ".join(parts)


def _get_driver_phone(name: str, drivers: list[dict]) -> str | None:
    """Return a driver's phone from the enriched drivers list.

    Tries a case-insensitive full-name match first, then first-name
    only if that fails (e.g. schedule "Ryan" vs roster "Ryan Bielak").

    Args:
        name: Driver name as stored on the semester schedule.
        drivers: Output of get_all_drivers_with_history().

    Returns:
        str or None: The phone number if found, otherwise None.
    """
    if not name:
        return None

    target = name.strip().lower()
    for driver in drivers:
        driver_name = (driver.get("name") or "").strip()
        if driver_name.lower() == target:
            return driver.get("phone") or None

    target_first = target.split()[0]
    for driver in drivers:
        driver_name = (driver.get("name") or "").strip()
        if not driver_name:
            continue
        if driver_name.lower().split()[0] == target_first:
            return driver.get("phone") or None

    return None


def send_driver_sms_reminders() -> dict:
    """Text this Sunday's pickup, return, and backup drivers.

    Looks up the upcoming Sunday's semester_schedule entry, resolves
    phones from the Available Drivers / Form Responses roster, and
    sends one SMS per assigned driver. Split-shift weeks send a
    second text to the return-only driver.

    Returns:
        dict: {"status": "completed", "results": {...}} on a finished
            run, or {"status": "failed", "reason": str} if the Sunday
            has no schedule entry or a hard error occurs.
    """
    try:
        sunday_date = get_next_sunday_date()
        schedule = get_semester_schedule()
        entry = _find_schedule_entry(schedule, sunday_date)
        if entry is None:
            logger.error("No semester schedule entry for sunday=%s.", sunday_date)
            return {
                "status": "failed",
                "reason": "no schedule entry for this Sunday",
            }

        routes = get_routes()
        drivers = get_all_drivers_with_history(_to_sheet_date_format(sunday_date))

        results: dict = {
            "sunday": sunday_date,
            "shuttle_1": _remind_shuttle("shuttle_1", entry, routes, drivers),
            "shuttle_2": _remind_shuttle("shuttle_2", entry, routes, drivers),
            "backup": _remind_backup(entry, drivers),
        }
        return {"status": "completed", "results": results}
    except Exception as exc:
        logger.error("Driver SMS reminders failed: %s", exc)
        return {"status": "failed", "reason": str(exc)}


def _remind_shuttle(
    shuttle_id: str,
    entry: dict,
    routes: list[dict],
    drivers: list[dict],
) -> dict:
    """Send pickup (and return, if split) reminders for one shuttle."""
    label = _SHUTTLE_LABELS[shuttle_id]
    stops = _format_stops_for_sms(shuttle_id, routes)
    base_name = (entry.get(shuttle_id) or "").strip() or None
    pickup_name = (entry.get(f"{shuttle_id}_pickup") or base_name or "").strip() or None
    return_name = (entry.get(f"{shuttle_id}_return") or base_name or "").strip() or None

    if not pickup_name and not return_name:
        return {"status": "skipped", "reason": "no driver assigned"}

    pickup_result = _send_driver_message(
        pickup_name,
        drivers,
        (
            f"{BRAND_PREFIX} Hi {_first_name(pickup_name or '')}, reminder "
            f"you're driving {label} this Sunday. Stops: {stops}. Be at "
            f"church by 8:30 AM. {OPT_OUT_NOTICE}"
        ),
    )

    result: dict = {
        "pickup": {"name": pickup_name, **pickup_result},
        "return": None,
    }

    split = (
        pickup_name
        and return_name
        and pickup_name.lower() != return_name.lower()
    )
    if split:
        return_result = _send_driver_message(
            return_name,
            drivers,
            (
                f"{BRAND_PREFIX} Hi {_first_name(return_name or '')}, reminder "
                f"you're driving the {label} RETURN leg only this Sunday. "
                f"Stops: {stops}. Be at church by 8:30 AM. {OPT_OUT_NOTICE}"
            ),
        )
        result["return"] = {"name": return_name, **return_result}

    return result


def _remind_backup(entry: dict, drivers: list[dict]) -> dict | str:
    """Send the backup-driver text, or return "none" if none is listed."""
    backup_name = entry.get("backup")
    if not backup_name:
        return "none"

    backup_name = str(backup_name).strip()
    if not backup_name:
        return "none"

    send_result = _send_driver_message(
        backup_name,
        drivers,
        (
            f"{BRAND_PREFIX} Hi {_first_name(backup_name)}, you're the BACKUP "
            f"driver this Sunday in case Shuttle 1 or Shuttle 2 needs "
            f"coverage. Be at church by 8:30 AM. {OPT_OUT_NOTICE}"
        ),
    )
    return {"name": backup_name, **send_result}


def _send_driver_message(name: str | None, drivers: list[dict], body: str) -> dict:
    """Look up a phone and send one SMS. Returns a small status dict."""
    if not name:
        return {"status": "skipped", "reason": "no driver assigned"}

    phone = _get_driver_phone(name, drivers)
    if not phone:
        logger.warning("No phone on file for driver %r; not sending SMS.", name)
        return {"status": "failed", "reason": "no phone found"}

    # Check opt-out status ourselves first (rather than only relying on
    # send_sms()'s own check) so a driver who has texted STOP shows up
    # here as "skipped: opted out" - a clear signal they need a
    # replacement or a direct call - instead of an ambiguous "sms send
    # failed". send_sms() still re-checks this itself for any other
    # caller that isn't going through this function.
    try:
        normalized = normalize_to_e164(phone)
        if is_phone_opted_out(normalized):
            logger.warning(
                "Not texting driver %r: %s has opted out of SMS.", name, normalized
            )
            return {"status": "skipped", "reason": "driver opted out of SMS"}
    except (ValueError, RuntimeError) as exc:
        logger.warning(
            "Could not check opt-out status for driver %r phone=%r: %s",
            name, phone, exc,
        )

    if send_sms(phone, body):
        logger.info("Sent driver SMS reminder to %r.", name)
        return {"status": "sent"}

    logger.error("SMS send failed for driver %r phone=%r.", name, phone)
    return {"status": "failed", "reason": "sms send failed"}


def _find_schedule_entry(schedule: list[dict], sunday_date: str) -> dict | None:
    """Return the semester schedule document for sunday_date, if any."""
    for entry in schedule:
        if entry.get("date") == sunday_date:
            return entry
    return None


def _to_sheet_date_format(iso_date: str) -> str:
    """Convert "YYYY-MM-DD" to the Sheets tab's "M/D/YY" format."""
    parsed = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{parsed.month}/{parsed.day}/{parsed.strftime('%y')}"


def _strip_ampm(pickup_time: str) -> str:
    """Drop AM/PM from a pickup time so SMS stays short."""
    cleaned = pickup_time.replace(" AM", "").replace(" PM", "")
    cleaned = cleaned.replace("AM", "").replace("PM", "")
    return cleaned.strip()


def _first_name(name: str) -> str:
    """Return the first word of a driver name."""
    parts = name.strip().split()
    return parts[0] if parts else ""
