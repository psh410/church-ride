# Pins down that the Wednesday driver reminder checks the Available Drivers
# sheet first, saves any needed change, and tells the drivers about it.
#
# Run it directly (no pytest needed), from the repo root:
#
#     python3 tests/test_wednesday_reminder_update.py
#
# Nothing here touches Sheets, Firestore, Gmail or the network.

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import functions.send_weekly_emails as we

results: list[tuple[str, bool]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    results.append((label, bool(condition)))
    print(("PASS" if condition else "FAIL"), "-", label, detail if not condition else "")


SUNDAY = "2026-10-04"
THIS = {"date": SUNDAY, "slot": "Shuttle 2 (whole day)", "old": "Peter Hahn", "new": "Dae Kang",
        "reason": "elder, first Sunday (communion)"}
LATER = {"date": "2026-10-11", "slot": "Shuttle 2 (whole day)", "old": "Albert Lee", "new": "Dae Kang",
         "reason": "no longer available"}


def result(changes=(), unfilled=(), errors=()):
    return {"changes": list(changes), "unfilled": list(unfilled), "errors": list(errors),
            "skipped": [], "applied": True, "report": ""}


def refresh(res):
    with mock.patch("functions.rebalance_drivers.run_rebalance", return_value=res):
        return "\n".join(we._refresh_schedule_for_reminder(SUNDAY))


check("nothing changed means no update section", we._refresh_schedule_for_reminder.__name__ and refresh(result()) == "")

text = refresh(result([THIS]))
check("a change for this Sunday is announced", "A driver change was made for this Sunday" in text, text)
check("it names old and new driver", "Peter Hahn → Dae Kang" in text, text)

text = refresh(result([THIS, LATER]))
check("changes to later Sundays are listed separately",
      "Other upcoming changes" in text and "Albert Lee → Dae Kang" in text, text)
check("this Sunday's change comes first", text.index("this Sunday") < text.index("Other upcoming"), text)

text = refresh(result([LATER]))
check("only later changes: no 'this Sunday' announcement", "this Sunday" not in text, text)

text = refresh(result(unfilled=[{"date": SUNDAY, "slot": "Shuttle 1 return", "driver": "Old Driver", "reason": "x"}]))
check("an unfilled slot this Sunday is called out", "STILL NEEDS A DRIVER" in text and "Old Driver" in text, text)

text = refresh(result([THIS], errors=[{"id": "x", "error": "boom"}]))
check("a partly failed save is flagged", "could not be saved" in text, text)

with mock.patch("functions.rebalance_drivers.run_rebalance", side_effect=RuntimeError("sheets down")):
    check("a failed check does not raise and adds nothing", we._refresh_schedule_for_reminder(SUNDAY) == [])

# The email body shows the section.
routes = [{"shuttle_id": "shuttle_1", "shuttle_name": "Shuttle 1", "van": "Ford Transit (Gray)",
           "stops": [{"stop_name": "FAR", "pickup_time": "9:05 AM"}]}]
assignments = [{"route_id": "shuttle_1", "pickup_driver": "Dae Kang", "return_driver": "Dae Kang"}]
body = we._build_wednesday_reminder_body(SUNDAY, assignments, routes, None, ["A driver change was made for this Sunday:"])
check("the reminder shows the update section", "SCHEDULE UPDATE" in body and "A driver change was made" in body)
check("the update comes before the shuttle details", body.index("SCHEDULE UPDATE") < body.index("SHUTTLE 1"))
plain = we._build_wednesday_reminder_body(SUNDAY, assignments, routes, None)
check("no update section when there is nothing to say", "SCHEDULE UPDATE" not in plain)

# The check runs before the schedule is read, and the send uses the refreshed schedule.
order = []
with mock.patch.object(we, "clear_route_caches"), \
     mock.patch.object(we, "_refresh_schedule_for_reminder", side_effect=lambda d: order.append("refresh") or []), \
     mock.patch.object(we, "get_semester_schedule", side_effect=lambda: order.append("read") or []):
    we.send_wednesday_reminder(SUNDAY)
check("the sheet is checked before the schedule is read", order[:2] == ["refresh", "read"], str(order))

print()
failed = [label for label, ok in results if not ok]
if failed:
    print(f"{len(failed)} FAILED: {failed}")
    sys.exit(1)
print(f"All {len(results)} checks passed.")
