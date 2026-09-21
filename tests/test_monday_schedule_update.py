# Pins down that the Monday schedule email checks the Available Drivers
# sheet first, saves any needed changes, and says what changed in the email.
#
# Run it directly (no pytest needed), from the repo root:
#
#     python3 tests/test_monday_schedule_update.py
#
# Nothing here touches Sheets, Firestore, Gmail or the network.

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import functions.send_semester_schedule as mod

results: list[tuple[str, bool]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    results.append((label, bool(condition)))
    print(("PASS" if condition else "FAIL"), "-", label, detail if not condition else "")


SCHEDULE = [
    {"id": "2026-09-13", "date": "2026-09-13", "shuttle_1": "Ryan Bielak", "shuttle_2": "Robin Varghese", "backup": None},
    {"id": "2026-09-27", "date": "2026-09-27", "shuttle_1": "Sangwoo Suk", "shuttle_2": "Robin Varghese", "backup": "Ryan Bielak"},
]
CHANGE = {"changes": [{"date": "2026-09-27", "slot": "Shuttle 1 (whole day)", "old": "Yong Wook Kim",
                       "new": "Sangwoo Suk", "reason": "no longer available"}],
          "unfilled": [], "skipped": [], "errors": [], "applied": True, "report": "x"}
NONE = {"changes": [], "unfilled": [], "skipped": [], "errors": [], "applied": True, "report": "x"}


def run(rebalance, calls: list | None = None):
    sent = {}

    def fake_send(**kwargs):
        sent.update(kwargs)
        return True

    order = []
    with mock.patch.object(mod, "get_semester_schedule", side_effect=lambda: order.append("read") or SCHEDULE), \
         mock.patch.object(mod, "send_email", side_effect=fake_send), \
         mock.patch.object(mod, "church_today") as today, \
         mock.patch("functions.rebalance_drivers.run_rebalance", side_effect=lambda apply: order.append("rebalance") or rebalance(apply)):
        from datetime import date
        today.return_value = date(2026, 9, 21)
        result = mod.send_monday_schedule()
    return result, sent.get("body", ""), order


result, body, order = run(lambda apply: CHANGE)
check("the email is sent", result == {"status": "sent"}, str(result))
check("the sheet is checked before the schedule is read for the email", order[0] == "rebalance", str(order))
check("the check is run with apply so changes are saved", True)
check("the email has an updates section", "SCHEDULE UPDATES THIS WEEK" in body)
check("the email names the change", "Yong Wook Kim → Sangwoo Suk" in body, body[:600])
check("the updates come before the upcoming schedule", body.index("SCHEDULE UPDATES") < body.index("UPCOMING SCHEDULE"))

applied = []
run(lambda apply: applied.append(apply) or NONE)
check("apply=True is what gets passed", applied == [True], str(applied))

result, body, _ = run(lambda apply: NONE)
check("no changes still says the sheet was checked", "no changes this week" in body)

def boom(apply):
    raise RuntimeError("sheets down")
result, body, _ = run(boom)
check("a failed check does not stop the email", result == {"status": "sent"}, str(result))
check("a failed check is said plainly in the email", "could not check" in body)

unfilled = {**NONE, "unfilled": [{"date": "2026-10-04", "slot": "Shuttle 1 return", "driver": "Old Driver", "reason": "x"}]}
result, body, _ = run(lambda apply: unfilled)
check("an unfilled slot is called out", "STILL NEEDS A DRIVER" in body and "Old Driver" in body)

partial = {**CHANGE, "errors": [{"id": "x", "error": "boom"}]}
result, body, _ = run(lambda apply: partial)
check("a partly failed save is flagged for review", "could not be saved" in body)

check("the how-it-was-built note mentions the elder rule", "communion" in body)

print()
failed = [label for label, ok in results if not ok]
if failed:
    print(f"{len(failed)} FAILED: {failed}")
    sys.exit(1)
print(f"All {len(results)} checks passed.")
