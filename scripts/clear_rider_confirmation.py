# Deletes a rider's signup-confirmation record so a test signup can be
# re-run. Nothing in production calls this; it exists because the
# confirmation flow deliberately refuses to text the same number twice
# for the same Sunday, which makes repeat testing look broken when it
# is actually working.
#
# The record lives in Firestore, not the sheet, so deleting the form
# response row does NOT reset it. That is the whole reason for this
# script.
#
# Usage, from the repo root with the venv active:
#
#   python -m scripts.clear_rider_confirmation 7034010571
#   python -m scripts.clear_rider_confirmation 7034010571 --date 2026-09-27
#   python -m scripts.clear_rider_confirmation --list
#
# An admin on the settings.ADMIN_SMS_PHONES allowlist can do the same
# thing for their own number by texting RESETME to the church line.

from __future__ import annotations

import argparse
import sys

from db.firestore_client import (
    RIDER_CONFIRMATIONS_COLLECTION,
    clear_rider_confirmation,
    get_client,
)
from functions.read_riders_sheet import get_next_sunday_date
from functions.send_sms import normalize_to_e164


def list_confirmations() -> int:
    """Print every confirmation record currently on file."""
    docs = list(get_client().collection(RIDER_CONFIRMATIONS_COLLECTION).stream())
    if not docs:
        print("No confirmation records on file.")
        return 0

    print(f"{len(docs)} confirmation record(s):")
    for doc in sorted(docs, key=lambda d: d.id):
        data = doc.to_dict() or {}
        name = data.get("name") or data.get("details", {}).get("name") or ""
        print(f"  {doc.id}  {name}")
    return 0


def clear(phone: str, sunday_date: str) -> int:
    """Delete one confirmation record, reporting whether it existed."""
    try:
        normalized = normalize_to_e164(phone)
    except ValueError as exc:
        print(f"Bad phone number: {exc}", file=sys.stderr)
        return 1

    doc_id = f"{normalized}_{sunday_date}"

    if not clear_rider_confirmation(normalized, sunday_date):
        print(f"No record for {doc_id}; nothing to clear.")
        return 0

    print(f"Deleted {doc_id}. That number can be confirmed again for {sunday_date}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phone", nargs="?", help="Phone number, any format.")
    parser.add_argument(
        "--date",
        help='Sunday in "YYYY-MM-DD" form. Defaults to the next upcoming Sunday.',
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List every confirmation record instead of deleting one.",
    )
    args = parser.parse_args()

    if args.list:
        return list_confirmations()

    if not args.phone:
        parser.error("a phone number is required unless --list is given")

    return clear(args.phone, args.date or get_next_sunday_date())


if __name__ == "__main__":
    raise SystemExit(main())
