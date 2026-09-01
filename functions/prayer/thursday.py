# Reads the Thursday Night Prayer Meeting schedule from Google Sheets
# and builds reminder emails for that week's speaker and praise leader.

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from config import settings
from functions.read_sheets import get_sheet_client
from functions.send_email import send_email

logger = logging.getLogger(__name__)

PRAYER_SHEET_ID = "1Vs26gjZdwhyMlYjVGQ7HUG8bFlLf-SZaDBrkq76o5cs"
CURRENT_SCHEDULE_TAB = "2026 Fall"
SERVANTS_TAB = "Servants"

# Speaker-cell values that mean there is no prayer meeting that week.
NON_MEETING_VALUES = {
    "fall break",
    "spring break",
    "summer break",
    "maundy thursday",
    "no prayer meeting",
    "thanksgiving",
    "fourth of july weekend",
}

# Cached {name: email} from the Servants tab - populated once per process.
_servant_emails_cache: dict[str, str] | None = None


def get_servant_emails() -> dict[str, str]:
    """Return a name-to-email map from the Servants tab.

    Column A is the servant's name, column B is their email. Cached
    after the first successful read so later lookups in the same
    process don't re-hit Sheets.

    Returns:
        dict[str, str]: {name: email, ...}. Empty dict if the tab
            can't be read or has no data.

    Raises:
        RuntimeError: If the Servants tab can't be read.
    """
    global _servant_emails_cache

    if _servant_emails_cache is not None:
        return _servant_emails_cache

    try:
        service = get_sheet_client()
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=PRAYER_SHEET_ID, range=f"{SERVANTS_TAB}!A:C")
            .execute()
        )
        rows = result.get("values", [])
    except Exception as exc:
        raise RuntimeError(f"Failed to read '{SERVANTS_TAB}' tab: {exc}") from exc

    emails: dict[str, str] = {}
    for row in rows[1:]:  # row 0 is the header (Servants, Email)
        name = _cell(row, 0)
        email = _cell(row, 1)
        if name and email:
            emails[name] = email

    _servant_emails_cache = emails
    return _servant_emails_cache


def get_thursday_schedule(target_date: str) -> dict | None:
    """Return the prayer-meeting assignment for a given Thursday.

    Reads CURRENT_SCHEDULE_TAB and finds the row whose column A date
    matches target_date. Layout is Date (A), Speaker (B), Praise (C),
    Slides (D, ignored), Notes (E), Schedule (F). Older rows may store
    "Name: email@domain" in Speaker/Praise; newer rows store just the
    name and the email is looked up from the Servants tab. If the
    speaker cell is a known non-meeting value (break, holiday, etc.),
    or no matching row exists, this returns None.

    Args:
        target_date: The Thursday to look up, as "M/D/YY" with no
            leading zeros, e.g. "9/3/26".

    Returns:
        dict or None: {"date", "speaker_name", "speaker_email",
            "praise_name", "praise_email", "notes"} when a meeting
            is scheduled, otherwise None.

    Raises:
        RuntimeError: If the schedule tab can't be read.
    """
    try:
        service = get_sheet_client()
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=PRAYER_SHEET_ID, range=CURRENT_SCHEDULE_TAB)
            .execute()
        )
        rows = result.get("values", [])
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read '{CURRENT_SCHEDULE_TAB}' tab: {exc}"
        ) from exc

    if not rows:
        return None

    target_normalized = _normalize_sheet_date(target_date)
    matching_row = None
    for row in rows[1:]:  # row 0 is the header
        row_date = _normalize_sheet_date(_cell(row, 0))
        if row_date and row_date == target_normalized:
            matching_row = row
            break

    if matching_row is None:
        logger.info("No prayer schedule row found for target_date=%r.", target_date)
        return None

    raw_speaker = _cell(matching_row, 1)  # column B
    raw_praise = _cell(matching_row, 2)  # column C
    notes = _cell(matching_row, 4)  # column E

    speaker_name, speaker_email = _parse_servant_cell(raw_speaker)
    if speaker_name.lower() in NON_MEETING_VALUES:
        logger.info(
            "No prayer meeting on %s (%s).",
            target_date,
            speaker_name,
        )
        return None

    praise_name, praise_email = _parse_servant_cell(raw_praise)

    return {
        "date": target_date,
        "speaker_name": speaker_name,
        "speaker_email": speaker_email,
        "praise_name": praise_name,
        "praise_email": praise_email,
        "notes": notes,
    }


def get_next_thursday_date() -> str:
    """Return the upcoming Thursday as "M/D/YY" with no leading zeros.

    If today is already Thursday, returns today rather than next week.

    Returns:
        str: The date, e.g. "9/3/26".
    """
    today = date.today()
    # weekday(): Monday=0 ... Thursday=3 ... Sunday=6
    days_until_thursday = (3 - today.weekday()) % 7
    thursday = today + timedelta(days=days_until_thursday)
    return f"{thursday.month}/{thursday.day}/{thursday.strftime('%y')}"


def build_reminder_email_body(speaker_name: str, worship_leader_name: str, formatted_date: str) -> str:
    return f"""{speaker_name} and {worship_leader_name},

Thank you for serving the church by sharing the Word and leading praise at the upcoming Thursday Night Prayer Meeting {formatted_date}. Below is the schedule and agenda for the evening.

Schedule

7:00 PM - Opening
Praise
Begin with worship and praise to prepare our hearts.

7:10 PM - Sermon
The speaker shares the message for the evening.

7:40 PM - Individual Prayer
After the sermon, the speaker will guide the congregation into a time of personal prayer and reflection.

8:10 PM - Corporate Prayer
The speaker will lead the congregation in praying over specific topics. (Sample topics are provided below.)

8:30 PM - Group Prayer
The speaker will transition the congregation to pray in small groups, with a partner, or individually.

9:10 PM - Closing Praise
Reunite for one final song of praise and worship.

9:15 PM - Closing Prayer
The speaker closes the meeting with prayer.

Sample Prayer Topics
- Pastor KJ, elders, and leaders
- Congregation esp those in difficult times
- Pray for one age group like undergrads/gsg, youth groups, etc
- Church events (Lord's Day Service and upcoming events like Revival)
- Campus/Community

Let me know if you have any questions.

Blessings.

---
This is an automated reminder sent by the CFC Prayer Coordination Agent."""


def send_thursday_reminder(target_date: str | None = None) -> dict:
    """Send the Thursday reminder to that week's speaker and praise leader.

    Args:
        target_date: Optional "M/D/YY" Thursday to send for. Defaults
            to get_next_thursday_date().

    Returns:
        dict: {"status": "sent"}, {"status": "skipped"} if there is no
            meeting that week, or {"status": "failed"}.
    """
    if target_date is None:
        target_date = get_next_thursday_date()

    try:
        assignment = get_thursday_schedule(target_date)
    except Exception as exc:
        logger.error("Failed to read Thursday prayer schedule: %s", exc)
        return {"status": "failed"}

    if assignment is None:
        return {"status": "skipped"}

    formatted_date = _format_full_date(target_date)
    body = build_reminder_email_body(
        assignment["speaker_name"],
        assignment["praise_name"],
        formatted_date,
    )

    recipients = [
        email
        for email in (assignment.get("speaker_email"), assignment.get("praise_email"))
        if email
    ]
    if not recipients:
        logger.error(
            "No servant emails on file for speaker=%r praise=%r; not sending.",
            assignment.get("speaker_name"),
            assignment.get("praise_name"),
        )
        return {"status": "failed"}

    try:
        sent = send_email(
            to=", ".join(recipients),
            subject=f"Thursday Night Prayer Meeting Reminder - {formatted_date}",
            body=body,
            bcc=settings.BCC_EMAIL,
        )
    except Exception as exc:
        logger.error("Failed to send Thursday prayer reminder: %s", exc)
        return {"status": "failed"}

    if not sent:
        return {"status": "failed"}
    return {"status": "sent"}


def _parse_servant_cell(raw: str) -> tuple[str, str | None]:
    """Split a Speaker/Praise cell into a name and email.

    Older rows store "Name: email@domain". Newer rows store just the
    name, so the email is looked up from get_servant_emails().

    Args:
        raw: The raw cell value from the schedule tab.

    Returns:
        tuple[str, str | None]: (name, email). email is None if the
            cell is empty or no lookup match exists.
    """
    if not raw:
        return "", None

    if ":" in raw:
        name, email = raw.split(":", 1)
        name = name.strip()
        email = email.strip() or None
        return name, email

    return raw, get_servant_emails().get(raw)


def _cell(row: list[str], index: int) -> str:
    """Safely read a cell from a sheet row by column index."""
    if index < 0 or index >= len(row):
        return ""
    return str(row[index]).strip()


def _normalize_sheet_date(raw: str) -> str:
    """Normalize a sheet date to "M/D/YY" so "9/3/26" matches "09/03/2026"."""
    raw = raw.strip()
    if not raw:
        return ""
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
            return f"{parsed.month}/{parsed.day}/{parsed.strftime('%y')}"
        except ValueError:
            continue
    return raw


def _format_full_date(sheet_date: str) -> str:
    """Format a "M/D/YY" sheet date as "September 3, 2026"."""
    normalized = _normalize_sheet_date(sheet_date)
    try:
        parsed = datetime.strptime(normalized, "%m/%d/%y")
    except ValueError:
        return sheet_date
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
