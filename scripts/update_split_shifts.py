# One-time migration script that adds split-shift pickup/return driver
# fields to every document in Firestore's "semester_schedule"
# collection. Run this once, then it can be deleted/archived - it's
# not called by any agent, trigger, or scheduled job.
#
# Adds "shuttle_1_pickup"/"shuttle_1_return" and
# "shuttle_2_pickup"/"shuttle_2_return" to every semester_schedule
# document. For most weeks these just mirror the existing single
# "shuttle_1"/"shuttle_2" driver name (the same person covers both the
# pickup and return legs that week). A handful of weeks have a genuine
# split shift where a different driver covers the return leg - those
# are hardcoded in SPLIT_SHIFT_OVERRIDES below. The original
# "shuttle_1"/"shuttle_2" fields are left untouched for backward
# compatibility with any code that hasn't been updated to read the new
# split fields yet.

from __future__ import annotations

import logging

from db.firestore_client import SEMESTER_SCHEDULE_COLLECTION, get_client

logger = logging.getLogger(__name__)

# Sunday date -> {"shuttle_N_pickup"/"shuttle_N_return": driver_name}
# for the specific shuttle/leg combinations that differ from the
# existing single-driver "shuttle_N" value for that week. Any
# shuttle/leg not listed here for a given date just gets
# shuttle_N_pickup = shuttle_N_return = the existing shuttle_N value.
SPLIT_SHIFT_OVERRIDES: dict[str, dict[str, str]] = {
    "2026-09-27": {"shuttle_1_pickup": "Ryan Bielak", "shuttle_1_return": "Sangwoo Suk"},
    "2026-10-11": {"shuttle_1_pickup": "Ryan Bielak", "shuttle_1_return": "Peter Hahn"},
    "2026-11-01": {"shuttle_1_pickup": "Ryan Bielak", "shuttle_1_return": "Albert Lee"},
    "2026-12-13": {"shuttle_1_pickup": "Ryan Bielak", "shuttle_1_return": "Yong Wook Kim"},
}


def update_split_shifts() -> dict:
    """Add shuttle_1/2_pickup and shuttle_1/2_return fields to every
    semester_schedule document.

    For each document, "shuttle_N_pickup" and "shuttle_N_return"
    default to that document's existing "shuttle_N" value, then get
    overridden per SPLIT_SHIFT_OVERRIDES (matched by the document's
    "date" field) for the weeks with an actual split shift. Existing
    fields (including "shuttle_1"/"shuttle_2" themselves) are left
    alone - this only adds the four new fields.

    Safe to re-run: each run just overwrites the same four fields with
    the same computed values, so running it twice has no extra effect.

    Returns:
        dict: {"updated": int, "errors": list[dict]}. "updated" is the
            number of documents successfully written; "errors" holds
            {"date": str, "error": str} for any documents that failed.

    Raises:
        RuntimeError: If the semester_schedule collection can't be read.
    """
    client = get_client()

    try:
        docs = list(client.collection(SEMESTER_SCHEDULE_COLLECTION).stream())
    except Exception as exc:
        raise RuntimeError(f"Failed to read semester_schedule collection: {exc}") from exc

    updated = 0
    errors: list[dict] = []

    for doc in docs:
        data = doc.to_dict() or {}
        sunday_date = data.get("date", doc.id)
        overrides = SPLIT_SHIFT_OVERRIDES.get(sunday_date, {})

        update_fields: dict[str, str | None] = {}
        for shuttle_id in ("shuttle_1", "shuttle_2"):
            base_value = data.get(shuttle_id)
            update_fields[f"{shuttle_id}_pickup"] = overrides.get(f"{shuttle_id}_pickup", base_value)
            update_fields[f"{shuttle_id}_return"] = overrides.get(f"{shuttle_id}_return", base_value)

        try:
            doc.reference.update(update_fields)
            updated += 1
            logger.info("Updated %s (%s): %s", doc.id, sunday_date, update_fields)
        except Exception as exc:
            logger.error("Failed to update %s (%s): %s", doc.id, sunday_date, exc)
            errors.append({"date": sunday_date, "error": str(exc)})

    return {"updated": updated, "errors": errors}


def main() -> None:
    """Run the migration locally against the real semester_schedule collection."""
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    print("Updating semester_schedule documents with split-shift pickup/return fields...")
    result = update_split_shifts()
    print(result)


if __name__ == "__main__":
    main()
