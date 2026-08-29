# Reads driver and route data directly from Google Sheets via the Sheets API.
#
# All driver/route data lives in Google Sheets (maintained by hand and via
# a Google Form), not Firestore. This module is the single connection
# point for that data. Driving history is the one exception - it comes
# from the Firestore "assignments" collection via db.firestore_client, so
# this module cross-references both sources for get_driver_history()/
# get_all_drivers_with_history().

from __future__ import annotations

import logging
from datetime import date, datetime

import google.auth
from googleapiclient.discovery import build

from config import settings
from db.firestore_client import get_assignment

logger = logging.getLogger(__name__)

# Read-only scope is sufficient - this module never writes to Sheets.
_SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"

# Tab (sheet) names within the SHEETS_ID spreadsheet.
AVAILABLE_DRIVERS_TAB = "Available Drivers"
FORM_RESPONSES_TAB = "Form Responses 1"
ROUTES_TAB = "Routes"

# Reference departure time for each shuttle, used to pick a sane value
# when the Routes tab has inconsistent departure_time entries across a
# shuttle's stop-rows (see _resolve_departure_time()).
_DEPARTURE_TIME_REFERENCE = {
    "shuttle_1": "11:30 AM",
    "shuttle_2": "11:50 AM",
}

# --------------------------------------------------------------------------
# Client initialization
# --------------------------------------------------------------------------
# A single Sheets API client is created lazily and reused by every
# function in this module, the same pattern db/firestore_client.py uses
# for its Firestore client.
_sheets_service = None


def get_sheet_client():
    """Return the shared Google Sheets API client, creating it on first use.

    Returns:
        The Sheets API service object (googleapiclient Resource) used to
        read spreadsheet data.

    Raises:
        RuntimeError: If the Sheets client cannot be initialized (e.g.
            missing/invalid Google Cloud credentials).
    """
    global _sheets_service

    if _sheets_service is None:
        try:
            credentials, _ = google.auth.default(scopes=[_SHEETS_READONLY_SCOPE])
            _sheets_service = build("sheets", "v4", credentials=credentials)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to initialize Google Sheets client: {exc}"
            ) from exc

    return _sheets_service


# --------------------------------------------------------------------------
# Available Drivers + Form Responses
# --------------------------------------------------------------------------
def get_available_drivers(sunday_date: str) -> list[dict]:
    """Return full driver details for everyone available on a given Sunday.

    Reads the row in the "Available Drivers" tab whose date (column A)
    matches sunday_date - the driver names listed in columns B onward on
    that row are cross-referenced against the "Form Responses 1" tab to
    pull each driver's full contact/preference details.

    Args:
        sunday_date: The Sunday date to look up, formatted to match the
            sheet, e.g. "8/23/26".

    Returns:
        list[dict]: One dict per available driver with keys "name",
            "email", "phone", "age_range", "conflict_dates",
            "additional_comments", and "shift" (one of "Both",
            "Pickup", or "Drop-off" - defaults to "Both" if the driver
            didn't answer that question). Empty list if no row matches
            sunday_date or no drivers are listed for it.

    Raises:
        RuntimeError: If either sheet tab can't be read.
    """
    try:
        service = get_sheet_client()
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=settings.SHEETS_ID, range=AVAILABLE_DRIVERS_TAB)
            .execute()
        )
        rows = result.get("values", [])
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read '{AVAILABLE_DRIVERS_TAB}' tab: {exc}"
        ) from exc

    matching_row = next(
        (row for row in rows if row and row[0].strip() == sunday_date), None
    )
    if matching_row is None:
        logger.info("No 'Available Drivers' row found for sunday_date=%s.", sunday_date)
        return []

    driver_names = [name.strip() for name in matching_row[1:] if name and name.strip()]
    if not driver_names:
        return []

    form_responses = _get_form_responses()

    drivers = []
    for name in driver_names:
        details = form_responses.get(name)
        if details is None:
            # Listed as available but has no matching form submission -
            # still include them with whatever we know (just their name).
            logger.warning(
                "Driver %r is listed as available but has no matching "
                "'%s' entry.",
                name,
                FORM_RESPONSES_TAB,
            )
            details = {
                "name": name,
                "email": None,
                "phone": None,
                "age_range": None,
                "conflict_dates": [],
                "additional_comments": None,
            }
        drivers.append(details)

    return drivers


def _get_form_responses() -> dict[str, dict]:
    """Read the Form Responses 1 tab and index driver details by name.

    Expected columns: Timestamp, Full Name, Email Address, Phone Number,
    Conflict Dates, Additional Comments, Age Range, Shift.

    Returns:
        dict[str, dict]: Maps each driver's full name to their details
            dict (see get_available_drivers() for the keys).

    Raises:
        RuntimeError: If the tab can't be read.
    """
    try:
        service = get_sheet_client()
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=settings.SHEETS_ID, range=FORM_RESPONSES_TAB)
            .execute()
        )
        rows = result.get("values", [])
    except Exception as exc:
        raise RuntimeError(f"Failed to read '{FORM_RESPONSES_TAB}' tab: {exc}") from exc

    if not rows:
        return {}

    header = rows[0]
    name_idx = _find_column_index(header, "Full Name")
    email_idx = _find_column_index(header, "Email Address")
    phone_idx = _find_column_index(header, "Phone Number")
    conflict_dates_idx = _find_column_index(header, "Conflict Dates")
    comments_idx = _find_column_index(header, "Additional Comments")
    age_range_idx = _find_column_index(header, "Age Range")
    shift_idx = _find_column_index(header, "Shift")

    responses: dict[str, dict] = {}
    for row in rows[1:]:
        name = _cell(row, name_idx)
        if not name:
            continue

        conflict_dates_raw = _cell(row, conflict_dates_idx)
        conflict_dates = (
            [d.strip() for d in conflict_dates_raw.split(",") if d.strip()]
            if conflict_dates_raw
            else []
        )

        responses[name] = {
            "name": name,
            "email": _cell(row, email_idx) or None,
            "phone": _cell(row, phone_idx) or None,
            "age_range": _cell(row, age_range_idx) or None,
            "conflict_dates": conflict_dates,
            "additional_comments": _cell(row, comments_idx) or None,
            "shift": _cell(row, shift_idx) or "Both",
        }

    return responses


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
def get_routes() -> list[dict]:
    """Return all routes (shuttles) and their stops from the Routes tab.

    Row 1 is headers (shuttle_id, shuttle_name, van, stop_name,
    pickup_time, departure_time); data starts on row 2. Rows sharing the
    same shuttle_id are grouped into a single route dict.
    departure_time is a per-shuttle value, not per-stop, but the sheet
    repeats it on every one of that shuttle's rows - see
    _resolve_departure_time() for how a single value is picked if those
    repeated entries don't all agree.

    Returns:
        list[dict]: One dict per shuttle_id, in the order first seen:
            {"shuttle_id": str, "shuttle_name": str, "van": str,
            "departure_time": str,
            "stops": [{"stop_name": str, "pickup_time": str}, ...]}.

    Raises:
        RuntimeError: If the tab can't be read.
    """
    try:
        service = get_sheet_client()
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=settings.SHEETS_ID, range=ROUTES_TAB)
            .execute()
        )
        rows = result.get("values", [])
    except Exception as exc:
        raise RuntimeError(f"Failed to read '{ROUTES_TAB}' tab: {exc}") from exc

    if not rows:
        return []

    header = rows[0]
    shuttle_id_idx = _find_column_index(header, "shuttle_id")
    shuttle_name_idx = _find_column_index(header, "shuttle_name")
    van_idx = _find_column_index(header, "van")
    stop_name_idx = _find_column_index(header, "stop_name")
    pickup_time_idx = _find_column_index(header, "pickup_time")
    departure_time_idx = _find_column_index(header, "departure_time")

    routes_by_id: dict[str, dict] = {}
    departure_times_by_shuttle: dict[str, list[str]] = {}

    for row in rows[1:]:  # data starts row 2
        shuttle_id = _cell(row, shuttle_id_idx)
        if not shuttle_id:
            continue

        if shuttle_id not in routes_by_id:
            routes_by_id[shuttle_id] = {
                "shuttle_id": shuttle_id,
                "shuttle_name": _cell(row, shuttle_name_idx),
                "van": _cell(row, van_idx),
                "departure_time": "",  # filled in below once all rows are seen
                "stops": [],
            }

        stop_name = _cell(row, stop_name_idx)
        if stop_name:
            routes_by_id[shuttle_id]["stops"].append(
                {
                    "stop_name": stop_name,
                    "pickup_time": _cell(row, pickup_time_idx),
                }
            )

        departure_time = _cell(row, departure_time_idx)
        if departure_time:
            departure_times_by_shuttle.setdefault(shuttle_id, []).append(departure_time)

    for shuttle_id, route in routes_by_id.items():
        route["departure_time"] = _resolve_departure_time(
            shuttle_id, departure_times_by_shuttle.get(shuttle_id, [])
        )

    # Dicts preserve insertion order in Python 3.7+, so this naturally
    # returns routes in the order their shuttle_id first appeared.
    return list(routes_by_id.values())


def _resolve_departure_time(shuttle_id: str, values: list[str]) -> str:
    """Pick one departure_time value for a shuttle from its raw sheet rows.

    departure_time applies to the whole shuttle, but the Routes tab
    repeats it on every stop-row for that shuttle_id, so sheet editors
    can accidentally enter different values for the same shuttle. This
    resolves that to a single value:
    - If every collected value is identical, that value is used as-is.
    - If they differ, the value closest to that shuttle's reference
      time (_DEPARTURE_TIME_REFERENCE) is used - "closest" meaning the
      smallest absolute difference in minutes since midnight - and a
      warning is logged so the inconsistency can be fixed in the sheet.

    Args:
        shuttle_id: The shuttle these departure_time values belong to.
        values: Every non-empty departure_time cell value collected
            across that shuttle's rows, in row order. May be empty if
            the column was blank for every row.

    Returns:
        str: The resolved departure_time, or "" if values is empty.
    """
    if not values:
        return ""

    unique_values = set(values)
    if len(unique_values) == 1:
        return values[0]

    logger.warning(
        "Routes tab has inconsistent departure_time values for "
        "shuttle_id=%r: %s. Using the value closest to the reference "
        "time instead - please fix the sheet.",
        shuttle_id,
        sorted(unique_values),
    )

    reference = _DEPARTURE_TIME_REFERENCE.get(shuttle_id)
    if reference is None:
        # No reference time defined for this shuttle - fall back to the
        # first value seen rather than guessing which one is "right".
        return values[0]

    try:
        reference_minutes = _time_to_minutes(reference)
    except ValueError:
        return values[0]

    parsed_values: list[tuple[str, int]] = []
    for value in values:
        try:
            parsed_values.append((value, _time_to_minutes(value)))
        except ValueError:
            logger.warning(
                "Skipping unparseable departure_time value %r for shuttle_id=%r.",
                value,
                shuttle_id,
            )

    if not parsed_values:
        return values[0]

    return min(parsed_values, key=lambda pair: abs(pair[1] - reference_minutes))[0]


def _time_to_minutes(time_str: str) -> int:
    """Convert a "H:MM AM/PM" time string into minutes since midnight.

    Args:
        time_str: A time string like "11:30 AM".

    Returns:
        int: Minutes since midnight (0-1439).

    Raises:
        ValueError: If time_str isn't in "H:MM AM/PM" format.
    """
    parsed = datetime.strptime(time_str.strip(), "%I:%M %p")
    return parsed.hour * 60 + parsed.minute


# --------------------------------------------------------------------------
# Driving history (from Firestore, via db.firestore_client.get_assignment)
# --------------------------------------------------------------------------
def get_driver_history(driver_name: str) -> dict:
    """Return how often a driver has driven this semester.

    get_assignment() only looks assignments up by sunday_date (there's no
    driver-indexed query in Firestore), so this walks every Sunday found
    in the "Available Drivers" tab, calls get_assignment() for each one,
    and keeps the dates where this driver appears.

    Args:
        driver_name: The driver's full name, matching the "driver_name"
            field stored on each assignment document.

    Returns:
        dict: {"times_driven": int, "last_driven_date": str or None,
            "sundays_driven": list[str]} (sundays_driven is sorted
            ascending, in Firestore's "YYYY-MM-DD" format).

    Raises:
        RuntimeError: If reading the semester's Sundays or querying
            assignments fails.
    """
    try:
        semester_sundays = _get_all_semester_sundays()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read semester Sundays while building history for "
            f"driver_name={driver_name!r}: {exc}"
        ) from exc

    sundays_driven = set()
    for sheet_date in semester_sundays:
        try:
            iso_date = _sheet_date_to_iso(sheet_date)
        except ValueError:
            # Not a parseable date - skip (e.g. a stray header cell).
            continue

        try:
            assignments = get_assignment(iso_date)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to get assignments for sunday_date={iso_date!r} "
                f"while building history for driver_name={driver_name!r}: {exc}"
            ) from exc

        if any(assignment.get("driver_name") == driver_name for assignment in assignments):
            sundays_driven.add(iso_date)

    sorted_sundays = sorted(sundays_driven)

    return {
        "times_driven": len(sorted_sundays),
        "last_driven_date": sorted_sundays[-1] if sorted_sundays else None,
        "sundays_driven": sorted_sundays,
    }


def get_all_drivers_with_history(sunday_date: str) -> list[dict]:
    """Return available drivers enriched with driving history, ready for the assignment agent.

    Args:
        sunday_date: The Sunday date to build the enriched driver list
            for, formatted to match the sheet, e.g. "8/23/26".

    Returns:
        list[dict]: Each available driver's details (including "shift",
            one of "Both", "Pickup", or "Drop-off") merged with their
            driving history ("times_driven", "last_driven_date",
            "sundays_driven") and "total_available_sundays_remaining"
            (how many Sundays they're listed as available for, minus how
            many they've already driven).

    Raises:
        RuntimeError: If reading Sheets/Firestore data fails.
    """
    try:
        drivers = get_available_drivers(sunday_date)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to build enriched driver list for "
            f"sunday_date={sunday_date!r}: {exc}"
        ) from exc

    try:
        availability_by_sunday = _get_available_drivers_by_sunday()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read per-Sunday availability while building "
            f"enriched driver list for sunday_date={sunday_date!r}: {exc}"
        ) from exc

    enriched_drivers = []
    for driver in drivers:
        history = get_driver_history(driver["name"])

        # How many semester Sundays (from Available Drivers column A) is
        # this driver listed as available for, minus how many of those
        # they've already driven.
        available_sundays_count = sum(
            1
            for available_names in availability_by_sunday.values()
            if driver["name"] in available_names
        )
        total_available_sundays_remaining = (
            available_sundays_count - history["times_driven"]
        )

        enriched_drivers.append(
            {
                **driver,
                **history,
                "total_available_sundays_remaining": total_available_sundays_remaining,
            }
        )

    return enriched_drivers


def _get_available_drivers_by_sunday() -> dict[str, set[str]]:
    """Read the whole Available Drivers tab into a per-Sunday set of driver names.

    Used to count, for each driver, how many semester Sundays they're
    listed as available for (columns B onward on that Sunday's row).

    Returns:
        dict[str, set[str]]: Maps each Sunday date string (as it appears
            in column A) to the set of driver names listed as available
            that day.

    Raises:
        RuntimeError: If the tab can't be read.
    """
    try:
        service = get_sheet_client()
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=settings.SHEETS_ID, range=AVAILABLE_DRIVERS_TAB)
            .execute()
        )
        rows = result.get("values", [])
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read '{AVAILABLE_DRIVERS_TAB}' tab: {exc}"
        ) from exc

    availability_by_sunday: dict[str, set[str]] = {}
    for row in rows:
        if not row or not row[0].strip():
            continue

        sunday = row[0].strip()
        available_names = {name.strip() for name in row[1:] if name and name.strip()}
        availability_by_sunday[sunday] = available_names

    return availability_by_sunday


# --------------------------------------------------------------------------
# Small parsing helpers shared by the functions above
# --------------------------------------------------------------------------
def _get_all_semester_sundays() -> list[str]:
    """Return every date listed in the Available Drivers tab's column A.

    Returns:
        list[str]: Date strings exactly as they appear in the sheet
            (e.g. "8/23/26"), in row order.

    Raises:
        RuntimeError: If the tab can't be read.
    """
    try:
        service = get_sheet_client()
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=settings.SHEETS_ID, range=f"{AVAILABLE_DRIVERS_TAB}!A:A")
            .execute()
        )
        rows = result.get("values", [])
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read dates from '{AVAILABLE_DRIVERS_TAB}' tab: {exc}"
        ) from exc

    return [row[0].strip() for row in rows if row and row[0].strip()]


def _sheet_date_to_iso(date_str: str) -> str:
    """Convert a sheet date like "8/23/26" into Firestore's "YYYY-MM-DD" format.

    Args:
        date_str: The date string as it appears in the sheet.

    Returns:
        str: The date in ISO "YYYY-MM-DD" format.

    Raises:
        ValueError: If date_str doesn't match the expected M/D/YY format.
    """
    parsed: date = datetime.strptime(date_str, "%m/%d/%y").date()
    return parsed.isoformat()


def _cell(row: list[str], index: int) -> str:
    """Safely read a cell from a sheet row by column index.

    Args:
        row: The row (list of cell values) as returned by the Sheets API.
        index: The column index to read, or -1 if the column wasn't found.

    Returns:
        str: The cell's value, stripped of whitespace, or "" if the
            index is invalid or the row is too short.
    """
    if index < 0 or index >= len(row):
        return ""
    return str(row[index]).strip()


def _find_column_index(header: list[str], column_name: str) -> int:
    """Find a column's index by header name (case-insensitive).

    Args:
        header: The header row (first row) of a sheet tab.
        column_name: The expected header name for this column.

    Returns:
        int: The matching column's index, or -1 if column_name doesn't
            appear in header.
    """
    normalized_header = [str(cell).strip().lower() for cell in header]
    normalized_name = column_name.strip().lower()
    if normalized_name in normalized_header:
        return normalized_header.index(normalized_name)
    return -1
