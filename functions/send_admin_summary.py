# Builds the on-demand ride summary that admins request by texting
# "UPDATE" to the church Twilio number (handled by the /sms-webhook
# route in cloud_app.py).
#
# This is pull, not push: nothing goes out on a schedule. An admin whose
# phone is listed in settings.ADMIN_SMS_PHONES texts the keyword and
# gets the current counts back as a reply. Counts come from a single
# read of the rider sheet, grouped in Python, because this runs inside a
# Twilio webhook request - Twilio gives up on us after about 15 seconds,
# and a Cloud Run cold start plus two separate sheet reads would cut
# that close. That's also why this module does its own grouping instead
# of calling both get_all_riders_for_sunday() and get_rider_counts(),
# which would each re-read the same tab.

from __future__ import annotations

import logging
from datetime import datetime

from config import settings
from db.firestore_client import get_semester_schedule
from functions.read_riders_sheet import (
    get_next_sunday_date,
    get_riders_for_sunday,
    get_stop_to_shuttle_map,
)
from functions.send_sms import BRAND_PREFIX, OPT_OUT_NOTICE, normalize_to_e164

logger = logging.getLogger(__name__)

# Keywords an admin can text to request the summary. Deliberately away
# from anything Twilio reserves (STOP/STOPALL/UNSUBSCRIBE/CANCEL/END/
# QUIT/START/YES/HELP/INFO) - Twilio answers those itself, so they'd
# never behave like a normal keyword.
ADMIN_SUMMARY_KEYWORDS = {"UPDATE", "STATUS"}


def is_admin_phone(phone: str) -> bool:
    """Return whether a phone number is allowed to request the summary.

    Compares against settings.ADMIN_SMS_PHONES after normalizing both
    sides to E.164, so however the numbers are formatted in .env
    ("+17035550100", "703-555-0100", "(703) 555-0100") they still match.

    Args:
        phone: The sender's phone number, in any format
            normalize_to_e164() accepts.

    Returns:
        bool: True if this number is on the admin allowlist, False if
            it isn't (or doesn't normalize to a valid US number).
    """
    try:
        target = normalize_to_e164(phone)
    except ValueError:
        return False

    for allowed in settings.ADMIN_SMS_PHONES:
        try:
            if normalize_to_e164(allowed) == target:
                return True
        except ValueError:
            logger.warning(
                "Ignoring unparseable entry in ADMIN_SMS_PHONES: %r", allowed
            )
            continue

    return False


def build_admin_summary(sunday_date: str | None = None) -> str:
    """Build the ride summary text for a Sunday.

    Produces something like:

        CFC Rides: For Sunday service 9/13/26, 50 riders requested.
        Shuttle 1 (Sangwoo): 10
        Shuttle 2 (Peter): 8
        Backup driver: Youngwook
        Non-shuttle requests: 32
        Reply HELP for help, STOP to opt out.

    Shuttles are listed dynamically from the Routes tab, so adding a
    third shuttle makes it appear here without a code change. Rider
    totals are post-deduplication (one signup per person).

    Args:
        sunday_date: The Sunday to summarize, in ISO "YYYY-MM-DD"
            format. Defaults to the upcoming Sunday, which is today's
            date when called on a Sunday.

    Returns:
        str: The multi-line summary text, ready to send as an SMS body.

    Raises:
        RuntimeError: If the rider signups can't be read. Driver names
            are best-effort - a Firestore failure downgrades them to
            "unassigned" rather than failing the whole summary, since
            the counts are the point.
    """
    if sunday_date is None:
        sunday_date = get_next_sunday_date()

    riders = get_riders_for_sunday(sunday_date, include_non_shuttle=True)

    # Start every known shuttle at 0 so a shuttle with no signups still
    # shows up in the message rather than silently vanishing.
    shuttle_totals: dict[str, int] = {
        shuttle_id: 0 for shuttle_id in sorted(set(get_stop_to_shuttle_map().values()))
    }

    non_shuttle_total = 0
    for rider in riders:
        shuttle_id = rider.get("shuttle_id")
        if shuttle_id is None:
            non_shuttle_total += 1
            continue
        shuttle_totals[shuttle_id] = shuttle_totals.get(shuttle_id, 0) + 1

    entry = _get_schedule_entry(sunday_date)

    lines = [
        f"{BRAND_PREFIX} For Sunday service {_format_short_date(sunday_date)}, "
        f"{len(riders)} riders requested."
    ]
    for shuttle_id, total in shuttle_totals.items():
        driver = _driver_label(entry, shuttle_id)
        lines.append(f"{_shuttle_label(shuttle_id)} ({driver}): {total}")
    lines.append(f"Backup driver: {_backup_label(entry)}")
    lines.append(f"Non-shuttle requests: {non_shuttle_total}")
    lines.append(OPT_OUT_NOTICE)

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------
def _get_schedule_entry(sunday_date: str) -> dict | None:
    """Return the semester_schedule doc for a Sunday, or None.

    Best-effort on purpose: if Firestore is unreachable we still want to
    send the rider counts, just without driver names.
    """
    try:
        schedule = get_semester_schedule()
    except Exception as exc:
        logger.warning(
            "Could not read semester schedule for %s (%s); "
            "summary will omit driver names.",
            sunday_date,
            exc,
        )
        return None

    for entry in schedule:
        if entry.get("date") == sunday_date:
            return entry

    logger.info("No semester schedule entry for sunday=%s.", sunday_date)
    return None


def _driver_label(entry: dict | None, shuttle_id: str) -> str:
    """Return the driver name to show for one shuttle.

    Handles split weeks, where a shuttle has one driver for pickup and
    another for the return leg, by showing "Pickup/Return" first names.
    """
    if entry is None:
        return "unassigned"

    base = (entry.get(shuttle_id) or "").strip() or None
    pickup = (entry.get(f"{shuttle_id}_pickup") or base or "").strip() or None
    returning = (entry.get(f"{shuttle_id}_return") or base or "").strip() or None

    if pickup and returning and pickup.lower() != returning.lower():
        return f"{_first_name(pickup)}/{_first_name(returning)}"
    if pickup:
        return _first_name(pickup)
    if returning:
        return _first_name(returning)

    return "unassigned"


def _backup_label(entry: dict | None) -> str:
    """Return the backup driver's first name, or "none" if there isn't one."""
    if entry is None:
        return "none"

    backup = (entry.get("backup") or "").strip()
    return _first_name(backup) if backup else "none"


def _shuttle_label(shuttle_id: str) -> str:
    """Turn a shuttle_id like "shuttle_1" into a label like "Shuttle 1"."""
    return shuttle_id.replace("_", " ").title()


def _first_name(name: str) -> str:
    """Return the first word of a name, to keep the SMS short."""
    parts = name.strip().split()
    return parts[0] if parts else ""


def _format_short_date(iso_date: str) -> str:
    """Convert "2026-09-13" to "9/13/26"."""
    parsed = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{parsed.month}/{parsed.day}/{parsed.strftime('%y')}"
