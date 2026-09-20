# Pins down how riders who need a personal driver are listed in the admin
# update email and the driver email: "Name - Stop - Driver", with
# "Unassigned" when nobody is driving them, and "(shuttle full)" for a
# rider whose shuttle had no seats when they signed up.
#
# Also checks that every send re-reads the Routes tab first, so a route
# edited a moment ago is what goes out.
#
# Run it directly, from the repo root:
#
#     python3 tests/test_personal_driver.py
#
# Nothing here touches Sheets, Firestore, Gmail or the network.

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import functions.read_riders_sheet as rs
import functions.send_weekly_emails as we

results: list[tuple[str, bool]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    results.append((label, bool(condition)))
    print(("PASS" if condition else "FAIL"), "-", label, detail if not condition else "")


HEADER = [
    "Timestamp", "Grade", "Full Name (first + last)", "Campus Address / Dorm",
    "Phone Number", "Email", "Small Group", "SMS Consent (Optional)", "Driver",
]
STOP_MAP = {"FAR": "shuttle_2", "SDRP": "shuttle_1"}


def sheet_rows(*signups):
    return [HEADER, *signups]


def read(rows):
    service = mock.MagicMock()
    service.spreadsheets().values().get().execute.return_value = {"values": rows}
    with mock.patch.object(rs, "get_sheet_client", return_value=service), \
         mock.patch.object(rs, "get_stop_to_shuttle_map", return_value=STOP_MAP), \
         mock.patch.object(rs, "_get_signup_window",
                           return_value=(datetime(2026, 9, 1), datetime(2026, 12, 31))), \
         mock.patch.object(rs, "_drop_cancelled", side_effect=lambda r, d: r):
        return rs.get_riders_for_sunday("2026-09-20", include_non_shuttle=True)


def row(name, stop, driver="", phone="7031110000"):
    return ["9/14/2026 10:00:00", "Freshman", name, stop, phone,
            f"{name.split()[0].lower()}@example.com", "", "", driver]


riders = {r["name"]: r for r in read(sheet_rows(
    row("Sarah Lee", "1002 S Lincoln Ave", "Daniel Kim", "7031110001"),
    row("Justin Kim", "FAR/driver", "", "7031110002"),
    row("Grace Ryoo", "Illini Tower", "", "7031110003"),
    row("Tom Suh", "FAR/driver", "  Ellie   Kim ", "7031110004"),
    row("Sam Park", "Illini Tower", "shuttle 2", "7031110005"),
    row("Amy Cho", "1002 S Lincoln Apt 1/2", "Van", "7031110006"),
    row("Ann Bae", "SDRP", "", "7031110007"),
))}

check("driver name is read from the Driver column", riders["Sarah Lee"]["personal_driver"] == "Daniel Kim")
check("extra spaces in a driver name are trimmed", riders["Tom Suh"]["personal_driver"] == "Ellie Kim",
      riders["Tom Suh"]["personal_driver"])
check("an empty Driver cell means unassigned", riders["Grace Ryoo"]["personal_driver"] == "")
check("a shuttle typed in Driver is not a person", riders["Sam Park"]["personal_driver"] == "")
check("Van typed in Driver is not a person", riders["Amy Cho"]["personal_driver"] == "")
check("a shuttle-full rider keeps the stop they asked for",
      riders["Justin Kim"]["stop_display"] == "FAR", riders["Justin Kim"]["stop_display"])
check("a shuttle-full rider is marked as such", riders["Justin Kim"]["shuttle_full"] is True)
check("shuttle-full riders are not on a shuttle", riders["Justin Kim"]["shuttle_id"] is None)
check("an ordinary rider is not marked shuttle full", riders["Grace Ryoo"]["shuttle_full"] is False)
check("a slash in a real address survives", riders["Amy Cho"]["stop_display"] == "1002 S Lincoln Apt 1/2",
      riders["Amy Cho"]["stop_display"])

# The Driver column is found by title, wherever it sits.
moved = ["Timestamp", "Grade", "Full Name (first + last)", "Campus Address / Dorm",
         "Phone Number", "Email", "Driver", "Small Group"]
got = read([moved, ["9/14/2026 10:00:00", "Freshman", "Kim Lee", "Illini Tower",
                    "7031110009", "k@example.com", "Peter Hahn", ""]])
check("the Driver column is found by title, not position",
      got[0]["personal_driver"] == "Peter Hahn", got[0]["personal_driver"])

no_column = ["Timestamp", "Grade", "Full Name (first + last)", "Campus Address / Dorm",
             "Phone Number", "Email"]
got = read([no_column, ["9/14/2026 10:00:00", "Freshman", "Kim Lee", "Illini Tower",
                        "7031110009", "k@example.com"]])
check("no Driver column at all reads as unassigned", got[0]["personal_driver"] == "")

# ---- The email lines ----
line = we._format_personal_driver_rider
check("assigned line", line(riders["Sarah Lee"]) == "Sarah Lee - 1002 S Lincoln Ave - Daniel Kim",
      line(riders["Sarah Lee"]))
check("shuttle-full line", line(riders["Tom Suh"]) == "Tom Suh - FAR (shuttle full) - Ellie Kim",
      line(riders["Tom Suh"]))
check("unassigned line", line(riders["Grace Ryoo"]) == "Grace Ryoo - Illini Tower - Unassigned",
      line(riders["Grace Ryoo"]))
check("shuttle-full and unassigned", line(riders["Justin Kim"]) == "Justin Kim - FAR (shuttle full) - Unassigned",
      line(riders["Justin Kim"]))

non_shuttle = [r for r in riders.values() if r["shuttle_id"] is None]
all_riders = {
    "total": 7, "shuttle_total": 1, "non_shuttle_total": len(non_shuttle),
    "shuttle_riders": [riders["Ann Bae"]], "non_shuttle_riders": non_shuttle,
}

# ---- Admin update email ----
routes = [
    {"shuttle_id": "shuttle_1", "shuttle_name": "Shuttle 1", "van": "Ford Transit (Gray)",
     "stops": [{"stop_name": "SDRP", "pickup_time": "9:10 AM"}]},
    {"shuttle_id": "shuttle_2", "shuttle_name": "Shuttle 2", "van": "GMC Savanna (Silver)",
     "stops": [{"stop_name": "FAR", "pickup_time": "9:00 AM"}]},
]
with mock.patch.object(we, "get_stop_to_shuttle_map", return_value=STOP_MAP):
    counts = we._build_shuttle_counts(all_riders["shuttle_riders"])
with mock.patch.object(we, "build_static_map_url", return_value="http://map"), \
     mock.patch.object(we, "build_map_legend", return_value="legend"):
    admin = we._build_saturday_summary(
        all_riders, counts, non_shuttle, set(), routes,
        {"FAR": "9:00 AM", "SDRP": "9:10 AM"}, {},
    )
check("admin email lists the driver", "Sarah Lee - 1002 S Lincoln Ave - Daniel Kim" in admin)
check("admin email lists Unassigned", "Grace Ryoo - Illini Tower - Unassigned" in admin)
check("admin email lists a shuttle-full rider", "Justin Kim - FAR (shuttle full) - Unassigned" in admin)
check("admin email never prints the raw /driver flag", "FAR/driver" not in admin)

# ---- Driver email ----
assignments = [{"route_id": "shuttle_1", "pickup_driver": "Dae Kang", "return_driver": "Dae Kang"},
               {"route_id": "shuttle_2", "pickup_driver": "Peter Hahn", "return_driver": "Peter Hahn"}]
body = we._build_saturday_driver_assignment_body(
    "2026-09-20", assignments, all_riders, routes, {"FAR": "9:00 AM", "SDRP": "9:10 AM"})
check("driver email has a personal driver section", "PERSONAL DRIVER RIDES" in body)
check("driver email lists the driver", "Sarah Lee - 1002 S Lincoln Ave - Daniel Kim" in body)
check("driver email lists Unassigned", "Grace Ryoo - Illini Tower - Unassigned" in body)
check("the section comes after the shuttles", body.index("PERSONAL DRIVER RIDES") > body.index("SHUTTLE 2"))
none_body = we._build_saturday_driver_assignment_body(
    "2026-09-20", assignments, {**all_riders, "non_shuttle_riders": []}, routes, {})
check("no personal driver section when nobody needs one", "PERSONAL DRIVER RIDES" not in none_body)

# ---- Every send re-reads the route ----
def calls_clear(fn, *args):
    with mock.patch.object(we, "clear_route_caches") as clear:
        try:
            fn(*args)
        except Exception:
            pass
    return clear.call_count >= 1

check("Wednesday reminder clears the route cache first", calls_clear(we.send_wednesday_reminder, "2026-09-20"))
check("Saturday update clears the route cache first", calls_clear(we.send_saturday_update, "2026-09-20"))
check("driver assignment clears the route cache first", calls_clear(we.send_saturday_driver_assignment, "2026-09-20"))
check("shuttle-full alert clears the route cache first", calls_clear(we.send_shuttle_full_alert, "shuttle_1", "2026-09-20"))

import functions.send_driver_sms_reminder as sms
import functions.rider_reminder as rr
for label, module, fn, args in (
    ("driver text", sms, sms.send_driver_sms_reminders, ()),
    ("rider reminder", rr, rr.send_saturday_rider_reminders, ("2026-09-20", True)),
):
    with mock.patch.object(rs, "clear_route_caches") as clear:
        try:
            fn(*args)
        except Exception:
            pass
    check(f"{label} clears the route cache first", clear.call_count >= 1)


# ---- Sorted by driver, then student ----
order = [r["name"] for r in we._sort_personal_driver_riders(non_shuttle)]
check("sorted by driver then student, Unassigned last",
      order == ["Sarah Lee", "Tom Suh", "Grace Ryoo", "Justin Kim"], str(order))
check("driver email uses the sorted order",
      body.index("Sarah Lee - ") < body.index("Tom Suh - ") < body.index("Grace Ryoo - ") < body.index("Justin Kim - "))
check("admin email uses the sorted order",
      admin.index("Sarah Lee - ") < admin.index("Tom Suh - ") < admin.index("Grace Ryoo - ") < admin.index("Justin Kim - "))

print()
failed = [label for label, ok in results if not ok]
if failed:
    print(f"{len(failed)} FAILED: {failed}")
    sys.exit(1)
print(f"All {len(results)} checks passed.")

