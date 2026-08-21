# Handles the three recurring email workflows for the church ride
# coordination system: the Wednesday driver reminder, the Saturday
# rider-count update (sent at noon/6PM/9PM), and the immediate
# "shuttle full" alert.

from __future__ import annotations

import logging
from datetime import datetime

import anthropic

from config import settings
from db.firestore_client import get_assignment
from functions.read_riders_sheet import (
    STOP_TO_SHUTTLE,
    get_next_sunday_date,
    get_rider_counts,
    get_riders_for_sunday,
    is_shuttle_full,
)
from functions.read_sheets import get_all_drivers_with_history, get_routes
from functions.send_email import send_email

logger = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS = 600

CHURCH_ADDRESS = "2906 Crossing Ct, Champaign, IL"
SERVICE_TIME = "9:30 AM"

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

_WEDNESDAY_SYSTEM_PROMPT = (
    "You are drafting a Wednesday reminder email for a church shuttle "
    "driver at Covenant Fellowship Church. Write in a warm, friendly "
    "church community tone. Be concise but include all important "
    "details. Sign off as 'CFC Ride Coordination Team'"
)


def send_wednesday_reminder(sunday_date: str) -> dict:
    """Send each assigned driver a Wednesday reminder about their run.

    Reads this Sunday's assignments and rider counts, looks up each
    driver's email, and uses Claude to draft a warm reminder covering
    their shuttle, stops/times, and expected rider counts per stop.

    Args:
        sunday_date: The Sunday date to send reminders for, in ISO
            "YYYY-MM-DD" format.

    Returns:
        dict: {"sent_count": int, "failures": list[dict]}.

    Raises:
        RuntimeError: If assignments, rider counts, or driver data can't
            be read.
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
        rider_counts = get_rider_counts(sunday_date)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read rider counts for sunday_date={sunday_date!r}: {exc}"
        ) from exc

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

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    sent_count = 0
    failures: list[dict] = []

    for assignment in assignments:
        driver_name = assignment.get("driver_name") or assignment.get("driver_id")
        route_id = assignment.get("route_id")

        try:
            driver = drivers_by_name.get(driver_name)
            if driver is None or not driver.get("email"):
                raise RuntimeError(f"No email on file for driver {driver_name!r}.")

            stop_breakdown = _build_stop_breakdown(route_id, routes, rider_counts)
            shuttle_label = SHUTTLE_NAMES.get(route_id, assignment.get("route_name", route_id))
            shuttle_total = rider_counts.get(route_id, {}).get("total", 0)

            body = _draft_wednesday_reminder(
                client,
                driver_name=driver_name,
                shuttle_label=shuttle_label,
                shuttle_total=shuttle_total,
                stop_breakdown=stop_breakdown,
            )

            sent = send_email(
                to=driver["email"],
                subject=f"CFC Shuttle Reminder - Sunday {sunday_date}",
                body=body,
                cc=settings.OVERSEER_DRIVER_EMAIL,
                bcc=settings.BCC_EMAIL,
            )

            if sent:
                sent_count += 1
            else:
                failures.append({"driver_name": driver_name, "error": "send_email returned False."})

        except Exception as exc:
            logger.error("Failed to send Wednesday reminder to %r: %s", driver_name, exc)
            failures.append({"driver_name": driver_name, "error": str(exc)})

    return {"sent_count": sent_count, "failures": failures}


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
def _build_stop_breakdown(route_id: str, routes: list[dict], rider_counts: dict) -> str:
    """Build a "stop: pickup_time - N riders" text block for one shuttle.

    Args:
        route_id: The shuttle_id to build the breakdown for.
        routes: Route dicts from functions.read_sheets.get_routes().
        rider_counts: The dict returned by get_rider_counts().

    Returns:
        str: One line per stop, e.g. "FAR (9:05 AM): 4 riders".
    """
    stop_names = [stop for stop, shuttle in STOP_TO_SHUTTLE.items() if shuttle == route_id]

    # Prefer the authoritative stop order from the Routes sheet when
    # available; fall back to the hardcoded mapping otherwise.
    for route in routes:
        if route.get("shuttle_id") == route_id and route.get("stops"):
            stop_names = [stop["stop_name"] for stop in route["stops"]]
            break

    stop_counts = rider_counts.get(route_id, {}).get("stops", {})
    lines = [
        f"- {stop} ({STOP_TIMES.get(stop, 'time TBD')}): {stop_counts.get(stop, 0)} riders"
        for stop in stop_names
    ]
    return "\n".join(lines)


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


def _draft_wednesday_reminder(
    client: anthropic.Anthropic,
    driver_name: str,
    shuttle_label: str,
    shuttle_total: int,
    stop_breakdown: str,
) -> str:
    """Use Claude to draft a warm Wednesday reminder email for one driver.

    Args:
        client: A shared anthropic.Anthropic client.
        driver_name: The driver's name.
        shuttle_label: The shuttle's friendly name (with van).
        shuttle_total: Total riders expected on that shuttle.
        stop_breakdown: Pre-formatted "stop (time): N riders" lines.

    Returns:
        str: The drafted email body.

    Raises:
        RuntimeError: If the Claude API call fails.
    """
    user_prompt = f"""Write a Wednesday reminder email for this driver:

- Driver's name: {driver_name}
- Shuttle: {shuttle_label}
- Stops and pickup times, with expected riders per stop:
{stop_breakdown}
- Total riders expected: {shuttle_total}
- Church address: {CHURCH_ADDRESS}
- Service time: {SERVICE_TIME}
- Contact for questions: {settings.OVERSEER_DRIVER_EMAIL}

Include all of the above details clearly."""

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=_WEDNESDAY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text.strip()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to draft Wednesday reminder for driver {driver_name!r}: {exc}"
        ) from exc


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
