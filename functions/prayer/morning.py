# Reads the Morning Prayer weekly schedule from Google Sheets
# (devotional rotation + worship-leader directory) and sends a
# plain-text reminder to that week's servants via the same Gmail
# domain-wide delegation path as functions/prayer/thursday.py.

from __future__ import annotations

import logging
import re

from datetime import date, datetime, timedelta

import google.auth
from googleapiclient.discovery import build

from config import settings
# Central, shared with everything else in this system that asks what day
# it is. See config/clock.py for why a UTC answer is always wrong here.
from config.clock import CHICAGO
from functions.read_sheets import get_sheet_client
from functions.send_email import send_email

logger = logging.getLogger(__name__)

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

---
Sent by the CFC Coordination Agent
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

    The job fires Saturday 6pm Chicago (Cloud Scheduler job
    morning-prayer-reminder, "0 18 * * 6") and covers the Mon-Fri that
    follows.

    This used to be written for a Sunday 6pm run, and on a Saturday it
    walked back six days to the previous Sunday. Every email described
    the week that had just ended rather than the week ahead: the send on
    Sat 9/12 covered Mon 9/7 through Fri 9/11. The names were read
    correctly off the rotation sheet, just from the wrong row, which is
    why it looked like the wrong people rather than like a broken job.

    Runs outside the Saturday window are treated as retries:

        Saturday          -> the Monday two days out (the send window)
        Sunday            -> the Monday one day out (same upcoming week)
        Monday before noon-> today, still that same week
        Mon pm to Friday  -> the current week's Monday, so a late retry
                             re-sends the week in progress rather than
                             skipping ahead to one nobody has reached

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

    weekday = now.weekday()  # Monday=0 ... Saturday=5, Sunday=6
    today = now.date()

    if weekday == 5:  # Saturday: the actual send window
        return today + timedelta(days=2)
    if weekday == 6:  # Sunday: still pointing at the same upcoming week
        return today + timedelta(days=1)
    if weekday == 0 and now.hour < 12:  # Monday morning, that week has begun
        return today
    return today - timedelta(days=weekday)


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
    worship: dict[str, dict] = {
        key: {"name": None, "absent": False} for key, _ in SCHEDULE_DAYS
    }

    try:
        devotional = _get_devotional_for_week(monday_label)
    except Exception as exc:
        logger.error("Failed to read devotional rotation: %s", exc)

    try:
        worship = _get_worship_for_week(week_monday)
    except Exception as exc:
        logger.error("Failed to read worship schedule: %s", exc)

    try:
        absences = get_absent_names(week_monday)
    except Exception as exc:
        logger.error("Failed to read absence flags: %s", exc)
        absences = set()

    schedule: dict[str, dict] = {}
    for key, _label in SCHEDULE_DAYS:
        dev_name = _clean_name(devotional.get(key))
        worship_day = worship.get(key) or {}
        worship_name = _clean_name(worship_day.get("name"))
        schedule[key] = {
            "devotional": dev_name,
            "worship": worship_name,
            "theme": PRAYER_THEMES[key],
            "devotional_absent": _name_is_absent(dev_name, absences),
            # Colour is the authority for worship: a covered day already
            # names the substitute, so it is NOT an absence. Only the
            # grey ABSENCE swatch, or an explicit comment naming them,
            # marks the slot as uncovered.
            "worship_absent": bool(worship_day.get("absent"))
            or _name_is_absent(worship_name, absences),
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


def get_roster_emails() -> dict[str, str]:
    """Return a name-to-email map from the Devotional Servants tab.

    Reads DEVOTIONAL_SHEET_ID Servants!A:C. Column A is the name,
    B is the phone, C is the email. Row 0 is a coordinator label
    (not a roster header); rows without both a name and an email
    are skipped.

    Returns:
        dict[str, str]: {name: email, ...}.

    Raises:
        RuntimeError: If the Servants tab can't be read.
    """
    rows = get_sheet_range(DEVOTIONAL_SHEET_ID, f"{SERVANTS_TAB}!A:C")
    roster: dict[str, str] = {}
    for row in rows[1:]:
        name = _cell(row, 0)
        email = _cell(row, 2)
        if not name or not email or "@" not in email:
            continue
        if "coordinator" in name.lower():
            continue
        roster[name] = email
    return roster


def get_recipients_for_week(schedule_dict: dict, roster: dict[str, str]) -> list[str]:
    """Return sorted unique emails for this week's devotional and worship leaders.

    Looks up each day's names in the roster with an exact match first,
    then a first-name match so "Ryan Bielak" resolves to "Ryan" and
    "Dae-Woung" resolves to "Dae". Names with no match are logged.

    Args:
        schedule_dict: Output of get_morning_prayer_schedule().
        roster: Output of get_roster_emails().

    Returns:
        list[str]: Unique recipient emails, sorted.
    """
    emails, _ = resolve_recipients_for_week(schedule_dict, roster)
    return emails


def resolve_recipients_for_week(
    schedule_dict: dict, roster: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Resolve the week's recipients, and report the names that failed.

    Args:
        schedule_dict: Output of get_morning_prayer_schedule().
        roster: Output of get_roster_emails().

    Returns:
        tuple[list[str], list[str]]: Unique recipient emails sorted, and
            a sorted list of human-readable problems, one per name that
            could not be resolved to exactly one person. Problems are
            emailed to the coordinator rather than only logged, because
            the whole failure mode here is silent: a name that resolves
            to nobody just quietly misses their reminder.
    """
    emails: set[str] = set()
    problems: set[str] = set()

    for key, label in SCHEDULE_DAYS:
        day = schedule_dict.get(key) or {}
        for role, name in (
            ("devotional", day.get("devotional")),
            ("worship", day.get("worship")),
        ):
            if not name:
                continue
            email, reason = resolve_roster_email(name, roster)
            if email:
                emails.add(email)
            else:
                problems.add(f"{label} {role}: {reason}")
                logger.warning("Morning Prayer recipient unresolved: %s", reason)

    return sorted(emails), sorted(problems)


def send_morning_prayer_email(now: datetime | None = None) -> None:
    """Fetch the schedule, build the plain-text body, and send to servants.

    To: every unique devotional and worship leader serving that week.
    BCC: peterhahn@cfchome.org plus settings.BCC_EMAIL when it is a
    different address. Sheet failures still send a fallback message
    so someone on BCC knows to look.

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

    try:
        roster = get_roster_emails()
        recipients, problems = resolve_recipients_for_week(schedule, roster)
    except Exception as exc:
        logger.error("Failed to resolve Morning Prayer recipients: %s", exc)
        return

    if not recipients:
        logger.warning("No Morning Prayer recipients for this week; not sending.")
        return

    body = build_morning_prayer_body(schedule, error_note=error_note)
    bcc_addresses = _bcc_addresses()

    sent = send_email(
        to=", ".join(recipients),
        subject=EMAIL_SUBJECT,
        body=body,
        bcc=", ".join(bcc_addresses) if bcc_addresses else None,
    )
    if not sent:
        raise RuntimeError("Failed to send Morning Prayer email")

    if problems:
        _alert_unresolved_names(problems)


def _alert_unresolved_names(problems: list[str]) -> None:
    """Email the coordinator about names that couldn't be resolved.

    Sent separately rather than appended to the reminder, so the people
    serving that week don't read internal bookkeeping. Never raises: a
    failed alert must not take down a reminder that already went out.
    """
    lines = [
        "Some names on this week's Morning Prayer schedule could not be",
        "matched to exactly one person on the Servants tab, so they did",
        "NOT receive the reminder:",
        "",
    ]
    lines.extend(f"  - {problem}" for problem in problems)
    lines += [
        "",
        "A name matching nobody is usually a spelling difference between",
        "the rotation sheet and the Servants tab. A name matching several",
        "people needs a surname added on the rotation sheet to tell them",
        "apart. Either way the fix is in the sheet, not the code.",
        "",
        "Nobody is ever guessed at. That is deliberate: the previous",
        "behavior was to pick whichever namesake appeared first on the",
        "Servants tab, which quietly sent one person's reminder to",
        "another.",
    ]

    try:
        send_email(
            to=ALWAYS_BCC,
            subject="Morning Prayer: unmatched names this week",
            body="\n".join(lines),
            bcc=None,
        )
        logger.info("Alerted %s about %d unresolved name(s).", ALWAYS_BCC, len(problems))
    except Exception as exc:
        logger.error("Could not send unresolved-name alert: %s", exc)


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


def _get_worship_for_week(week_monday: date) -> dict[str, dict]:
    """Return Mon-Fri worship leaders for one week, read from cell colors.

    The sheet encodes the weekly assignment as colour, not text. Every
    leader has a signature colour, visible on their own name cell in the
    directory (columns H-J). A weekday column is normally tinted with
    its regular leader's colour, and when somebody else covers, that
    week's cell is tinted with the SUBSTITUTE's colour instead.

    So the colour on a day cell is the assignment for that week, and the
    directory's days column (J) is only the standing default.

    This was invisible to the code until now, because everything else
    here reads Sheets through the values API, which returns contents and
    no formatting at all. The week of 9/14/2026 is the example: Monday
    was tinted Kevin Kim's colour while Ryan recovered from surgery, and
    Friday was tinted Albert Lee's for a four week stretch, and the
    email announced Ryan and Andrew for both.

    Args:
        week_monday: Monday of the week being emailed.

    Returns:
        dict: {day_key: {"name": str | None, "absent": bool}}. absent is
            True only when the cell carries the grey ABSENCE colour from
            the legend, meaning nobody is covering; in that case the
            standing leader is named so the reader knows who is out.
    """
    grid = _worship_grid()
    directory = _worship_directory(grid)
    colour_to_name = {
        entry["colour"]: entry["name"]
        for entry in directory
        if entry["colour"]
    }
    absence_colour = _legend_colour(grid, "ABSENCE")

    # The standing assignment, used when a cell has no usable colour.
    standing: dict[str, str | None] = {key: None for key, _ in SCHEDULE_DAYS}
    for entry in directory:
        if entry["days"].strip().lower() == "backup":
            continue
        for key in _parse_worship_days(entry["days"]):
            if standing[key] is not None:
                logger.warning(
                    "Two standing worship leaders for %s (%s and %s); "
                    "keeping the first.",
                    key,
                    standing[key],
                    entry["name"],
                )
                continue
            standing[key] = entry["name"]

    row = _week_row(grid, week_monday - timedelta(days=1))

    result: dict[str, dict] = {}
    for offset, (key, _label) in enumerate(SCHEDULE_DAYS):
        cell_colour = _cell_colour(row, offset + 1)  # B..F are the weekdays
        default = standing.get(key)

        if absence_colour and cell_colour == absence_colour:
            result[key] = {"name": default, "absent": True}
            continue

        name = colour_to_name.get(cell_colour) if cell_colour else None
        if name is None:
            # No colour, or a colour belonging to nobody in the
            # directory. Fall back rather than dropping the day, and say
            # so, since an unrecognised colour usually means a leader
            # was added without a directory row.
            if cell_colour and cell_colour not in (None, _WHITE_HEX):
                logger.warning(
                    "Worship cell for %s is %s, which matches no leader; "
                    "using the standing assignment %r.",
                    key,
                    cell_colour,
                    default,
                )
            name = default

        result[key] = {"name": name, "absent": False}

    return result


_WHITE_HEX = "#FFFFFF"

# Names in the directory's days column that are not weekday assignments.
_NON_DAY_DIRECTORY_VALUES = {"backup"}


def _worship_grid() -> list[dict]:
    """Read the worship tab once, WITH cell formatting, falling back a year.

    One grid call serves the directory, the weekly colours and the
    comments column. The previous code made a separate values call for
    each, inside a path that already has a Twilio-style latency budget
    to respect.
    """
    service = _sheets_grid_client()
    last_error = None
    for tab in (WORSHIP_TAB, WORSHIP_TAB_FALLBACK):
        try:
            result = (
                service.spreadsheets()
                .get(
                    spreadsheetId=WORSHIP_SHEET_ID,
                    ranges=[f"'{tab}'!A1:L200"],
                    includeGridData=True,
                )
                .execute()
            )
            return result["sheets"][0]["data"][0].get("rowData", [])
        except Exception as exc:
            last_error = exc
            logger.warning("Worship tab %r failed: %s", tab, exc)
    raise RuntimeError(f"Failed to read worship sheet: {last_error}")


def _sheets_grid_client():
    """A Sheets client for the grid API, which returns formatting.

    Separate from read_sheets.get_sheet_client() only because that one
    is shared by every values read in the project; this needs the same
    read-only scope but a distinct call.
    """
    global _grid_service
    if _grid_service is None:
        try:
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
            )
            _grid_service = build("sheets", "v4", credentials=credentials)
        except Exception as exc:
            raise RuntimeError(f"Failed to initialize Sheets grid client: {exc}") from exc
    return _grid_service


_grid_service = None


def _cell_value(row: dict | None, index: int) -> str:
    cells = (row or {}).get("values") or []
    if index >= len(cells):
        return ""
    return str((cells[index] or {}).get("formattedValue", "")).strip()


def _cell_colour(row: dict | None, index: int) -> str | None:
    """Background colour of one cell as an uppercase hex string.

    Google omits channels that are at full value, so an unset background
    arrives as {} and has to be read as white rather than as black.
    """
    cells = (row or {}).get("values") or []
    if index >= len(cells):
        return None
    fmt = (cells[index] or {}).get("effectiveFormat") or {}
    background = fmt.get("backgroundColor")
    if background is None:
        return None
    channels = (
        background.get("red", 1.0),
        background.get("green", 1.0),
        background.get("blue", 1.0),
    )
    return "#" + "".join(f"{int(round(c * 255)):02X}" for c in channels)


def _worship_directory(grid: list[dict]) -> list[dict]:
    """Leaders from columns H-J, each with the colour of their name cell.

    A directory row is one with BOTH a name in H and something in J.
    That is what separates real entries from the legend block lower
    down, where "ABSENCE" sits in H with J empty.
    """
    entries = []
    for row in grid:
        name = _clean_name(_cell_value(row, 7))
        days = _cell_value(row, 9)
        if not name or not days:
            continue
        entries.append(
            {"name": name, "days": days, "colour": _cell_colour(row, 7)}
        )
    return entries


def _legend_colour(grid: list[dict], label: str) -> str | None:
    """The colour of a legend swatch, looked up by its own text.

    Read from the sheet rather than hardcoded, so recolouring the legend
    keeps working and nobody has to find a hex constant buried in here.
    """
    wanted = label.strip().lower()
    for row in grid:
        for index in (7, 8, 1):
            if _cell_value(row, index).strip().lower() == wanted:
                return _cell_colour(row, index)
    return None


def _week_row(grid: list[dict], week_sunday: date) -> dict | None:
    """The weekly row whose column A date matches this Sunday."""
    target = _normalize_sheet_date(week_sunday)
    for row in grid:
        if _normalize_sheet_date(_cell_value(row, 0)) == target:
            return row
    logger.warning("No worship row found for week of %s.", week_sunday)
    return None


def _worship_week_comment(week_monday: date) -> str:
    """Return the Comments cell (column G) for this week's worship row."""
    row = _week_row(_worship_grid(), week_monday - timedelta(days=1))
    return _cell_value(row, 6)


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
    """Return roster names whose first or full name appears in text.

    Matched on word boundaries rather than as substrings. The old
    substring test meant any short given name hiding inside an ordinary
    English word marked that person absent: "No prayer this Sunday"
    flagged Sun, "arrive 10 minutes early" flagged Min, and "Enjoy the
    week" flagged Joy. Short Korean given names are exactly the ones
    ordinary words swallow, so this hit the roster it could least
    afford to.
    """
    lowered = text.lower()
    found: set[str] = set()
    for name in roster:
        if _mentions_word(lowered, name.lower()):
            found.add(name)
            continue
        first = _given_name(name)
        if first and _mentions_word(lowered, first):
            found.add(name)
    return found


def _mentions_word(haystack_lower: str, needle_lower: str) -> bool:
    """Whether needle appears in haystack as a whole word."""
    if not needle_lower:
        return False
    return re.search(rf"\b{re.escape(needle_lower)}\b", haystack_lower) is not None


def _name_is_absent(name: str | None, absences: set[str]) -> bool:
    """Whether this schedule name matches anyone marked absent.

    Uses _same_person rather than the old "any token in common" test,
    which treated everyone sharing a surname as the same human being.
    On a roster where a large share of surnames are Kim, Lee or Park,
    marking one person away struck out every one of their namesakes on
    the rendered schedule.
    """
    if not name:
        return False
    return any(_same_person(name, absent) for absent in absences)


def _given_name(name: str | None) -> str:
    """The first token of a name, lowercased. "Dae-Woung Kang" -> "dae"."""
    tokens = _ordered_tokens(name)
    return tokens[0] if tokens else ""


def _ordered_tokens(name: str | None) -> list[str]:
    """Lowercased name tokens in order, hyphens treated as spaces.

    Trailing punctuation is dropped so "Kristin K." tokenizes the same
    way "Kristin K" does.
    """
    if not name:
        return []
    cleaned = name.replace("-", " ").lower()
    return [part.strip(".,;:") for part in cleaned.split() if part.strip(".,;:")]


def _same_person(a: str | None, b: str | None) -> bool:
    """Whether two written names plausibly refer to one person.

    The rule: given names must match, and the remaining tokens must not
    contradict each other. One side having no surname is treated as
    compatible, since schedules routinely carry a bare first name while
    the roster carries the full one.

        "Justin"      vs "Justin Kim"   -> same    (surname absent, not contradicted)
        "Kristin K."  vs "Kristin Kim"  -> same    (K is a prefix of Kim)
        "Justin Kim"  vs "Kristin Kim"  -> DIFFERENT (given names differ)
        "David Sun"   vs "David Lee"    -> DIFFERENT (surnames contradict)
        "Kim"         vs "Justin Kim"   -> DIFFERENT (a bare surname identifies nobody)

    That last case is the important one. It is tempting to let a bare
    surname match, and it is exactly how the old code handed one
    person's email to another.
    """
    a_tokens, b_tokens = _ordered_tokens(a), _ordered_tokens(b)
    if not a_tokens or not b_tokens:
        return False
    if a_tokens == b_tokens:
        return True
    if a_tokens[0] != b_tokens[0]:
        return False

    a_rest, b_rest = a_tokens[1:], b_tokens[1:]
    if not a_rest or not b_rest:
        # One side is a bare given name. Nothing contradicts.
        return True
    if len(a_rest) != len(b_rest):
        return False
    # Allow an initial to stand for a full surname, so "Kristin K"
    # matches "Kristin Kim" but never "Kristin Park".
    return all(
        x == y or x.startswith(y) or y.startswith(x)
        for x, y in zip(a_rest, b_rest)
    )


def _lookup_roster_email(name: str, roster: dict[str, str]) -> str | None:
    """Resolve a schedule name to exactly one Servants-tab email.

    Returns None when the name matches nobody OR matches more than one
    person. Refusing to answer is the point: the previous version
    returned the first roster entry sharing any token, in sheet row
    order, so "Ryan Kim" was handed Ryan Bielak's address and reordering
    the Servants tab silently changed who got the email.

    Use resolve_roster_email() when the caller needs to report why.
    """
    email, _ = resolve_roster_email(name, roster)
    return email


def resolve_roster_email(
    name: str, roster: dict[str, str]
) -> tuple[str | None, str]:
    """Resolve a name to one email, with the reason when it can't.

    Args:
        name: The name as written on the schedule sheet.
        roster: Output of get_roster_emails().

    Returns:
        tuple[str | None, str]: The email and an empty string on
            success, or None and a human-readable reason. The reason is
            written to be read by whoever has to fix the sheet, so it
            names the candidates when a name is ambiguous.
    """
    if not name:
        return None, "empty name"

    for roster_name, email in roster.items():
        if roster_name.lower() == name.lower():
            return email, ""

    matches = [(rn, em) for rn, em in roster.items() if _same_person(name, rn)]
    if len(matches) == 1:
        return matches[0][1], ""
    if not matches:
        return None, f"{name!r} is not on the Servants tab"
    names = ", ".join(sorted(rn for rn, _ in matches))
    return None, f"{name!r} could be any of: {names}"


def _name_tokens(name: str) -> set[str]:
    return {part for part in name.replace("-", " ").lower().split() if part}


def _parse_worship_days(raw: str) -> list[str]:
    """Parse a directory day cell like "Tues, Thurs" into Mon/Tue keys.

    Splits on commas, slashes, ampersands, "and", and plain whitespace,
    and matches a token by its first three letters. The old version
    looked each token up in an exact abbreviation table split on commas
    only, so "Monday", "Mon/Wed" and "Mon & Wed" all matched nothing and
    that leader silently vanished from the schedule with no error.
    """
    days: list[str] = []
    for token in re.split(r"[,/&+;]|\band\b|\s+", raw.lower()):
        token = token.strip().rstrip(".")
        if len(token) < 3:
            continue
        key = _DAY_ABBREVIATIONS.get(token[:3])
        if key and key not in days:
            days.append(key)
    return days


def _schedule_table(schedule_dict: dict) -> str:
    """Build a space-aligned plain-text schedule table.

    Column widths are measured from the content rather than fixed. The
    fixed widths used to run cells together whenever one overflowed:
    "Ryan Bielak (absent)" is twenty characters in a seventeen character
    column, so it printed as "Ryan Bielak (absent)Sunday Sermon
    Reflection" with no gap at all. A slot reading "(Backup)" makes that
    worse, not better.
    """
    rows = []
    for key, label in SCHEDULE_DAYS:
        day = schedule_dict.get(key) or {}
        rows.append(
            (
                label,
                _render_name(
                    day.get("devotional"), bool(day.get("devotional_absent"))
                ),
                _render_name(day.get("worship"), bool(day.get("worship_absent"))),
                day.get("theme") or PRAYER_THEMES[key],
            )
        )

    headers = ("Day", "Devotional", "Worship", "Prayer Theme")
    # Two spaces of breathing room, and the last column never pads.
    widths = [
        max(len(headers[i]), max(len(row[i]) for row in rows)) + 2
        for i in range(3)
    ]

    def line(cells):
        return (
            "".join(f"{cells[i]:<{widths[i]}}" for i in range(3)) + cells[3]
        ).rstrip()

    return "\n".join(
        [
            line(headers),
            line(tuple("-" * len(h) for h in headers)),
            *(line(row) for row in rows),
        ]
    )


def _render_name(name: str | None, is_absent: bool) -> str:
    """One schedule cell.

    An absent worship leader is named rather than hidden, with Backup in
    place of a substitute, because the sheet names two backups and
    picking one would invent an assignment nobody made. A day that IS
    covered never reaches here as absent: the covering leader's own
    colour on that cell is what _get_worship_for_week reads, so their
    name is simply printed as normal.
    """
    if not name:
        return "—" if not is_absent else "Backup"
    if is_absent:
        return f"{name} out - Backup"
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
