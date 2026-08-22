# Reads rider signups from the Google Sheet rider form responses.
#
# Rider signups come in through a Google Form (like driver signups), and
# land in a "Form Responses 1" tab on a separate spreadsheet identified by
# settings.RIDER_SHEET_ID. This module reads that tab, filters to the
# current week's signup window, and maps each rider's chosen stop to the
# shuttle that serves it.

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from config import settings
from functions.read_sheets import get_sheet_client

logger = logging.getLogger(__name__)

# Tab name within the settings.RIDER_SHEET_ID spreadsheet.
FORM_RESPONSES_TAB = "Form Responses 1"

# Fixed column positions in the sheet (A=0, B=1, ... G=6). Column G
# ("Driver") is intentionally not read here.
_TIMESTAMP_COL = 0
_NAME_COL = 1
_GRADE_COL = 2
_STOP_COL = 3
_PHONE_COL = 4
_EMAIL_COL = 5

# Which shuttle serves each campus stop. Any stop not listed here
# (including "Other") is not served by a shuttle and is ignored.
STOP_TO_SHUTTLE = {
    "Allen": "shuttle_2",
    "FAR": "shuttle_1",
    "Icon": "shuttle_2",
    "ISR": "shuttle_2",
    "SDRP": "shuttle_1",
}

# Each shuttle can seat 14 riders plus the driver (15 total).
MAX_RIDERS_PER_SHUTTLE = 14

# Google Forms timestamps are typically "M/D/YYYY H:MM:SS", but tolerate a
# couple of close variants in case the sheet's locale/format differs.
_TIMESTAMP_FORMATS = (
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
)


def get_riders_for_sunday(sunday_date: str, include_non_shuttle: bool = False) -> list[dict]:
    """Return riders who signed up for a given Sunday.

    Reads every row of the "Form Responses 1" tab and keeps only rows
    whose timestamp falls within this Sunday's signup window (the
    previous Sunday at 10:00 AM through the preceding Saturday at
    6:00 PM).

    Args:
        sunday_date: The Sunday date to fetch signups for, in ISO
            "YYYY-MM-DD" format, e.g. "2026-08-23".
        include_non_shuttle: If False (default), only riders whose
            campus stop maps to a real shuttle are returned (identical
            to the original behavior). If True, every rider in the
            signup window is returned - shuttle riders get their normal
            "shuttle_id", while riders whose stop isn't a serviced stop
            (e.g. "Other", or any free-text entry) get "shuttle_id": None
            and "stop" set to whatever they typed in the Campus Address
            field.

    Returns:
        list[dict]: One dict per valid signup, each with "name", "email"
            (or None), "phone", "stop", "shuttle_id" (str or None),
            "grade", and "submitted_at" (ISO timestamp string).

    Raises:
        RuntimeError: If sunday_date is invalid or the sheet can't be read.
    """
    try:
        window_start, window_end = _get_signup_window(sunday_date)
    except ValueError as exc:
        raise RuntimeError(f"Invalid sunday_date={sunday_date!r}: {exc}") from exc

    try:
        service = get_sheet_client()
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=settings.RIDER_SHEET_ID, range=FORM_RESPONSES_TAB)
            .execute()
        )
        rows = result.get("values", [])
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read '{FORM_RESPONSES_TAB}' tab: {exc}"
        ) from exc

    if not rows:
        return []

    riders = []
    for row in rows[1:]:  # row 0 is the header
        timestamp_raw = _cell(row, _TIMESTAMP_COL)
        if not timestamp_raw:
            continue

        submitted_at = _parse_timestamp(timestamp_raw)
        if submitted_at is None:
            logger.warning(
                "Skipping rider signup row with unparseable timestamp: %r",
                timestamp_raw,
            )
            continue

        if not (window_start <= submitted_at <= window_end):
            continue

        stop = _cell(row, _STOP_COL)
        shuttle_id = STOP_TO_SHUTTLE.get(stop)
        if shuttle_id is None and not include_non_shuttle:
            # Not a serviced stop (e.g. "Other") - ignore this signup.
            continue

        riders.append(
            {
                "name": _cell(row, _NAME_COL),
                "email": _cell(row, _EMAIL_COL) or None,
                "phone": _cell(row, _PHONE_COL),
                "stop": stop,
                "shuttle_id": shuttle_id,
                "grade": _cell(row, _GRADE_COL),
                "submitted_at": submitted_at.isoformat(),
            }
        )

    return riders


def get_all_riders_for_sunday(sunday_date: str) -> dict:
    """Return every signup for a Sunday, split into shuttle/non-shuttle groups.

    Args:
        sunday_date: The Sunday date to fetch signups for, in ISO
            "YYYY-MM-DD" format.

    Returns:
        dict: {
            "shuttle_riders": list[dict] - riders with a real shuttle_id,
            "non_shuttle_riders": list[dict] - riders with shuttle_id None,
            "total": int,
            "shuttle_total": int,
            "non_shuttle_total": int,
        }

    Raises:
        RuntimeError: If reading the rider signups fails.
    """
    try:
        riders = get_riders_for_sunday(sunday_date, include_non_shuttle=True)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to get all riders for sunday_date={sunday_date!r}: {exc}"
        ) from exc

    shuttle_riders = [rider for rider in riders if rider["shuttle_id"] is not None]
    non_shuttle_riders = [rider for rider in riders if rider["shuttle_id"] is None]

    return {
        "shuttle_riders": shuttle_riders,
        "non_shuttle_riders": non_shuttle_riders,
        "total": len(riders),
        "shuttle_total": len(shuttle_riders),
        "non_shuttle_total": len(non_shuttle_riders),
    }


def get_rider_counts(sunday_date: str) -> dict:
    """Return rider counts grouped by shuttle and stop for a given Sunday.

    Args:
        sunday_date: The Sunday date to count signups for, in ISO
            "YYYY-MM-DD" format.

    Returns:
        dict: One key per shuttle_id, each mapping to
            {"total": int, "stops": {stop_name: int, ...}} (every stop
            that shuttle serves is included, even with a count of 0),
            plus a top-level "grand_total" across all shuttles.

    Raises:
        RuntimeError: If reading the rider signups fails.
    """
    try:
        riders = get_riders_for_sunday(sunday_date)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to get rider counts for sunday_date={sunday_date!r}: {exc}"
        ) from exc

    # Pre-populate every known shuttle/stop combination with 0 so the
    # result always has a consistent shape, even for stops with no
    # signups yet.
    stops_by_shuttle: dict[str, list[str]] = {}
    for stop, shuttle_id in STOP_TO_SHUTTLE.items():
        stops_by_shuttle.setdefault(shuttle_id, []).append(stop)

    counts: dict = {
        shuttle_id: {"total": 0, "stops": {stop: 0 for stop in stops}}
        for shuttle_id, stops in stops_by_shuttle.items()
    }

    for rider in riders:
        shuttle_id = rider["shuttle_id"]
        stop = rider["stop"]
        counts[shuttle_id]["total"] += 1
        counts[shuttle_id]["stops"][stop] = counts[shuttle_id]["stops"].get(stop, 0) + 1

    counts["grand_total"] = len(riders)
    return counts


def is_shuttle_full(shuttle_id: str, sunday_date: str) -> bool:
    """Check whether a shuttle has reached its rider capacity.

    Args:
        shuttle_id: The shuttle to check, e.g. "shuttle_1".
        sunday_date: The Sunday date to check, in ISO "YYYY-MM-DD" format.

    Returns:
        bool: True if that shuttle has MAX_RIDERS_PER_SHUTTLE (14) or
            more riders signed up.

    Raises:
        RuntimeError: If reading rider counts fails, or shuttle_id isn't
            a known shuttle.
    """
    try:
        counts = get_rider_counts(sunday_date)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to check capacity for shuttle_id={shuttle_id!r}, "
            f"sunday_date={sunday_date!r}: {exc}"
        ) from exc

    shuttle_counts = counts.get(shuttle_id)
    if shuttle_counts is None:
        raise RuntimeError(
            f"Unknown shuttle_id={shuttle_id!r}; expected one of "
            f"{sorted(set(STOP_TO_SHUTTLE.values()))}."
        )

    return shuttle_counts["total"] >= MAX_RIDERS_PER_SHUTTLE


def get_next_sunday_date() -> str:
    """Return the date of the next upcoming Sunday.

    If today is already Sunday, returns today's date rather than the
    Sunday a week from now.

    Returns:
        str: The date in "YYYY-MM-DD" format.
    """
    today = date.today()

    # date.weekday(): Monday=0 ... Sunday=6. This computes how many days
    # to add to reach the next Sunday, treating "0 days away" (today is
    # Sunday) as 0 rather than wrapping to 7.
    days_until_sunday = (6 - today.weekday()) % 7
    next_sunday = today + timedelta(days=days_until_sunday)

    return next_sunday.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# Small parsing helpers
# --------------------------------------------------------------------------
def _get_signup_window(sunday_date: str) -> tuple[datetime, datetime]:
    """Compute the valid rider signup window for a given Sunday.

    The window runs from the previous Sunday at 10:00 AM through the
    Saturday immediately before sunday_date at 6:00 PM.

    Args:
        sunday_date: The Sunday date to compute the window for, in ISO
            "YYYY-MM-DD" format.

    Returns:
        tuple[datetime, datetime]: (window_start, window_end).

    Raises:
        ValueError: If sunday_date isn't valid "YYYY-MM-DD".
    """
    target_date = datetime.strptime(sunday_date, "%Y-%m-%d").date()
    previous_sunday = target_date - timedelta(days=7)
    saturday_before = target_date - timedelta(days=1)

    window_start = datetime.combine(previous_sunday, time(10, 0))
    window_end = datetime.combine(saturday_before, time(18, 0))

    return window_start, window_end


def _parse_timestamp(raw: str) -> datetime | None:
    """Parse a Google Forms timestamp cell into a datetime.

    Args:
        raw: The raw timestamp string as returned by the Sheets API.

    Returns:
        datetime or None: The parsed timestamp, or None if it doesn't
            match any known format.
    """
    raw = raw.strip()
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _cell(row: list[str], index: int) -> str:
    """Safely read a cell from a sheet row by column index.

    Args:
        row: The row (list of cell values) as returned by the Sheets API.
        index: The column index to read.

    Returns:
        str: The cell's value, stripped of whitespace, or "" if the row
            is too short to have that column.
    """
    if index < 0 or index >= len(row):
        return ""
    return str(row[index]).strip()
