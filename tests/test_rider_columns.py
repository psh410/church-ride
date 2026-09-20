# Pins down that a rider's name and grade are read by header, not by
# position. The live form has Grade in column B and Full Name in column C,
# but the code assumed the reverse, which labelled every rider in the
# admin email with their year (Freshman, Sophomore) instead of their name.
#
# Run it directly (no pytest needed), from the repo root:
#
#     python3 tests/test_rider_columns.py
#
# Nothing here touches Sheets, Firestore or the network.

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import functions.read_riders_sheet as sheet_mod

SUNDAY = "2026-09-20"
STOP_MAP = {"FAR": "shuttle_1"}

results: list[tuple[str, bool]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    results.append((label, bool(condition)))
    print(("PASS" if condition else "FAIL"), "-", label, detail if not condition else "")


HEADER_NAME_FIRST = [
    "Timestamp", "Full Name (first + last)", "Grade",
    "Campus Address / Dorm", "Phone Number", "Email",
]
HEADER_GRADE_FIRST = [
    "Timestamp", "Grade", "Full Name (first + last)",
    "Campus Address / Dorm", "Phone Number", "Email",
]


def read(header: list[str], row: list[str]):
    """Run get_riders_for_sunday and get_signup_row against a fake sheet."""
    rows = [header, row]
    service = mock.MagicMock()
    service.spreadsheets().values().get().execute.return_value = {"values": rows}
    with mock.patch.object(sheet_mod, "get_sheet_client", return_value=service), \
         mock.patch.object(sheet_mod, "get_stop_to_shuttle_map", return_value=STOP_MAP), \
         mock.patch.object(sheet_mod, "_get_signup_window") as window, \
         mock.patch.object(sheet_mod, "_drop_cancelled", side_effect=lambda r, d: r):
        from datetime import datetime
        window.return_value = (datetime(2026, 9, 1), datetime(2026, 12, 31))
        riders = sheet_mod.get_riders_for_sunday(SUNDAY)
        single = sheet_mod.get_signup_row(2)
    return riders[0], single


name_first_row = ["9/14/2026 10:00:00", "Peter Hahn", "Freshman", "FAR", "7034010571", "p@example.com"]
grade_first_row = ["9/14/2026 10:00:00", "Freshman", "Peter Hahn", "FAR", "7034010571", "p@example.com"]

for label, header, row in (
    ("name in column B", HEADER_NAME_FIRST, name_first_row),
    ("grade in column B", HEADER_GRADE_FIRST, grade_first_row),
):
    rider, single = read(header, row)
    check(f"{label}: signup list has the name", rider["name"] == "Peter Hahn", rider["name"])
    check(f"{label}: signup list has the grade", rider["grade"] == "Freshman", rider["grade"])
    check(f"{label}: single row has the name", single["name"] == "Peter Hahn", single["name"])
    check(f"{label}: single row has the grade", single["grade"] == "Freshman", single["grade"])

# Headers Google has reworded still match on their leading words.
odd = list(HEADER_GRADE_FIRST)
odd[1] = "  GRADE (current year)"
odd[2] = "Full name"
rider, _ = read(odd, grade_first_row)
check("reworded headers still resolve", rider["name"] == "Peter Hahn", rider["name"])

# A required header that can't be found fails loudly instead of guessing.
missing = ["Timestamp", "Who", "Year", "Campus Address / Dorm", "Phone Number", "Email"]
try:
    read(missing, grade_first_row)
    raised = False
except sheet_mod.SheetLayoutError as exc:
    raised = "Full Name" in str(exc) or "full name" in str(exc).lower()
check("missing required header raises SheetLayoutError", raised)

# resolve_columns directly.
cols = sheet_mod.resolve_columns(HEADER_GRADE_FIRST)
check("resolve_columns maps every field",
      cols["timestamp"] == 0 and cols["grade"] == 1 and cols["name"] == 2
      and cols["stop"] == 3 and cols["phone"] == 4 and cols["email"] == 5, str(cols))
check("driver column is optional (-1)", cols["driver"] == -1, str(cols))
cols = sheet_mod.resolve_columns(HEADER_GRADE_FIRST + ["Small Group", "Driver"])
check("driver column found by title anywhere", cols.get("driver") == 7, str(cols))

# Reordered columns still work.
shuffled = ["Email", "Phone Number", "Campus Address / Dorm", "Full Name (first + last)", "Grade", "Timestamp"]
cols = sheet_mod.resolve_columns(shuffled)
check("shuffled columns resolve", cols["name"] == 3 and cols["timestamp"] == 5, str(cols))

# Two fields cannot share one column.
with mock.patch.dict(sheet_mod._COLUMN_SPECS, {"grade": ("full name", False, False)}):
    try:
        sheet_mod.resolve_columns(HEADER_GRADE_FIRST)
        dup = False
    except sheet_mod.SheetLayoutError:
        dup = True
check("one column mapped to two fields raises", dup)

# Name column full of grades is caught.
rows = [["a"] * 6] + [["t", "Peter", "Freshman", "FAR", "1", "e"]] * 5
c = sheet_mod.resolve_columns(HEADER_NAME_FIRST)
try:
    sheet_mod.check_name_column([HEADER_NAME_FIRST] + [["t", "Freshman", "Peter", "FAR", "1", "e"]] * 5, c)
    caught = False
except sheet_mod.SheetLayoutError:
    caught = True
check("name column full of grades raises", caught)
try:
    sheet_mod.check_name_column([HEADER_NAME_FIRST] + [["t", "Peter", "Freshman", "FAR", "1", "e"]] * 5, c)
    ok_names = True
except sheet_mod.SheetLayoutError:
    ok_names = False
check("normal names pass the name check", ok_names)

# Diagnostic helper.
def diag(header, row):
    service = mock.MagicMock()
    service.spreadsheets().values().get().execute.return_value = {"values": [header, row]}
    with mock.patch.object(sheet_mod, "get_sheet_client", return_value=service):
        return sheet_mod.check_sheet_headers()
good = diag(HEADER_GRADE_FIRST + ["Small Group", "SMS Consent (Optional)"], grade_first_row + ["", "Yes"])
check("check_sheet_headers ok on good sheet", good["ok"], str(good))
bad = diag(missing, grade_first_row)
check("check_sheet_headers reports problems on bad sheet", (not bad["ok"]) and bad["problems"], str(bad))

# Cancel-flagging finds the address column by header.
import functions.write_riders_sheet as writer
service = mock.MagicMock()
service.spreadsheets().values().get().execute.return_value = {"values": [shuffled]}
check("column_letter A/Z/AA", (sheet_mod.column_letter(0), sheet_mod.column_letter(25), sheet_mod.column_letter(26)) == ("A", "Z", "AA"))

print()
failed = [label for label, ok in results if not ok]
if failed:
    print(f"{len(failed)} FAILED: {failed}")
    sys.exit(1)
print(f"All {len(results)} checks passed.")
