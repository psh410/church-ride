# Handles the three recurring email workflows for the church ride
# coordination system: the Wednesday driver reminder, the Saturday
# rider-count update (sent at noon/6PM/9PM), and the immediate
# "shuttle full" alert.

from __future__ import annotations

import logging
from datetime import datetime

from config import settings
from db.firestore_client import get_assignment
from functions.read_riders_sheet import (
    get_next_sunday_date,
    get_rider_counts,
    get_riders_for_sunday,
    is_shuttle_full,
)
from functions.read_sheets import get_all_drivers_with_history, get_routes
from functions.send_email import send_email

logger = logging.getLogger(__name__)

CHURCH_ADDRESS = "2906 Crossing Ct, Champaign, IL"
SERVICE_TIME = "9:30 AM"
OVERSEER_DRIVER_CONTACT_NAME = "Dae Kang"

# Friendly display name (including which van) for each shuttle.
SHUTTLE_NAMES = {
    "shuttle_1": "Shuttle 1 (Ford Transit - Gray)",
    "shuttle_2": "Shuttle 2 (GMC Savanna - Silver)",
}

# Pickup time for each campus stop, regardless of shuttle.
STOP_TIMES = {
    "FAR": "9:05 AM",
    "SDRP": "9:10 AM",
    "Allen": "9:00 AM",
    "ISR": "9:05 AM",
    "Icon": "9:10 AM",
}

# Link to the full rider signup sheet, included at the bottom of the
# Saturday status update email.
RIDER_SIGNUP_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1efQiqUoqAX5uNF22Aw8v6AkfGC3IjuSLBdcg23LldCw/edit?usp=sharing"
)


def send_wednesday_reminder(sunday_date: str) -> dict:
    """Send one combined reminder email to all of this Sunday's drivers.

    Unlike a per-driver reminder, this sends a single email addressed to
    every assigned driver at once, listing each driver's shuttle/van and
    every stop it serves (with pickup times, regardless of rider count)
    plus standard van pickup/return instructions. No rider names or
    counts are included, and no Claude drafting is used - the body is
    built directly from static text and Sheets data.

    Args:
        sunday_date: The Sunday date to send the reminder for, in ISO
            "YYYY-MM-DD" format.

    Returns:
        dict: {"sent_count": int, "failures": list[dict]}. sent_count is
            1 if the combined email was sent successfully, 0 otherwise.

    Raises:
        RuntimeError: If assignments, routes, or driver data can't be
            read.
    """
    try:
        assignments = get_assignment(sunday_date)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read assignments for sunday_date={sunday_date!r}: {exc}"
        ) from exc

    if not assignments:
        logger.info("No assignments found for sunday_date=%r; nothing to remind.", sunday_date)
        return {"sent_count": 0, "failures": []}

    try:
        routes = get_routes()
    except Exception as exc:
        raise RuntimeError(f"Failed to read routes: {exc}") from exc

    # Drivers only exist in Sheets with a name (no stable ID), so we look
    # each assignment's driver up by name to get their email address.
    try:
        drivers_by_name = {
            driver["name"]: driver
            for driver in get_all_drivers_with_history(_to_sheet_date_format(sunday_date))
        }
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read driver details for sunday_date={sunday_date!r}: {exc}"
        ) from exc

    driver_emails = []
    failures: list[dict] = []

    for assignment in assignments:
        driver_name = assignment.get("driver_name") or assignment.get("driver_id")
        driver = drivers_by_name.get(driver_name)
        if driver is None or not driver.get("email"):
            logger.error("No email on file for driver %r; excluding from reminder.", driver_name)
            failures.append({"driver_name": driver_name, "error": "No email on file."})
            continue
        driver_emails.append(driver["email"])

    if not driver_emails:
        return {"sent_count": 0, "failures": failures}

    body = _build_wednesday_reminder_body(sunday_date, assignments, routes)

    try:
        sent = send_email(
            to=", ".join(driver_emails),
            subject=f"CFC Sunday Shuttle Reminder - {_format_short_date(sunday_date)}",
            body=body,
            cc=settings.OVERSEER_DRIVER_EMAIL,
            bcc=settings.BCC_EMAIL,
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to send Wednesday reminder email: {exc}") from exc

    if not sent:
        failures.append({"driver_name": "all", "error": "send_email returned False."})
        return {"sent_count": 0, "failures": failures}

    return {"sent_count": 1, "failures": failures}


def send_saturday_update(sunday_date: str) -> dict:
    """Send the overseer a plain-text rider-count status update.

    Called at noon, 6 PM, and 9 PM on Saturday. Purely a status report -
    no Claude drafting, since the content is simple and time-sensitive.

    Args:
        sunday_date: The Sunday date to report counts for, in ISO
            "YYYY-MM-DD" format.

    Returns:
        dict: {"status": "sent" or "failed"}.

    Raises:
        RuntimeError: If rider counts or the rider list can't be read.
    """
    try:
        counts = get_rider_counts(sunday_date)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read rider counts for sunday_date={sunday_date!r}: {exc}"
        ) from exc

    try:
        riders = get_riders_for_sunday(sunday_date)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read riders for sunday_date={sunday_date!r}: {exc}"
        ) from exc

    full_shuttles = []
    for shuttle_id in SHUTTLE_NAMES:
        try:
            if is_shuttle_full(shuttle_id, sunday_date):
                full_shuttles.append(shuttle_id)
        except Exception as exc:
            logger.error("Failed to check capacity for %r: %s", shuttle_id, exc)

    body = _build_saturday_summary(counts, riders, full_shuttles)

    try:
        sent = send_email(
            to=settings.OVERSEER_DRIVER_EMAIL,
            subject=f"CFC Shuttle Update - Sunday {sunday_date}",
            body=body,
            cc=settings.OVERSEER_RIDE_EMAIL,
            bcc=settings.BCC_EMAIL,
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to send Saturday update email: {exc}") from exc

    if not sent:
        logger.error("send_email returned False for Saturday update (sunday_date=%r).", sunday_date)
        return {"status": "failed"}

    return {"status": "sent"}


def send_shuttle_full_alert(shuttle_id: str, sunday_date: str) -> dict:
    """Send an immediate alert that a shuttle has hit capacity.

    Args:
        shuttle_id: The shuttle that's full, e.g. "shuttle_1".
        sunday_date: The Sunday date this alert is for, in ISO
            "YYYY-MM-DD" format.

    Returns:
        dict: {"status": "sent" or "failed"}.

    Raises:
        RuntimeError: If rider counts can't be read.
    """
    try:
        counts = get_rider_counts(sunday_date)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read rider counts for sunday_date={sunday_date!r}: {exc}"
        ) from exc

    shuttle_counts = counts.get(shuttle_id, {"total": 0, "stops": {}})
    shuttle_label = SHUTTLE_NAMES.get(shuttle_id, shuttle_id)

    stop_lines = "\n".join(
        f"- {stop}: {count} riders ({STOP_TIMES.get(stop, 'time TBD')})"
        for stop, count in shuttle_counts["stops"].items()
    )

    body = (
        f"SHUTTLE FULL - ACTION REQUIRED\n\n"
        f"{shuttle_label} has reached capacity for Sunday {sunday_date}.\n\n"
        f"Current count: {shuttle_counts['total']}/14\n\n"
        f"Stop breakdown:\n{stop_lines}\n\n"
        "No more riders can be accepted for this shuttle.\n\n"
        "- CFC Ride Coordination Team"
    )

    try:
        sent = send_email(
            to=settings.OVERSEER_DRIVER_EMAIL,
            subject=f"\U0001f6a8 SHUTTLE FULL - Action Required - Sunday {sunday_date}",
            body=body,
            cc=settings.OVERSEER_RIDE_EMAIL,
            bcc=settings.BCC_EMAIL,
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to send shuttle-full alert email: {exc}") from exc

    if not sent:
        logger.error(
            "send_email returned False for shuttle-full alert (shuttle_id=%r, sunday_date=%r).",
            shuttle_id,
            sunday_date,
        )
        return {"status": "failed"}

    return {"status": "sent"}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _build_wednesday_reminder_body(
    sunday_date: str, assignments: list[dict], routes: list[dict]
) -> str:
    """Build the combined Wednesday reminder email body for all drivers.

    Args:
        sunday_date: The Sunday date this reminder is for, in ISO
            "YYYY-MM-DD" format.
        assignments: This Sunday's assignment dicts (each with
            "driver_name"/"driver_id" and "route_id").
        routes: Route dicts from functions.read_sheets.get_routes(),
            used to look up each shuttle's name, van, and full stop list.

    Returns:
        str: The complete plain-text email body.
    """
    driver_names = [assignment.get("driver_name") or assignment.get("driver_id") for assignment in assignments]
    first_names = [name.split(" ")[0] for name in driver_names]

    scheduled_word = _scheduled_word(len(driver_names))
    scheduled_phrase = f"you are {scheduled_word} scheduled" if scheduled_word else "you are scheduled"

    lines = [
        f"Hi {_join_names(first_names)},",
        "",
        f"This is a reminder that {scheduled_phrase} to drive this Sunday, "
        f"{_format_full_date(sunday_date)} at Covenant Fellowship Church.",
        "",
        "YOUR ASSIGNMENTS:",
        "",
    ]

    for assignment, driver_name in zip(assignments, driver_names):
        route = _find_route(routes, assignment.get("route_id"))

        if route:
            shuttle_label = f"{route.get('shuttle_name', assignment.get('route_id'))} \u2014 {route.get('van', 'van TBD')}"
            stops = route.get("stops", [])
        else:
            shuttle_label = assignment.get("route_name", assignment.get("route_id"))
            stops = []

        lines.append(f"{driver_name} \u2014 {shuttle_label}")
        for stop in stops:
            lines.append(f"- {stop.get('stop_name', 'Unknown stop')}: {stop.get('pickup_time', 'time TBD')}")
        lines.append("")

    lines.append("Please arrive at your first stop a few minutes early.")
    lines.append(f"Service starts at {SERVICE_TIME}.")
    lines.append(f"Church address: {CHURCH_ADDRESS}")
    lines.append("")
    lines.append("---")
    lines.append("CHURCH VAN INSTRUCTIONS")
    lines.append("")
    lines.append("1. Pickup & Key Access")
    lines.append("   - Van location: Parked in the church parking lot")
    lines.append(
        "   - Key lockbox: Located on the wall in the old kitchen "
        "(next to the conference room)"
    )
    lines.append("   - Unlock code: 2906E")
    lines.append("")
    lines.append("2. Return & Drop-Off")
    lines.append("   - Park the van back in the church parking lot")
    lines.append("   - Return the key to the wall lockbox in the old kitchen")
    lines.append("   - Close the box and press E to lock it")
    lines.append("   - Record the number of riders")
    lines.append("")
    lines.append(f"Questions? Contact {OVERSEER_DRIVER_CONTACT_NAME} at {settings.OVERSEER_DRIVER_EMAIL}")
    lines.append("")
    lines.append("See you Sunday!")
    lines.append("CFC Ride Coordination Team")
    lines.append("")
    lines.append("---")
    lines.append("This is an automated reminder sent by the CFC Ride Coordination Agent.")

    return "\n".join(lines)


def _join_names(names: list[str]) -> str:
    """Join a list of names into a natural English list.

    Args:
        names: The names to join, e.g. ["Josiah", "Ryan"].

    Returns:
        str: "Josiah" for one name, "Josiah and Ryan" for two, or
            "Josiah, Ryan, and Peter" for three or more.
    """
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def _scheduled_word(driver_count: int) -> str:
    """Pick the word describing how many drivers are scheduled.

    Args:
        driver_count: Number of drivers in this reminder.

    Returns:
        str: "both" for exactly two drivers, "all" for three or more,
            or "" for a single driver (the caller drops the word
            entirely in that case).
    """
    if driver_count == 2:
        return "both"
    if driver_count > 2:
        return "all"
    return ""


def _find_route(routes: list[dict], shuttle_id: str) -> dict | None:
    """Find a route dict by shuttle_id.

    Args:
        routes: Route dicts from functions.read_sheets.get_routes().
        shuttle_id: The shuttle_id to look up.

    Returns:
        dict or None: The matching route, or None if not found.
    """
    for route in routes:
        if route.get("shuttle_id") == shuttle_id:
            return route
    return None


def _format_short_date(iso_date: str) -> str:
    """Format an ISO date as a short display date, e.g. "Aug 23".

    Args:
        iso_date: A date string in "YYYY-MM-DD" format.

    Returns:
        str: The date formatted as "Mon D" (no zero-padded day).
    """
    parsed = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{parsed.strftime('%b')} {parsed.day}"


def _format_full_date(iso_date: str) -> str:
    """Format an ISO date as a full display date, e.g. "August 23, 2026".

    Args:
        iso_date: A date string in "YYYY-MM-DD" format.

    Returns:
        str: The date formatted as "Month D, YYYY" (no zero-padded day).
    """
    parsed = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def _build_saturday_summary(counts: dict, riders: list[dict], full_shuttles: list[str]) -> str:
    """Format the plain-text Saturday rider-count summary email body.

    Each stop's line shows its rider count, and - if any riders are
    signed up there - a bulleted list of their names underneath.

    Args:
        counts: The dict returned by get_rider_counts().
        riders: The rider dicts returned by get_riders_for_sunday(),
            each with "shuttle_id", "stop", and "name".
        full_shuttles: shuttle_ids that have hit capacity.

    Returns:
        str: The formatted email body.
    """
    names_by_shuttle_stop: dict[str, dict[str, list[str]]] = {}
    for rider in riders:
        stops = names_by_shuttle_stop.setdefault(rider["shuttle_id"], {})
        stops.setdefault(rider["stop"], []).append(rider["name"])

    lines = []
    for shuttle_id, shuttle_label in SHUTTLE_NAMES.items():
        shuttle_counts = counts.get(shuttle_id, {"total": 0, "stops": {}})
        # Only the "Shuttle N" part is uppercased - the van description
        # stays in its normal casing, e.g. "SHUTTLE 1 (Ford Transit - Gray)".
        header_label = shuttle_label.replace("Shuttle", "SHUTTLE", 1)
        lines.append(f"{header_label}: {_rider_count_text(shuttle_counts['total'])}")

        for stop, count in shuttle_counts["stops"].items():
            lines.append(f"- {stop}: {_rider_count_text(count)} ({STOP_TIMES.get(stop, 'time TBD')})")
            for name in names_by_shuttle_stop.get(shuttle_id, {}).get(stop, []):
                lines.append(f"    \u2022 {name}")

        if shuttle_id in full_shuttles:
            lines.append(f"\u26a0\ufe0f SHUTTLE {shuttle_id[-1]} IS FULL (14/14)")
        lines.append("")

    lines.append(f"TOTAL: {_rider_count_text(counts.get('grand_total', 0))}")
    lines.append("Deadline: Saturday 6 PM")
    lines.append("")
    lines.append(f"To view all signups: {RIDER_SIGNUP_SHEET_URL}")
    lines.append("")
    lines.append("---")
    lines.append(
        "This is an automated email sent by the CFC Ride Coordination "
        "Agent. Updates are sent Friday 9:30 PM, Saturday 3:00 PM, "
        "and Saturday 8:00 PM."
    )

    return "\n".join(lines)


def _rider_count_text(count: int) -> str:
    """Format a rider count with correct singular/plural wording.

    Args:
        count: The number of riders.

    Returns:
        str: e.g. "1 rider" or "0 riders"/"2 riders".
    """
    return f"{count} rider" if count == 1 else f"{count} riders"


def _to_sheet_date_format(iso_date: str) -> str:
    """Convert an ISO "YYYY-MM-DD" date into the Sheets tab's "M/D/YY" format.

    Args:
        iso_date: A date string in "YYYY-MM-DD" format.

    Returns:
        str: The same date in "M/D/YY" format, e.g. "2026-08-23" ->
            "8/23/26".

    Raises:
        ValueError: If iso_date isn't valid "YYYY-MM-DD".
    """
    parsed = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{parsed.month}/{parsed.day}/{parsed.strftime('%y')}"


def main() -> None:
    """Test send_saturday_update() locally against the next Sunday's date."""
    from dotenv import load_dotenv

    load_dotenv()

    sunday_date = get_next_sunday_date()
    print(f"Testing send_saturday_update() for sunday_date={sunday_date!r}...")
    result = send_saturday_update(sunday_date)
    print(result)


if __name__ == "__main__":
    main()
