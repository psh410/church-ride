# On-demand route and rider lookups a driver requests by text.
#
# A driver assigned to drive this Sunday texts ROUTE and gets their
# stops, times and live rider counts; RIDERS gets the names; SCHEDULE
# (or DUTY) gets their remaining assigned Sundays for the semester.
# Handled by the /sms-webhook route in cloud_app.py.
#
# Why this exists when the Friday reminder already lists the stops:
# signups keep arriving until Sunday 9am, so Friday's counts are stale
# by the time anyone is actually driving. These replies are built fresh
# on request.
#
# Authorization is free here: the sender's number is matched against the
# driver roster (functions.read_sheets.find_driver_by_phone), and only a
# driver actually assigned that Sunday gets route or rider details. A
# number that isn't a driver at all gets no reply, same as the admin
# keywords.
#
# No opt-out notice on these replies. Drivers already receive the Friday
# reminder carrying it, so they've had the initial message Twilio's
# policy requires, and these are conversational replies to a text they
# sent.

from __future__ import annotations

import logging
from datetime import date, datetime

from db.firestore_client import get_semester_schedule
from functions.read_riders_sheet import get_next_sunday_date, get_riders_for_sunday
from functions.read_sheets import find_driver_by_phone, get_routes
from functions.send_sms import BRAND_PREFIX

logger = logging.getLogger(__name__)

ROUTE_KEYWORDS = {"ROUTE"}
RIDERS_KEYWORDS = {"RIDERS"}
SCHEDULE_KEYWORDS = {"SCHEDULE", "DUTY"}
DRIVER_LOOKUP_KEYWORDS = ROUTE_KEYWORDS | RIDERS_KEYWORDS | SCHEDULE_KEYWORDS


def build_driver_lookup_reply(phone: str, keyword: str, sunday_date: str | None = None) -> str | None:
    """Build the ROUTE or RIDERS reply for whoever texted, if they're a driver.

    Args:
        phone: The sender's number, E.164 preferred.
        keyword: The keyword they texted, already uppercased. Expected to
            be in DRIVER_LOOKUP_KEYWORDS.
        sunday_date: Optional ISO "YYYY-MM-DD" Sunday. Defaults to the
            upcoming Sunday, which is today when called on a Sunday.

    Returns:
        str or None: The SMS body to reply with, or None when the number
            isn't in the driver roster at all - callers should stay
            silent in that case rather than confirming the keyword
            exists.

    Raises:
        RuntimeError: If the schedule, routes or rider signups can't be
            read.
    """
    driver = find_driver_by_phone(phone)
    if driver is None:
        logger.warning(
            "Ignoring %s keyword from %s: not in the driver roster.", keyword, phone
        )
        return None

    driver_name = (driver.get("name") or "").strip()

    # Fetch the Routes tab exactly once per reply and thread it through.
    # Every shuttle-id and stop lookup below needs it, and get_routes()
    # hits the Sheets API fresh each call - SCHEDULE walks every week in
    # the semester, so calling it per week would mean a dozen Sheets
    # round trips inside a webhook Twilio abandons after ~15 seconds.
    routes = get_routes()
    shuttle_ids = _known_shuttle_ids(routes)

    # SCHEDULE answers any roster driver, not just one assigned this
    # Sunday - the whole point is telling someone who isn't driving this
    # week when they next are.
    if keyword in SCHEDULE_KEYWORDS:
        return _build_schedule_reply(driver_name, shuttle_ids)

    if sunday_date is None:
        sunday_date = get_next_sunday_date()
    short_date = _format_short_date(sunday_date)

    entry = _find_schedule_entry(sunday_date)
    if entry is None:
        return f"{BRAND_PREFIX}\nNo driver schedule is set for {short_date} Sun yet."

    assignment = _find_assignment(driver_name, entry, shuttle_ids)

    if assignment is None:
        return (
            f"{BRAND_PREFIX}\nYou're not scheduled to drive {short_date} Sun."
        )

    if assignment["role"] == "backup":
        return (
            f"{BRAND_PREFIX}\n{short_date} Sun: you're backup, no route "
            f"unless S1 or S2 needs coverage."
        )

    shuttle_id = assignment["shuttle_id"]
    leg = assignment["leg"]

    if keyword in RIDERS_KEYWORDS:
        return _build_riders_reply(shuttle_id, leg, sunday_date, short_date, routes)
    return _build_route_reply(shuttle_id, leg, sunday_date, short_date, routes)


# --------------------------------------------------------------------------
# Reply builders
# --------------------------------------------------------------------------
def _build_route_reply(
    shuttle_id: str, leg: str, sunday_date: str, short_date: str, routes: list[dict]
) -> str:
    """Stops, times, and live per-stop counts for one shuttle."""
    route = _find_route(routes, shuttle_id)
    lines = [BRAND_PREFIX, f"Your route {short_date} Sun", _shuttle_line(route, shuttle_id, leg)]

    stops = (route or {}).get("stops", [])
    if not stops:
        lines.append("No stops listed for this shuttle.")
        return "\n".join(lines)

    # The return leg has no set stops and no set rider list: whoever is
    # heading back gets on, and the departure time is announced at
    # church rather than fixed in the Routes tab. So list nothing and
    # say so, rather than repeating the morning pickup stops and times
    # as though they applied.
    if leg == "return":
        lines.append("No set stops or rider list.")
        lines.append("Return time announced at church.")
        return "\n".join(lines)

    counts = _rider_counts_by_stop(shuttle_id, sunday_date)
    for stop in stops:
        name = _stop_name(stop)
        time = _strip_ampm(stop.get("pickup_time"))
        lines.append(f"{name} {time} ({counts.get(name, 0)})".strip())
    lines.append(f"{sum(counts.values())} riders total")
    return "\n".join(lines)


def _build_riders_reply(
    shuttle_id: str, leg: str, sunday_date: str, short_date: str, routes: list[dict]
) -> str:
    """Rider names grouped by stop, as "Jane K", for one shuttle."""
    label = _shuttle_label(shuttle_id)

    if leg == "return":
        return (
            f"{BRAND_PREFIX}\n{short_date} {label} RETURN leg has no set "
            f"rider list."
        )

    riders_by_stop = _riders_by_stop(shuttle_id, sunday_date)
    total = sum(len(names) for names in riders_by_stop.values())
    if total == 0:
        return f"{BRAND_PREFIX}\nRiders {short_date} {label}\nNo riders signed up yet."

    lines = [BRAND_PREFIX, f"Riders {short_date} {label}"]
    route = _find_route(routes, shuttle_id)
    for stop in (route or {}).get("stops", []):
        name = _stop_name(stop)
        names = riders_by_stop.get(name)
        if not names:
            continue
        time = _strip_ampm(stop.get("pickup_time"))
        lines.append(f"{name} {time}: {', '.join(names)}".replace("  ", " "))
    lines.append(f"{total} total")
    return "\n".join(lines)


def _build_schedule_reply(driver_name: str, shuttle_ids: list[str]) -> str:
    """List the driver's remaining assigned Sundays for the semester.

    Dates only, no shuttle or route: a driver checking this wants to
    know which days they're committed to, and can text ROUTE on the day
    for the details. Backup days are tagged, since that's the one thing
    about a date a driver can't work out from the date itself.

    Runs from today forward through the end of whatever the
    semester_schedule holds, with no cap - drivers are typically on no
    more than five Sundays, so the list stays short on its own.
    """
    today = date.today().isoformat()

    rows: list[str] = []
    for entry in sorted(get_semester_schedule(), key=lambda e: e.get("date") or ""):
        entry_date = (entry.get("date") or "").strip()
        if not entry_date or entry_date < today:
            continue

        assignment = _find_assignment(driver_name, entry, shuttle_ids)
        if assignment is None:
            continue

        row = _format_day(entry_date)
        if assignment["role"] == "backup":
            row += " (backup)"
        rows.append(row)

    if not rows:
        return f"{BRAND_PREFIX}\nNo upcoming driving days scheduled for you."

    return "\n".join(
        [BRAND_PREFIX, "Your driving days", *rows, f"{len(rows)} remaining"]
    )


# --------------------------------------------------------------------------
# Schedule and assignment lookup
# --------------------------------------------------------------------------
def _find_schedule_entry(sunday_date: str) -> dict | None:
    """Return the semester_schedule doc for a Sunday, or None."""
    for entry in get_semester_schedule():
        if entry.get("date") == sunday_date:
            return entry
    return None


def _find_assignment(
    driver_name: str, entry: dict, shuttle_ids: list[str]
) -> dict | None:
    """Work out what this driver is doing that Sunday, if anything.

    Returns:
        dict or None: {"role": "driver", "shuttle_id": str, "leg":
            "both"|"pickup"|"return"} when they're driving a shuttle,
            {"role": "backup"} when they're the backup, or None when
            they aren't on the schedule that week.
    """
    for shuttle_id in shuttle_ids:
        base = (entry.get(shuttle_id) or "").strip() or None
        pickup = (entry.get(f"{shuttle_id}_pickup") or base or "").strip() or None
        returning = (entry.get(f"{shuttle_id}_return") or base or "").strip() or None

        drives_pickup = pickup is not None and _names_match(driver_name, pickup)
        drives_return = returning is not None and _names_match(driver_name, returning)

        if drives_pickup and drives_return:
            return {"role": "driver", "shuttle_id": shuttle_id, "leg": "both"}
        if drives_pickup:
            return {"role": "driver", "shuttle_id": shuttle_id, "leg": "pickup"}
        if drives_return:
            return {"role": "driver", "shuttle_id": shuttle_id, "leg": "return"}

    backup = (entry.get("backup") or "").strip()
    if backup and _names_match(driver_name, backup):
        return {"role": "backup"}

    return None


def _names_match(roster_name: str, schedule_name: str) -> bool:
    """Compare a roster name to a schedule name, tolerating first-name entries.

    The semester schedule is maintained by hand and often holds just a
    first name ("Ryan") where the driver form has the full name ("Ryan
    Bielak"), so a first-name match counts. Same approach as
    send_driver_sms_reminder._get_driver_phone, in the other direction.
    """
    left = (roster_name or "").strip().lower()
    right = (schedule_name or "").strip().lower()
    if not left or not right:
        return False
    if left == right:
        return True
    return left.split()[0] == right.split()[0]


# --------------------------------------------------------------------------
# Rider grouping
# --------------------------------------------------------------------------
def _rider_counts_by_stop(shuttle_id: str, sunday_date: str) -> dict[str, int]:
    """Live per-stop rider counts for one shuttle."""
    counts: dict[str, int] = {}
    for rider in get_riders_for_sunday(sunday_date):
        if rider.get("shuttle_id") != shuttle_id:
            continue
        stop = (rider.get("stop") or "").strip()
        counts[stop] = counts.get(stop, 0) + 1
    return counts


def _riders_by_stop(shuttle_id: str, sunday_date: str) -> dict[str, list[str]]:
    """Live per-stop rider names ("Jane K") for one shuttle."""
    grouped: dict[str, list[str]] = {}
    for rider in get_riders_for_sunday(sunday_date):
        if rider.get("shuttle_id") != shuttle_id:
            continue
        stop = (rider.get("stop") or "").strip()
        grouped.setdefault(stop, []).append(_short_name(rider.get("name") or ""))
    return grouped


def _short_name(full_name: str) -> str:
    """Shorten "Jane Kim" to "Jane K", keeping SMS length predictable."""
    parts = full_name.strip().split()
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0]}"


# --------------------------------------------------------------------------
# Route helpers
# --------------------------------------------------------------------------
def _known_shuttle_ids(routes: list[dict]) -> list[str]:
    """Shuttle ids from the Routes tab, in sheet order."""
    return [route.get("shuttle_id") for route in routes if route.get("shuttle_id")]


def _find_route(routes: list[dict], shuttle_id: str) -> dict | None:
    """Return the Routes-tab entry for one shuttle, or None."""
    for route in routes:
        if route.get("shuttle_id") == shuttle_id:
            return route
    return None


def _shuttle_line(route: dict | None, shuttle_id: str, leg: str) -> str:
    """Build the "S1 Gray Van, RETURN leg only" header line."""
    label = _shuttle_label(shuttle_id)
    van = ((route or {}).get("van") or "").strip()
    line = f"{label} {van}".strip()
    if leg == "return":
        line += ", RETURN leg only"
    elif leg == "pickup":
        line += ", PICKUP leg only"
    return line


def _shuttle_label(shuttle_id: str) -> str:
    """Turn "shuttle_1" into "S1", matching the admin summary's shorthand."""
    if shuttle_id.startswith("shuttle_"):
        return f"S{shuttle_id[len('shuttle_'):]}"
    return shuttle_id.replace("_", " ").title()


def _stop_name(stop: dict) -> str:
    return (stop.get("stop_name") or "").strip()


def _strip_ampm(pickup_time: str | None) -> str:
    """Drop AM/PM to keep the message short, as the reminders do."""
    cleaned = (pickup_time or "").replace(" AM", "").replace(" PM", "")
    return cleaned.replace("AM", "").replace("PM", "").strip()


def _format_short_date(iso_date: str) -> str:
    """Convert "2026-09-13" to "9/13/26"."""
    parsed = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{parsed.month}/{parsed.day}/{parsed.strftime('%y')}"


def _format_day(iso_date: str) -> str:
    """Convert "2026-09-13" to "9/13" - the year is noise within a semester."""
    parsed = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{parsed.month}/{parsed.day}"
