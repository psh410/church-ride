# Sends the overseer a Monday update on the shuttle driver schedule
# for the rest of the semester.
#
# Unlike functions/send_weekly_emails.py (which reads live Sheets data
# for one specific Sunday), this reads the whole semester's schedule at
# once from Firestore's "semester_schedule" collection, so it can
# report both last week's drivers and everything still to come. Each
# document has "date" (ISO "YYYY-MM-DD"), "shuttle_1"/"shuttle_2"
# (driver names), "backup" (a third driver who can cover on short
# notice, or None if no one else was available that week), and "past"
# (whether that Sunday has already happened).

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from config import settings
from db.firestore_client import get_semester_schedule
from functions.send_email import send_email

logger = logging.getLogger(__name__)

# Visual divider used around each major section of the schedule email.
_SECTION_DIVIDER = "=" * 40


def send_monday_schedule() -> dict:
    """Send the overseer this week's shuttle driver schedule update.

    Reports last week's drivers (the most recent past Sunday in the
    semester schedule) plus every remaining Sunday's assigned drivers
    for the rest of the semester. No Claude drafting is used - the
    schedule is read live from Firestore's "semester_schedule"
    collection and the email body is built directly from it.

    Returns:
        dict: {"status": "sent" or "failed"}.
    """
    try:
        schedule = get_semester_schedule()
    except Exception as exc:
        logger.error("Failed to read semester schedule: %s", exc)
        return {"status": "failed"}

    last_week = _find_last_week(schedule)
    if last_week is None:
        logger.error("No past Sunday found in semester schedule; nothing to report.")
        return {"status": "failed"}

    remaining = [entry for entry in schedule if entry["date"] > last_week["date"]]

    body = _build_schedule_body(last_week, remaining, schedule)
    monday_date = _get_this_monday()

    try:
        sent = send_email(
            to=settings.OVERSEER_DRIVER_EMAIL,
            subject=f"CFC Shuttle Driver Schedule - Week of {_format_short_date(monday_date)}",
            body=body,
            bcc=settings.BCC_EMAIL,
        )
    except Exception as exc:
        logger.error("Failed to send Monday schedule email: %s", exc)
        return {"status": "failed"}

    if not sent:
        logger.error("send_email returned False for Monday schedule update.")
        return {"status": "failed"}

    return {"status": "sent"}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _find_last_week(schedule: list[dict]) -> dict | None:
    """Find the SEMESTER_SCHEDULE entry representing "last week".

    Prefers whichever entry is explicitly flagged "past": True (there
    should normally be exactly one, updated by hand as Sundays pass -
    if more than one is flagged, the latest of those is used). Falls
    back to the most recent entry whose date is on or before today, in
    case the "past" flags haven't been kept up to date.

    Args:
        schedule: The full semester schedule, from
            db.firestore_client.get_semester_schedule().

    Returns:
        dict or None: The "last week" entry, or None if schedule is
            empty and no entry qualifies as having already happened.
    """
    flagged = [entry for entry in schedule if entry.get("past")]
    if flagged:
        return max(flagged, key=lambda entry: entry["date"])

    today_iso = date.today().isoformat()
    past_entries = [entry for entry in schedule if entry["date"] <= today_iso]
    if past_entries:
        return max(past_entries, key=lambda entry: entry["date"])

    return None


def _build_schedule_body(last_week: dict, remaining: list[dict], schedule: list[dict]) -> str:
    """Format the plain-text Monday schedule update email body.

    Args:
        last_week: The semester schedule entry for the most recent past
            Sunday.
        remaining: Semester schedule entries for every Sunday after
            last_week, in schedule order.
        schedule: The full semester schedule (past and upcoming), used
            to calculate the semester-wide driver totals section.

    Returns:
        str: The complete plain-text email body.
    """
    lines = []
    lines.append("Hi Dae,")
    lines.append("")
    lines.append("This is the shuttle driver schedule for the rest of the Fall 2026 semester.")
    lines.append("")
    lines.append("\U0001f4cb HOW THIS SCHEDULE WAS BUILT:")
    lines.append("Each week's drivers were chosen based on:")
    lines.append("  \u2022 Availability (drivers who marked that Sunday as free)")
    lines.append(
        "  \u2022 Fairness (spreading drives evenly across everyone, "
        "roughly 4-5 times each this semester)"
    )
    lines.append(
        "  \u2022 No back-to-back weeks for the same driver when another "
        "option was available"
    )
    lines.append(
        "  \u2022 Younger drivers (Josiah, Ryan, Sangwoo) on the newer "
        "Shuttle 1 (Gray Van); older drivers on Shuttle 2 (Silver Van)"
    )
    lines.append(
        "  \u2022 A backup driver is listed each week in case the primary "
        "driver needs to swap last minute"
    )
    lines.append(
        "  \u2022 Peter Hahn has more scheduling conflicts than others "
        "this semester, so his total is slightly lower"
    )
    lines.append("")

    lines.append(_SECTION_DIVIDER)
    lines.append(f"\u2705 LAST WEEK \u2014 {_format_short_date(last_week['date'])}")
    lines.append(_SECTION_DIVIDER)
    lines.append(f"\U0001f690 Shuttle 1 (Gray Van): {last_week['shuttle_1']}")
    lines.append(f"\U0001f690 Shuttle 2 (Silver Van): {last_week['shuttle_2']}")
    lines.append("")

    lines.append(_SECTION_DIVIDER)
    lines.append("\U0001f4c5 UPCOMING SCHEDULE")
    lines.append(_SECTION_DIVIDER)
    lines.append("")

    for entry in remaining:
        backup = entry.get("backup")
        lines.append(_format_short_date(entry["date"]))
        lines.append(f"  \U0001f690 Shuttle 1 (Gray Van): {_format_shuttle_driver_text(entry, 'shuttle_1')}")
        lines.append(f"  \U0001f690 Shuttle 2 (Silver Van): {_format_shuttle_driver_text(entry, 'shuttle_2')}")
        lines.append(f"  \U0001f504 Backup: {backup if backup else 'No backup this week'}")
        lines.append("")

    lines.append(_SECTION_DIVIDER)
    lines.append("\u26a0\ufe0f No shuttle service on Nov 22 or Nov 29.")
    lines.append(_SECTION_DIVIDER)
    lines.append("")

    lines.append(_SECTION_DIVIDER)
    lines.append("\U0001f4ca DRIVER TOTALS FOR THE SEMESTER")
    lines.append(_SECTION_DIVIDER)
    for name, count in _calculate_driver_totals(schedule):
        lines.append(f"  {name}: {count} time{'s' if count != 1 else ''}")
    lines.append("")

    lines.append(
        "Reminder emails will automatically go out to drivers each Wednesday, "
        "and the final rider list will be sent to drivers each Sunday morning "
        "at 7:00 AM."
    )
    lines.append("")
    lines.append("CFC Ride Coordination Team")
    lines.append("")
    lines.append("---")
    lines.append("This is an automated email sent by the CFC Ride Coordination Agent.")

    return "\n".join(lines)


def _format_shuttle_driver_text(entry: dict, shuttle_id: str) -> str:
    """Format a shuttle's driver name(s) for one schedule entry line.

    Shows a single name for a normal week, or "Pickup: X, Return: Y"
    when that shuttle has a split shift (different drivers covering
    the pickup vs. return leg) that week. Falls back to the plain
    "shuttle_N" value if the "_pickup"/"_return" fields aren't present
    (e.g. before scripts/update_split_shifts.py has run).

    Args:
        entry: One semester schedule document (see
            db.firestore_client.get_semester_schedule()).
        shuttle_id: Which shuttle to format, "shuttle_1" or
            "shuttle_2".

    Returns:
        str: e.g. "Sangwoo Suk" or "Pickup: Ryan Bielak, Return: Sangwoo Suk".
    """
    base_name = entry.get(shuttle_id)
    pickup_driver = entry.get(f"{shuttle_id}_pickup") or base_name
    return_driver = entry.get(f"{shuttle_id}_return") or base_name

    if pickup_driver and return_driver and pickup_driver != return_driver:
        return f"Pickup: {pickup_driver}, Return: {return_driver}"
    return pickup_driver or return_driver or "TBD"


def _calculate_driver_totals(schedule: list[dict]) -> list[tuple[str, int]]:
    """Count how many times each driver is scheduled to drive this semester.

    Only counts actual driving assignments ("shuttle_1"/"shuttle_2") -
    being listed as a week's backup doesn't count toward these totals.

    Args:
        schedule: The full semester schedule, from
            db.firestore_client.get_semester_schedule(), including past
            weeks, so totals reflect the whole semester.

    Returns:
        list[tuple[str, int]]: (driver_name, count) pairs, sorted by
            count descending, then alphabetically by name for ties.
    """
    totals: dict[str, int] = {}
    for entry in schedule:
        for key in ("shuttle_1", "shuttle_2"):
            name = entry.get(key)
            if name:
                totals[name] = totals.get(name, 0) + 1

    return sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))


def _get_this_monday() -> str:
    """Return the current week's Monday date.

    Returns:
        str: The date of the Monday on or before today, in ISO
            "YYYY-MM-DD" format.
    """
    today = date.today()
    days_since_monday = today.weekday()  # Monday=0 ... Sunday=6
    monday = today - timedelta(days=days_since_monday)
    return monday.strftime("%Y-%m-%d")


def _format_short_date(iso_date: str) -> str:
    """Format an ISO date as a short display date, e.g. "Aug 23".

    Args:
        iso_date: A date string in "YYYY-MM-DD" format.

    Returns:
        str: The date formatted as "Mon D" (no zero-padded day).
    """
    parsed = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{parsed.strftime('%b')} {parsed.day}"


def main() -> None:
    """Test send_monday_schedule() locally."""
    from dotenv import load_dotenv

    load_dotenv()

    print("Testing send_monday_schedule()...")
    result = send_monday_schedule()
    print(result)


if __name__ == "__main__":
    main()
