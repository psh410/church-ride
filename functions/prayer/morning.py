# Reads the Morning Prayer weekly schedule from Google Sheets
# (devotional rotation + worship-leader directory) and sends a
# plain-text reminder to the coordinators via the same Gmail
# domain-wide delegation path as functions/prayer/thursday.py.

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from config import settings
from functions.read_sheets import get_sheet_client
from functions.send_email import send_email

logger = logging.getLogger(__name__)

CHICAGO = ZoneInfo("America/Chicago")

# Verified spreadsheet IDs (the public URLs sometimes get a character
# mistyped; these are the IDs the service account can actually read).
DEVOTIONAL_SHEET_ID = "1LSArFj5mf8_eMKFITAs3rMmqV7-P9AofuHbqKeuJQOE"
DEVOTIONAL_TAB = "2026 Rotation"
SERVANTS_TAB = "Servants"

WORSHIP_SHEET_ID = "1gCNLnHpzOCeMrUHHLra9KFIp7iRrFYEdfnFxRMjAhXE"
WORSHIP_TAB = "2026-2027"
WORSHIP_TAB_FALLBACK = "2025-2026"

# Thursday prayer sheet - same Servants tab thursday.py uses.
PRAYER_SHEET_ID = "1Vs26gjZdwhyMlYjVGQ7HUG8bFlLf-SZaDBrkq76o5cs"

COORDINATOR_TO = ["joshmkim0@gmail.com", "peterhahn@cfchome.org"]
ALWAYS_BCC = "peterhahn@cfchome.org"

EMAIL_SUBJECT = "Morning Prayer Schedule for Next Week"

# Short keys match get_morning_prayer_schedule(); labels are for the table.
SCHEDULE_DAYS = (
    ("Mon", "Monday"),
    ("Tue", "Tuesday"),
    ("Wed", "Wednesday"),
    ("Thu", "Thursday"),
    ("Fri", "Friday"),
)

PRAYER_THEMES = {
    "Mon": "Sunday Sermon Reflection",
    "Tue": "Church Meetings & Small Groups",
    "Wed": "Missions (Local/World)",
    "Thu": "Education Ministry",
    "Fri": "Worship Team & Lord's Day Worship",
}

_DAY_ABBREVIATIONS = {
    "mon": "Mon",
    "tue": "Tue",
    "tues": "Tue",
    "wed": "Wed",
    "thu": "Thu",
    "thurs": "Thu",
    "fri": "Fri",
}

_BLANK_VALUES = {"", "—", "-", "–", "n/a", "na", "none"}

_EMAIL_TEXT = """Hi everyone,

Thank you for serving in Morning Prayer next week. Here's the schedule:

{schedule_table}

Format

6:30 Worship
6:35 Devotional
6:45 Individual Prayer Time (transition into the day's theme)
7:29 Closing Prayer
"""

_FALLBACK_NOTE = (
    "We could not fully load this week's schedule from the rotation "
    "sheets. Please check the Devotional and Worship spreadsheets and "
    "forward the correct assignments if needed.\n\n"
)


def get_sheet_range(spreadsheet_id: str, range_name: str) -> list[list]:
    """Fetch a Google Sheet range as a list of row lists.

    Uses an explicit A1 range (never a bare tab name) so filtered or
    hidden views cannot shift columns the way they did in thursday.py.

    Args:
        spreadsheet_id: The spreadsheet to read.
        range_name: A1 notation, e.g. "'2026 Rotation'!A:F".

    Returns:
        list[list]: Raw rows from the Sheets API. Missing cells are
            omitted by the API, so callers should use _cell().

    Raises:
        RuntimeError: If the range cannot be read.
    """
    try:
        service = get_sheet_client()
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=range_name)
            .execute()
        )
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read sheet range {range_name!r}: {exc}"
        ) from exc
    return result.get("values", [])


def get_schedule_monday(now: datetime | None = None) -> date:
    """Return the Monday of the Morning Prayer week to email about.

    The job is meant to run Sunday 6pm Chicago time and cover the
    following Mon–Fri. If it runs after Monday morning (a retry later
    in the week), the last Sunday is used so the current week's row
    is still selected.

    Args:
        now: Optional timezone-aware datetime for tests. Defaults to
            the current time in America/Chicago.

    Returns:
        date: That week's Monday.
    """
    if now is None:
        now = datetime.now(CHICAGO)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=CHICAGO)
    else:
        now = now.astimezone(CHICAGO)

    weekday = now.weekday()  # Monday=0 ... Sunday=6
    if weekday == 6:
        sunday = now.date()
    elif weekday == 0 and now.hour < 12:
        # Monday morning is still the Sunday-night send window.
        sunday = now.date() - timedelta(days=1)
    else:
        days_since_sunday = (weekday + 1) % 7
        sunday = now.date() - timedelta(days=days_since_sunday)

    return sunday + timedelta(days=1)


def get_absent_names(week_monday: date) -> set[str]:
    """Return names flagged absent for the given week.

    Checks:
    - Extra columns on both Servants tabs (Thursday prayer sheet and
      the devotional sheet) for an Absent/Out/Flag header or cell.
    - The worship tab Comments cell for the matching Sunday week row
      (e.g. "Kevin out of town").

    Neither Servants tab currently has an absence column; this stays
    ready for when one is added and still picks up worship comments.

    Args:
        week_monday: Monday of the week being emailed.

    Returns:
        set[str]: Names (as they appear on the sheets) to flag.
    """
    absent: set[str] = set()
    roster = _servant_roster_names()

    for spreadsheet_id in (PRAYER_SHEET_ID, DEVOTIONAL_SHEET_ID):
        try:
            rows = get_sheet_range(spreadsheet_id, f"{SERVANTS_TAB}!A:Z")
        except Exception as exc:
            logger.warning("Could not read Servants absences (%s): %s", spreadsheet_id, exc)
            continue
        absent.update(_absences_from_servants_rows(rows))

    try:
        comment = _worship_week_comment(week_monday)
    except Exception as exc:
        logger.warning("Could not read worship week comment: %s", exc)
        comment = ""
    if comment:
        absent.update(_names_mentioned(comment, roster))

    return absent


def get_morning_prayer_schedule(now: datetime | None = None) -> dict:
    """Build the Mon–Fri Morning Prayer schedule for the target week.

    Args:
        now: Optional clock override for tests. See get_schedule_monday().

    Returns:
        dict: Keys Mon/Tue/Wed/Thu/Fri, each a dict with
            {devotional, worship, theme, devotional_absent,
            worship_absent}. Missing or blank cells are None.
            Sheet errors are logged and that side is left blank
            rather than raising.
    """
    week_monday = get_schedule_monday(now)
    monday_label = _format_monday_date(week_monday)

    devotional: dict[str, str | None] = {key: None for key, _ in SCHEDULE_DAYS}
    worship: dict[str, str | None] = {key: None for key, _ in SCHEDULE_DAYS}

    try:
        devotional = _get_devotional_for_week(monday_label)
    except Exception as exc:
        logger.error("Failed to read devotional rotation: %s", exc)

    try:
        worship = _get_worship_for_week()
    except Exception as exc:
        logger.error("Failed to read worship directory: %s", exc)

    try:
        absences = get_absent_names(week_monday)
    except Exception as exc:
        logger.error("Failed to read absence flags: %s", exc)
        absences = set()

    schedule: dict[str, dict] = {}
    for key, _label in SCHEDULE_DAYS:
        dev_name = _clean_name(devotional.get(key))
        worship_name = _clean_name(worship.get(key))
        schedule[key] = {
            "devotional": dev_name,
            "worship": worship_name,
            "theme": PRAYER_THEMES[key],
            "devotional_absent": _name_is_absent(dev_name, absences),
            "worship_absent": _name_is_absent(worship_name, absences),
        }
    return schedule


def build_morning_prayer_body(
    schedule_dict: dict,
    error_note: str | None = None,
) -> str:
    """Return the complete plain-text email body for a schedule dict.

    Args:
        schedule_dict: Output of get_morning_prayer_schedule().
        error_note: Optional warning inserted after the greeting
            when sheet data could not be loaded.

    Returns:
        str: Plain-text body with {schedule_table} filled in.
    """
    table = _schedule_table(schedule_dict)
    body = _EMAIL_TEXT.replace("{schedule_table}", table)

    note = error_note or ""
    if not note and _schedule_is_empty(schedule_dict):
        note = _FALLBACK_NOTE
    if note:
        body = body.replace("Hi everyone,\n\n", f"Hi everyone,\n\n{note}")
    return body


def send_morning_prayer_email(now: datetime | None = None) -> None:
    """Fetch the schedule, build the plain-text body, and send to coordinators.

    To: Josh and Peter. BCC: peterhahn@cfchome.org plus
    settings.BCC_EMAIL when it is a different address. Sheet failures
    still send a fallback message so coordinators know to look.

    Args:
        now: Optional clock override for tests.

    Raises:
        RuntimeError: If the Gmail send itself fails.
    """
    error_note = None
    try:
        schedule = get_morning_prayer_schedule(now)
    except Exception as exc:
        logger.error("Morning Prayer schedule build failed: %s", exc)
        schedule = _empty_schedule()
        error_note = _FALLBACK_NOTE

    body = build_morning_prayer_body(schedule, error_note=error_note)
    bcc_addresses = _bcc_addresses()

    sent = send_email(
        to=", ".join(COORDINATOR_TO),
        subject=EMAIL_SUBJECT,
        body=body,
        bcc=", ".join(bcc_addresses) if bcc_addresses else None,
    )
    if not sent:
        raise RuntimeError("Failed to send Morning Prayer email")


def send_morning_reminder() -> dict:
    """Cloud Run wrapper around send_morning_prayer_email().

    Returns:
        dict: {"status": "sent"} or {"status": "failed"} so
            /send-morning-prayer-reminder can jsonify the result.
    """
    try:
        send_morning_prayer_email()
    except Exception as exc:
        logger.error("Morning prayer reminder failed: %s", exc)
        return {"status": "failed"}
    return {"status": "sent"}


def _get_devotional_for_week(monday_label: str) -> dict[str, str | None]:
    """Return Mon–Fri devotional names for a "Mon D" week-of label."""
    rows = get_sheet_range(DEVOTIONAL_SHEET_ID, f"'{DEVOTIONAL_TAB}'!A:F")
    target = _normalize_monday_label(monday_label)
    matching_row = None
    for row in rows:
        if _normalize_monday_label(_cell(row, 0)) == target:
            matching_row = row
            break

    if matching_row is None:
        logger.info("No devotional row for monday_date=%r.", monday_label)
        return {key: None for key, _ in SCHEDULE_DAYS}

    # Columns B–F are Monday through Friday.
    return {
        key: _clean_name(_cell(matching_row, index))
        for index, (key, _label) in enumerate(SCHEDULE_DAYS, start=1)
    }


def _get_worship_for_week() -> dict[str, str | None]:
    """Return Mon–Fri worship leaders from the directory columns.

    The worship sheet's weekly rows are song titles, not people.
    Leaders live in columns H–J (name, phone, days) and are a
    standing weekday assignment (e.g. Ryan on Monday).
    """
    rows = _worship_directory_rows()
    directory: dict[str, str | None] = {key: None for key, _ in SCHEDULE_DAYS}
    for row in rows:
        name = _clean_name(_cell(row, 0))
        raw_days = _cell(row, 2)
        if not name or not raw_days or raw_days.lower() == "backup":
            continue
        for key in _parse_worship_days(raw_days):
            directory[key] = name
    return directory


def _worship_directory_rows() -> list[list]:
    """Read H:J from the current worship tab, falling back to last year."""
    last_error = None
    for tab in (WORSHIP_TAB, WORSHIP_TAB_FALLBACK):
        try:
            return get_sheet_range(WORSHIP_SHEET_ID, f"'{tab}'!H:J")
        except Exception as exc:
            last_error = exc
            logger.warning("Worship directory tab %r failed: %s", tab, exc)
    raise RuntimeError(f"Failed to read worship directory: {last_error}")


def _worship_week_comment(week_monday: date) -> str:
    """Return the Comments cell for the worship week of this Sunday."""
    week_sunday = week_monday - timedelta(days=1)
    last_error = None
    for tab in (WORSHIP_TAB, WORSHIP_TAB_FALLBACK):
        try:
            rows = get_sheet_range(WORSHIP_SHEET_ID, f"'{tab}'!A:G")
        except Exception as exc:
            last_error = exc
            continue
        target = _normalize_sheet_date(week_sunday)
        for row in rows:
            if _normalize_sheet_date(_cell(row, 0)) == target:
                return _cell(row, 6)
        return ""
    if last_error:
        raise RuntimeError(f"Failed to read worship week rows: {last_error}")
    return ""


def _absences_from_servants_rows(rows: list[list]) -> set[str]:
    """Parse optional absence columns from a Servants tab."""
    if not rows:
        return set()

    header = [_cell(rows[0], i) for i in range(len(rows[0]))]
    flag_indexes = [
        i
        for i, title in enumerate(header)
        if any(word in title.lower() for word in ("absent", "absence", "out", "flag"))
    ]

    absent: set[str] = set()
    data_rows = rows[1:] if _looks_like_header(header) else rows
    for row in data_rows:
        name = _clean_name(_cell(row, 0))
        if not name or "coordinator" in name.lower():
            continue
        flagged = False
        if flag_indexes:
            flagged = any(_is_absent_flag(_cell(row, i)) for i in flag_indexes)
        else:
            flagged = any(_is_absent_flag(_cell(row, i)) for i in range(1, len(row)))
        if flagged:
            absent.add(name)
    return absent


def _looks_like_header(header: list[str]) -> bool:
    joined = " ".join(header).lower()
    return any(word in joined for word in ("name", "email", "servants", "absent"))


def _is_absent_flag(value: str) -> bool:
    lowered = value.lower().strip()
    return lowered in {"absent", "absence", "out", "yes", "true", "y", "1"}


def _servant_roster_names() -> set[str]:
    """Collect servant names from both Servants tabs for comment matching."""
    names: set[str] = set()
    ranges = (
        (PRAYER_SHEET_ID, f"{SERVANTS_TAB}!A:A"),
        (DEVOTIONAL_SHEET_ID, f"{SERVANTS_TAB}!A:A"),
    )
    for spreadsheet_id, range_name in ranges:
        try:
            rows = get_sheet_range(spreadsheet_id, range_name)
        except Exception:
            continue
        for row in rows:
            name = _clean_name(_cell(row, 0))
            if name and "coordinator" not in name.lower() and name.lower() not in {
                "servants",
                "name",
            }:
                names.add(name)
    return names


def _names_mentioned(text: str, roster: set[str]) -> set[str]:
    """Return roster names whose first or full name appears in text."""
    lowered = text.lower()
    found: set[str] = set()
    for name in roster:
        if name.lower() in lowered:
            found.add(name)
            continue
        first = name.replace("-", " ").split()[0].lower()
        if first and first in lowered.split():
            found.add(name)
    return found


def _name_is_absent(name: str | None, absences: set[str]) -> bool:
    if not name:
        return False
    for absent in absences:
        if name.lower() == absent.lower():
            return True
        if _name_tokens(name) & _name_tokens(absent):
            return True
    return False


def _name_tokens(name: str) -> set[str]:
    return {part for part in name.replace("-", " ").lower().split() if part}


def _parse_worship_days(raw: str) -> list[str]:
    """Parse a directory day cell like "Tues, Thurs" into Mon/Tue keys."""
    days: list[str] = []
    for token in raw.split(","):
        key = _DAY_ABBREVIATIONS.get(token.strip().rstrip(".").lower())
        if key and key not in days:
            days.append(key)
    return days


def _schedule_table(schedule_dict: dict) -> str:
    """Build a space-aligned plain-text schedule table."""
    lines = [
        f"{'Day':<11}{'Devotional':<17}{'Worship':<17}Prayer Theme",
        f"{'------':<11}{'--------':<17}{'-------':<17}-----",
    ]
    for key, label in SCHEDULE_DAYS:
        day = schedule_dict.get(key) or {}
        devotional = _render_name(
            day.get("devotional"),
            bool(day.get("devotional_absent")),
        )
        worship = _render_name(
            day.get("worship"),
            bool(day.get("worship_absent")),
        )
        theme = day.get("theme") or PRAYER_THEMES[key]
        lines.append(f"{label:<11}{devotional:<17}{worship:<17}{theme}")
    return "\n".join(lines)


def _render_name(name: str | None, is_absent: bool) -> str:
    if not name:
        return "—"
    if is_absent:
        return f"{name} (absent)"
    return name


def _clean_name(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if cleaned.lower() in _BLANK_VALUES:
        return None
    return cleaned


def _empty_schedule() -> dict:
    return {
        key: {
            "devotional": None,
            "worship": None,
            "theme": PRAYER_THEMES[key],
            "devotional_absent": False,
            "worship_absent": False,
        }
        for key, _label in SCHEDULE_DAYS
    }


def _schedule_is_empty(schedule_dict: dict) -> bool:
    return all(
        not (schedule_dict.get(key) or {}).get("devotional")
        and not (schedule_dict.get(key) or {}).get("worship")
        for key, _label in SCHEDULE_DAYS
    )


def _bcc_addresses() -> list[str]:
    addresses: list[str] = []
    seen: set[str] = set()
    for address in (ALWAYS_BCC, settings.BCC_EMAIL):
        if not address:
            continue
        key = address.lower()
        if key in seen:
            continue
        seen.add(key)
        addresses.append(address)
    return addresses


def _format_monday_date(monday: date) -> str:
    """Format as "Mon D" with no leading zero, e.g. "Aug 31"."""
    return f"{monday.strftime('%b')} {monday.day}"


def _normalize_monday_label(raw: str) -> str:
    """Normalize a week-of cell to "Mon D" so "Sep 07" matches "Sep 7"."""
    raw = " ".join(raw.split())
    if not raw:
        return ""
    parts = raw.split()
    if len(parts) != 2 or not parts[1].isdigit():
        return raw
    month, day = parts
    for fmt in ("%b", "%B"):
        try:
            parsed = datetime.strptime(f"{month} {int(day)}", f"{fmt} %d")
            return _format_monday_date(parsed.date())
        except ValueError:
            continue
    return raw


def _normalize_sheet_date(raw: str | date) -> str:
    """Normalize a sheet date to "M/D/YY" for week-row matching."""
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return f"{raw.month}/{raw.day}/{raw.strftime('%y')}"
    raw = str(raw).strip()
    if not raw:
        return ""
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
            return f"{parsed.month}/{parsed.day}/{parsed.strftime('%y')}"
        except ValueError:
            continue
    return raw


def _cell(row: list, index: int) -> str:
    """Safely read a cell from a sheet row by column index."""
    if index < 0 or index >= len(row):
        return ""
    return str(row[index]).strip()
