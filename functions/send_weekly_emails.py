# Handles the three recurring email workflows for the church ride
# coordination system: the Wednesday driver reminder, the Saturday
# rider-count update (sent at noon/6PM/9PM), and the immediate
# "shuttle full" alert.

from __future__ import annotations

import logging
from datetime import datetime, time

from config import settings
from db.firestore_client import get_assignment
from functions.read_riders_sheet import (
    MAX_RIDERS_PER_SHUTTLE,
    get_all_riders_for_sunday,
    get_next_sunday_date,
    get_rider_counts,
    get_stop_times_map,
    get_stop_to_shuttle_map,
)
from functions.read_sheets import get_all_drivers_with_history, get_routes
from functions.send_email import send_email

logger = logging.getLogger(__name__)

CHURCH_ADDRESS = "2906 Crossing Ct, Champaign, IL"
SERVICE_TIME = "9:30 AM"
OVERSEER_DRIVER_CONTACT_NAME = "Dae Kang"
OVERSEER_RIDE_CONTACT_NAME = "Sarah Choi"

# Visual divider used around each driver's assignment block in the
# Wednesday reminder email.
_SECTION_DIVIDER = "=" * 40

# Shuttle names, vans, stop names, pickup times, and return departure
# times are NOT hardcoded here - they're all read live from the
# "Routes" tab via get_routes() (each route's own "departure_time"
# field) and functions.read_riders_sheet.get_stop_times_map()/
# get_stop_to_shuttle_map() inside each function below, so route changes
# in Sheets are picked up automatically without a code change.

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
    Reports both shuttle riders (with a per-stop breakdown and rider
    names) and non-shuttle riders (who need a personal driver
    coordinated separately, outside the shuttle system).

    Args:
        sunday_date: The Sunday date to report counts for, in ISO
            "YYYY-MM-DD" format.

    Returns:
        dict: {"status": "sent" or "failed"}.

    Raises:
        RuntimeError: If the rider list can't be read.
    """
    try:
        all_riders = get_all_riders_for_sunday(sunday_date)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read riders for sunday_date={sunday_date!r}: {exc}"
        ) from exc

    try:
        routes = get_routes()
    except Exception as exc:
        raise RuntimeError(f"Failed to read routes: {exc}") from exc

    try:
        stop_times_map = get_stop_times_map()
    except Exception as exc:
        raise RuntimeError(f"Failed to read stop pickup times: {exc}") from exc

    shuttle_riders = all_riders["shuttle_riders"]
    non_shuttle_riders = all_riders["non_shuttle_riders"]

    # Build the per-shuttle/per-stop breakdown locally from the rider
    # list we already have, rather than making a second Sheets read via
    # get_rider_counts()/is_shuttle_full().
    counts = _build_shuttle_counts(shuttle_riders)
    over_capacity_shuttles = [
        shuttle_id
        for shuttle_id, shuttle_counts in counts.items()
        if shuttle_counts["total"] > MAX_RIDERS_PER_SHUTTLE
    ]

    body = _build_saturday_summary(
        all_riders, counts, non_shuttle_riders, over_capacity_shuttles, routes, stop_times_map
    )

    try:
        sent = send_email(
            to=settings.OVERSEER_DRIVER_EMAIL,
            subject=f"CFC Shuttle Update - Sunday {sunday_date}",
            body=body,
            cc=f"{settings.OVERSEER_RIDE_EMAIL}, {settings.OVERSEER_RIDE_EMAIL_2}",
            bcc=settings.BCC_EMAIL,
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to send Saturday update email: {exc}") from exc

    if not sent:
        logger.error("send_email returned False for Saturday update (sunday_date=%r).", sunday_date)
        return {"status": "failed"}

    return {"status": "sent"}


def send_saturday_driver_assignment(sunday_date: str) -> dict:
    """Send drivers their current, confirmed rider list at 9:30 PM Saturday.

    Unlike send_wednesday_reminder() (sent days before Sunday), this
    goes out once most signups are in and lists every confirmed shuttle
    rider under their stop, so drivers know who to expect Sunday
    morning. Late additions after this email are still possible, in
    which case an updated list is sent. No Claude drafting is used -
    the body is built directly from static text plus Sheets/Firestore
    data.

    Args:
        sunday_date: The Sunday date to send the final rider list for,
            in ISO "YYYY-MM-DD" format.

    Returns:
        dict: {"sent_count": int, "failures": list[dict]}. sent_count is
            1 if the combined email was sent successfully, 0 otherwise.

    Raises:
        RuntimeError: If riders, assignments, or driver data can't be
            read.
    """
    try:
        all_riders = get_all_riders_for_sunday(sunday_date)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read riders for sunday_date={sunday_date!r}: {exc}"
        ) from exc

    try:
        assignments = get_assignment(sunday_date)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read assignments for sunday_date={sunday_date!r}: {exc}"
        ) from exc

    if not assignments:
        logger.info("No assignments found for sunday_date=%r; nothing to send.", sunday_date)
        return {"sent_count": 0, "failures": []}

    try:
        routes = get_routes()
    except Exception as exc:
        raise RuntimeError(f"Failed to read routes: {exc}") from exc

    try:
        stop_times_map = get_stop_times_map()
    except Exception as exc:
        raise RuntimeError(f"Failed to read stop pickup times: {exc}") from exc

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
            logger.error("No email on file for driver %r; excluding from final list.", driver_name)
            failures.append({"driver_name": driver_name, "error": "No email on file."})
            continue
        driver_emails.append(driver["email"])

    if not driver_emails:
        return {"sent_count": 0, "failures": failures}

    body = _build_saturday_driver_assignment_body(
        sunday_date, assignments, all_riders, routes, stop_times_map
    )

    try:
        sent = send_email(
            to=", ".join(driver_emails),
            subject=f"CFC Sunday Shuttle - Final Rider List - {_format_short_date(sunday_date)}",
            body=body,
            cc=f"{settings.OVERSEER_DRIVER_EMAIL}, {settings.OVERSEER_RIDE_EMAIL}, {settings.OVERSEER_RIDE_EMAIL_2}",
            bcc=settings.BCC_EMAIL,
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to send final rider list email: {exc}") from exc

    if not sent:
        failures.append({"driver_name": "all", "error": "send_email returned False."})
        return {"sent_count": 0, "failures": failures}

    return {"sent_count": 1, "failures": failures}


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

    try:
        routes = get_routes()
    except Exception as exc:
        raise RuntimeError(f"Failed to read routes: {exc}") from exc

    try:
        stop_times_map = get_stop_times_map()
    except Exception as exc:
        raise RuntimeError(f"Failed to read stop pickup times: {exc}") from exc

    shuttle_counts = counts.get(shuttle_id, {"total": 0, "stops": {}})
    route = _find_route(routes, shuttle_id)
    if route:
        shuttle_label = f"{route.get('shuttle_name', shuttle_id)} ({route.get('van', 'van TBD')})"
    else:
        shuttle_label = shuttle_id

    stop_lines = "\n".join(
        f"- {stop}: {count} riders ({stop_times_map.get(stop, 'time TBD')})"
        for stop, count in sorted(
            shuttle_counts["stops"].items(),
            key=lambda item: _stop_time_sort_key(item[0], stop_times_map),
        )
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
            cc=f"{settings.OVERSEER_RIDE_EMAIL}, {settings.OVERSEER_RIDE_EMAIL_2}",
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
            used to look up each shuttle's name, van, full stop list,
            and return departure_time.

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
    ]

    for assignment, driver_name in zip(assignments, driver_names):
        route = _find_route(routes, assignment.get("route_id"))

        if route:
            shuttle_name = route.get("shuttle_name", assignment.get("route_id"))
            van = route.get("van", "van TBD")
            stops = route.get("stops", [])
            return_time = route.get("departure_time") or "time TBD"
        else:
            shuttle_name = assignment.get("route_name", assignment.get("route_id"))
            van = "van TBD"
            stops = []
            return_time = "time TBD"

        # Only the "Shuttle N" part is uppercased - the van description
        # stays in its normal casing, e.g. "SHUTTLE 1 — Ford Transit (Gray)".
        shuttle_header = f"{shuttle_name.replace('Shuttle', 'SHUTTLE', 1)} \u2014 {van}"

        lines.append(_SECTION_DIVIDER)
        lines.append(shuttle_header)
        lines.append(f"Driver: {driver_name}")
        lines.append(_SECTION_DIVIDER)
        lines.append("Pickup Stops:")
        for stop in stops:
            lines.append(
                f"  \U0001f4cd {stop.get('stop_name', 'Unknown stop')}: "
                f"{stop.get('pickup_time', 'time TBD')}"
            )
        lines.append("")
        lines.append(f"\U0001f504 Return departure from church: {return_time}")
        lines.append(_SECTION_DIVIDER)
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


def _build_saturday_driver_assignment_body(
    sunday_date: str,
    assignments: list[dict],
    all_riders: dict,
    routes: list[dict],
    stop_times_map: dict,
) -> str:
    """Build the final confirmed-rider-list email body for all drivers.

    Args:
        sunday_date: The Sunday date this list is for, in ISO
            "YYYY-MM-DD" format.
        assignments: This Sunday's assignment dicts (each with
            "driver_name"/"driver_id" and "route_id").
        all_riders: The dict returned by get_all_riders_for_sunday(),
            used here for its "shuttle_riders" list.
        routes: Route dicts from functions.read_sheets.get_routes(),
            used to build each shuttle's name, van, full stop list, and
            return departure_time live instead of from a hardcoded
            lookup.
        stop_times_map: {stop_name: pickup_time} from
            functions.read_riders_sheet.get_stop_times_map(), used to
            sort each shuttle's stops in pickup-time order.

    Returns:
        str: The complete plain-text email body.
    """
    driver_names = [assignment.get("driver_name") or assignment.get("driver_id") for assignment in assignments]
    first_names = [name.split(" ")[0] for name in driver_names]

    # Group confirmed rider names by shuttle, then stop.
    names_by_shuttle_stop: dict[str, dict[str, list[str]]] = {}
    for rider in all_riders["shuttle_riders"]:
        stops = names_by_shuttle_stop.setdefault(rider["shuttle_id"], {})
        stops.setdefault(rider["stop"], []).append(rider["name"])

    lines = []
    lines.append(f"Hi {_join_names(first_names)},")
    lines.append("")
    lines.append(
        f"Here is your current rider list for Sunday, {_format_full_date(sunday_date)}. "
        "You will receive an updated list if there are any changes."
    )
    lines.append("")

    for assignment, driver_name in zip(assignments, driver_names):
        shuttle_id = assignment.get("route_id")
        route = _find_route(routes, shuttle_id)

        if route:
            van = route.get("van", "van TBD")
            return_time = route.get("departure_time") or "time TBD"
            # Every stop this shuttle serves, regardless of whether
            # anyone signed up for it this week - straight from Sheets.
            stops = sorted(
                route.get("stops", []),
                key=lambda stop: _stop_time_sort_key(stop.get("stop_name"), stop_times_map),
            )
        else:
            van = "van TBD"
            return_time = "time TBD"
            stops = []

        shuttle_number = shuttle_id[-1] if shuttle_id else "?"
        total_riders = sum(len(names) for names in names_by_shuttle_stop.get(shuttle_id, {}).values())

        lines.append(_SECTION_DIVIDER)
        lines.append(f"SHUTTLE {shuttle_number} \u2014 {van}")
        lines.append(f"Driver: {driver_name}")
        lines.append(f"Total riders: {total_riders}")
        lines.append(_SECTION_DIVIDER)
        lines.append("Pickup Stops:")
        lines.append("")

        for stop in stops:
            stop_name = stop.get("stop_name", "Unknown stop")
            pickup_time = stop.get("pickup_time") or stop_times_map.get(stop_name, "time TBD")
            lines.append(f"  \U0001f4cd {stop_name}: {pickup_time}")
            names = names_by_shuttle_stop.get(shuttle_id, {}).get(stop_name, [])
            if names:
                for name in names:
                    lines.append(f"      \u2022 {name}")
            else:
                lines.append("      No riders")
            lines.append("")

        lines.append(f"\U0001f504 Return departure from church: {return_time}")
        lines.append(_SECTION_DIVIDER)
        lines.append("")

    lines.append(f"Questions about driving? Contact {OVERSEER_DRIVER_CONTACT_NAME} at {settings.OVERSEER_DRIVER_EMAIL}")
    lines.append(f"Questions about riders? Contact {OVERSEER_RIDE_CONTACT_NAME} at {settings.OVERSEER_RIDE_EMAIL}")
    lines.append("")
    lines.append("See you Sunday!")
    lines.append("CFC Ride Coordination Team")
    lines.append("")
    lines.append("---")
    lines.append("This is an automated email sent by the CFC Ride Coordination Agent.")

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


def _build_shuttle_counts(shuttle_riders: list[dict]) -> dict:
    """Group shuttle riders into per-shuttle/per-stop counts.

    Mirrors the shape returned by
    functions.read_riders_sheet.get_rider_counts(), but is computed
    locally from an already-fetched rider list instead of making a
    second Sheets read.

    Args:
        shuttle_riders: Riders with a real shuttle_id (the
            "shuttle_riders" list from get_all_riders_for_sunday()).

    Returns:
        dict: One key per shuttle_id, each mapping to
            {"total": int, "stops": {stop_name: int, ...}} (every stop
            that shuttle serves is included, even with a count of 0).
    """
    stops_by_shuttle: dict[str, list[str]] = {}
    for stop, shuttle_id in get_stop_to_shuttle_map().items():
        stops_by_shuttle.setdefault(shuttle_id, []).append(stop)

    counts: dict = {
        shuttle_id: {"total": 0, "stops": {stop: 0 for stop in stops}}
        for shuttle_id, stops in stops_by_shuttle.items()
    }

    for rider in shuttle_riders:
        shuttle_id = rider["shuttle_id"]
        stop = rider["stop"]
        counts[shuttle_id]["total"] += 1
        counts[shuttle_id]["stops"][stop] = counts[shuttle_id]["stops"].get(stop, 0) + 1

    return counts


def _build_saturday_summary(
    all_riders: dict,
    counts: dict,
    non_shuttle_riders: list[dict],
    over_capacity_shuttles: list[str],
    routes: list[dict],
    stop_times_map: dict,
) -> str:
    """Format the plain-text Saturday rider-count summary email body.

    Leads with a total-requests header (shuttle vs. non-shuttle), then
    the usual shuttle breakdown (counts and rider names per stop), then
    - if there are any - a list of non-shuttle riders who still need a
    personal driver coordinated.

    Args:
        all_riders: The dict returned by get_all_riders_for_sunday(),
            used here for "total", "shuttle_total", and
            "non_shuttle_total".
        counts: The dict returned by _build_shuttle_counts().
        non_shuttle_riders: Riders with shuttle_id None, each with
            "name" and "stop" (their typed Campus Address).
        over_capacity_shuttles: shuttle_ids with more than
            MAX_RIDERS_PER_SHUTTLE riders signed up. No separate alert
            email is sent for these anymore - a note is added inline
            under that shuttle's total instead, so the coordinator can
            act without a hard stop on signups.
        routes: Route dicts from functions.read_sheets.get_routes(),
            used to build each shuttle's display header live instead of
            from a hardcoded lookup.
        stop_times_map: {stop_name: pickup_time} from
            functions.read_riders_sheet.get_stop_times_map(), used to
            sort each shuttle's stops in pickup-time order.

    Returns:
        str: The formatted email body.
    """
    # counts only holds numbers, so names are pulled separately from the
    # underlying rider list and grouped the same way (by shuttle, then stop).
    names_by_shuttle_stop: dict[str, dict[str, list[str]]] = {}
    for rider in all_riders["shuttle_riders"]:
        stops = names_by_shuttle_stop.setdefault(rider["shuttle_id"], {})
        stops.setdefault(rider["stop"], []).append(rider["name"])

    lines = [
        f"Total ride requests this week: {all_riders['total']}",
        f"- Shuttle stops: {all_riders['shuttle_total']}",
        f"- Non-shuttle (need personal driver): {all_riders['non_shuttle_total']}",
        "",
    ]

    for route in routes:
        shuttle_id = route.get("shuttle_id")
        shuttle_name = route.get("shuttle_name", shuttle_id)
        van = route.get("van", "van TBD")
        # Only the "Shuttle N" part is uppercased - the van description
        # stays in its normal casing, e.g. "SHUTTLE 1 (Ford Transit (Gray))".
        header_label = f"{shuttle_name.replace('Shuttle', 'SHUTTLE', 1)} ({van})"

        shuttle_counts = counts.get(shuttle_id, {"total": 0, "stops": {}})
        lines.append(f"{header_label}: {_rider_count_text(shuttle_counts['total'])}")

        if shuttle_id in over_capacity_shuttles:
            lines.append(
                f"\u26a0\ufe0f NOTE: This shuttle is over capacity "
                f"(max {MAX_RIDERS_PER_SHUTTLE} riders)."
            )
            lines.append("Coordinator action may be needed.")

        for stop in sorted(
            shuttle_counts["stops"], key=lambda s: _stop_time_sort_key(s, stop_times_map)
        ):
            count = shuttle_counts["stops"][stop]
            lines.append(f"- {stop}: {_rider_count_text(count)} ({stop_times_map.get(stop, 'time TBD')})")
            for name in names_by_shuttle_stop.get(shuttle_id, {}).get(stop, []):
                lines.append(f"    \u2022 {name}")

        lines.append("")

    lines.append(f"TOTAL: {_rider_count_text(all_riders['shuttle_total'])}")

    if non_shuttle_riders:
        lines.append("")
        lines.append("NON-SHUTTLE RIDERS (need personal driver coordination):")
        for rider in non_shuttle_riders:
            lines.append(f"  \u2022 {rider['name']} - {rider['stop']}")

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


def _stop_time_sort_key(stop: str, stop_times_map: dict) -> time:
    """Convert a stop's pickup time into a sortable value.

    Args:
        stop: The stop name to look up, e.g. "FAR".
        stop_times_map: {stop_name: pickup_time} built live from the
            Routes sheet (see
            functions.read_riders_sheet.get_stop_times_map()).

    Returns:
        time: The parsed pickup time, used as a sort key so stops
            display in pickup-time order rather than alphabetically.
            Stops missing from stop_times_map sort last.
    """
    pickup_time = stop_times_map.get(stop)
    if pickup_time is None:
        return time.max
    return datetime.strptime(pickup_time, "%I:%M %p").time()


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
