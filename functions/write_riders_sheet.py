# The only module in this system that writes to a Google Sheet.
#
# Everything else reads (functions/read_sheets.py and
# functions/read_riders_sheet.py both authenticate with
# spreadsheets.readonly). This module is deliberately separate rather
# than a function added to one of those, so the broader read/write scope
# is held by exactly one file and a reader can see at a glance that
# nothing else can modify the sheet.
#
# What it writes is a flag appended to a signup row's campus address
# cell, following a convention the Apps Script already established:
# "FAR/duplicate" for a repeat submission, "FAR/driver" when the
# shuttles were full. This adds "FAR/cancelled" when a rider texts SKIP.
#
# The flag does real work beyond record keeping. A flagged cell no
# longer matches any known stop, so read_riders_sheet drops that rider
# from every shuttle list automatically. Cancellations are also recorded
# in Firestore, which is the authoritative copy: this sheet is owned
# outside this system and a write here can fail for reasons we don't
# control, so nothing may depend on this having succeeded.

from __future__ import annotations

import logging

import google.auth
from googleapiclient.discovery import build

from config import settings
from functions.read_riders_sheet import (
    FORM_RESPONSES_TAB,
    column_letter,
    resolve_columns,
)

logger = logging.getLogger(__name__)

# Read/write, unlike the read modules. Note that on Cloud Run with the
# default compute service account the token's scope is decided by the
# runtime rather than by this request, so this is a statement of intent
# as much as a grant. The access that actually matters is the Editor
# permission the service account holds on the spreadsheet itself.
_SHEETS_RW_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# Appended after a "/" to the address cell. Matches the vocabulary the
# Apps Script uses so all three flags read the same way to a human
# scanning the sheet.
CANCELLED_FLAG = "cancelled"

_sheets_service = None


def get_writable_sheet_client():
    """Return the shared read/write Sheets client, creating it on first use.

    Separate from read_sheets.get_sheet_client() and its read-only
    credentials on purpose, so a bug in a read path cannot write.

    Raises:
        RuntimeError: If the client cannot be initialized.
    """
    global _sheets_service

    if _sheets_service is None:
        try:
            credentials, _ = google.auth.default(scopes=[_SHEETS_RW_SCOPE])
            _sheets_service = build("sheets", "v4", credentials=credentials)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to initialize writable Google Sheets client: {exc}"
            ) from exc

    return _sheets_service


def flag_signup_cancelled(row_number: int) -> dict:
    """Append "/cancelled" to a signup row's address cell.

    Reads the cell first rather than blindly overwriting, both to avoid
    clobbering an existing "/duplicate" or "/driver" flag and to make a
    repeat SKIP a no-op instead of producing "FAR/cancelled/cancelled".

    Args:
        row_number: The 1-indexed sheet row, from
            read_riders_sheet.find_signup_row_for_phone().

    Returns:
        dict: {"status": "flagged"|"already_flagged", "cell": str,
            "value": str} describing what the cell now holds.

    Raises:
        RuntimeError: If the read or write fails. Callers should treat
            this as recoverable and rely on the Firestore record, which
            is what every count actually reads.
    """
    try:
        service = get_writable_sheet_client()
        header = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=settings.RIDER_SHEET_ID, range=f"{FORM_RESPONSES_TAB}!1:1")
            .execute()
            .get("values", [[]])[0]
        )
        # The address column is found by its title, so a reordered form
        # can't send the flag to the wrong cell.
        column = column_letter(resolve_columns(header)["stop"])
    except Exception as exc:
        raise RuntimeError(f"Failed to find the address column: {exc}") from exc

    cell = f"{FORM_RESPONSES_TAB}!{column}{row_number}"

    try:
        current = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=settings.RIDER_SHEET_ID, range=cell)
            .execute()
        )
        values = current.get("values", [[]])
        existing = values[0][0] if values and values[0] else ""
    except Exception as exc:
        raise RuntimeError(f"Failed to read cell {cell}: {exc}") from exc

    if existing.strip().lower().endswith(f"/{CANCELLED_FLAG}"):
        logger.info("Row %s already flagged cancelled; leaving it alone.", row_number)
        return {"status": "already_flagged", "cell": cell, "value": existing}

    updated = f"{existing}/{CANCELLED_FLAG}"

    try:
        service.spreadsheets().values().update(
            spreadsheetId=settings.RIDER_SHEET_ID,
            range=cell,
            valueInputOption="RAW",
            body={"values": [[updated]]},
        ).execute()
    except Exception as exc:
        raise RuntimeError(f"Failed to write cell {cell}: {exc}") from exc

    logger.info("Flagged row %s cancelled: %r -> %r", row_number, existing, updated)
    return {"status": "flagged", "cell": cell, "value": updated}


def check_write_access() -> dict:
    """Confirm the service account can actually write to the rider sheet.

    Exists because the credentials, the scope and the spreadsheet's own
    sharing settings are three separate things, any one of which can be
    wrong, and the failure otherwise surfaces at 9:30 on a Saturday
    night when a rider tries to cancel. Writes a cell's existing value
    back over itself, so it changes nothing even on success.

    Returns:
        dict: {"ok": bool, "detail": str}.
    """
    cell = f"{FORM_RESPONSES_TAB}!A1"
    try:
        service = get_writable_sheet_client()
        current = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=settings.RIDER_SHEET_ID, range=cell)
            .execute()
        )
        values = current.get("values", [[""]])
        existing = values[0][0] if values and values[0] else ""

        service.spreadsheets().values().update(
            spreadsheetId=settings.RIDER_SHEET_ID,
            range=cell,
            valueInputOption="RAW",
            body={"values": [[existing]]},
        ).execute()
        return {"ok": True, "detail": f"Wrote {cell} unchanged ({existing!r})."}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}
