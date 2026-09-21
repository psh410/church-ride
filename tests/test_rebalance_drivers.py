# Pins down the rules for changing the semester driver schedule when
# availability changes: keep everything that still works, replace only the
# slots that don't, choose the fairest replacement, and keep the elders off
# the first Sunday of the month (communion).
#
# Run it directly (no pytest needed), from the repo root:
#
#     python3 tests/test_rebalance_drivers.py
#
# Pure logic. Nothing here touches Sheets, Firestore or the network.

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from functions.rebalance_drivers import format_report, is_first_sunday, plan_rebalance

results: list[tuple[str, bool]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    results.append((label, bool(condition)))
    print(("PASS" if condition else "FAIL"), "-", label, detail if not condition else "")


def entry(day, s1, s2, backup=None, **extra):
    return {"id": day, "date": day, "shuttle_1": s1, "shuttle_2": s2,
            "shuttle_1_pickup": extra.get("s1p", s1), "shuttle_1_return": extra.get("s1r", s1),
            "shuttle_2_pickup": extra.get("s2p", s2), "shuttle_2_return": extra.get("s2r", s2),
            "backup": backup, "past": False}


TODAY = "2026-09-20"

# ---- Nothing changes when everyone is still available ----
schedule = [entry("2026-09-27", "Yong Wook Kim", "Robin Varghese", "Ryan Bielak")]
avail = {"2026-09-27": ["Yong Wook Kim", "Robin Varghese", "Ryan Bielak", "Sangwoo Suk"]}
plan = plan_rebalance(schedule, avail, {}, TODAY)
check("no change when every driver is still available", plan["changes"] == [] and plan["updates"] == {})
check("the input schedule is not modified", schedule[0]["shuttle_1"] == "Yong Wook Kim")

# ---- Only the unavailable slot changes ----
avail = {"2026-09-27": ["Robin Varghese", "Ryan Bielak", "Sangwoo Suk"]}
plan = plan_rebalance(schedule, avail, {}, TODAY)
check("only the unavailable driver's slot changes", len(plan["changes"]) == 1, str(plan["changes"]))
new = plan["schedule"][0]
check("the still-available driver keeps their slot", new["shuttle_2"] == "Robin Varghese")
check("the backup is untouched", new["backup"] == "Ryan Bielak")
check("replacement is an available driver not already on that day",
      new["shuttle_1"] == "Sangwoo Suk", new["shuttle_1"])
check("update fields cover the whole shuttle 1 record",
      plan["updates"]["2026-09-27"] == {"shuttle_1": "Sangwoo Suk",
                                        "shuttle_1_pickup": "Sangwoo Suk",
                                        "shuttle_1_return": "Sangwoo Suk"}, str(plan["updates"]))

# ---- Fairness: fewest drives wins, and a new driver starts at zero ----
schedule = [
    entry("2026-09-27", "Yong Wook Kim", "Robin Varghese"),
    entry("2026-10-11", "Sangwoo Suk", "Ryan Bielak"),
    entry("2026-10-25", "Sangwoo Suk", "Ryan Bielak"),
    entry("2026-11-08", "Josiah Chong", "Ryan Bielak"),
]
avail = {"2026-09-27": ["Robin Varghese", "Sangwoo Suk", "Josiah Chong", "New Driver"]}
plan = plan_rebalance(schedule, avail, {}, TODAY)
check("a new driver with no drives is picked before busier drivers",
      plan["schedule"][0]["shuttle_1"] == "New Driver", plan["schedule"][0]["shuttle_1"])

avail = {"2026-09-27": ["Robin Varghese", "Sangwoo Suk", "Josiah Chong"]}
plan = plan_rebalance(schedule, avail, {}, TODAY)
check("otherwise the driver with the fewest drives is picked (Josiah 1, Sangwoo 2)",
      plan["schedule"][0]["shuttle_1"] == "Josiah Chong", plan["schedule"][0]["shuttle_1"])
check("drive counts move with the change",
      plan["drives_after"].get("yong wook kim", 0) == 0 and plan["drives_after"]["josiah chong"] == 2,
      str(plan["drives_after"]))

# Several replacements in one run stay fair: the second pick sees the first.
schedule = [
    entry("2026-09-27", "A One", "B Two"),
    entry("2026-10-11", "A One", "B Two"),
]
avail = {"2026-09-27": ["C Three", "D Four"], "2026-10-11": ["C Three", "D Four"]}
plan = plan_rebalance(schedule, avail, {}, TODAY)
picked = [plan["schedule"][0]["shuttle_1"], plan["schedule"][0]["shuttle_2"],
          plan["schedule"][1]["shuttle_1"], plan["schedule"][1]["shuttle_2"]]
check("replacements are spread out, not piled on one person",
      picked.count("C Three") == 2 and picked.count("D Four") == 2, str(picked))

# ---- Past Sundays and Sundays with no availability row are left alone ----
schedule = [entry("2026-09-13", "Gone One", "Gone Two"), entry("2026-10-11", "Gone One", "Gone Two")]
plan = plan_rebalance(schedule, {"2026-09-13": ["X"]}, {}, TODAY)
check("a Sunday before today is never changed", plan["changes"] == [])
check("a Sunday with no availability row is left alone and reported",
      plan["skipped"] and plan["updates"] == {}, str(plan["skipped"]))
plan = plan_rebalance([dict(entry("2026-10-11", "G", "H"), past=True)], {"2026-10-11": ["X"]}, {}, TODAY)
check("a Sunday flagged past is never changed", plan["changes"] == [])

# ---- Shift: a Pickup-only driver can only take a pickup slot ----
schedule = [entry("2026-10-11", "Old Driver", "Robin Varghese")]
avail = {"2026-10-11": ["Robin Varghese", "Ryan Bielak", "Sangwoo Suk"]}
plan = plan_rebalance(schedule, avail, {"Ryan Bielak": "Pickup"}, TODAY)
s = plan["schedule"][0]
check("a Both driver covers a whole shuttle when one is free",
      s["shuttle_1_pickup"] == "Sangwoo Suk" and s["shuttle_1_return"] == "Sangwoo Suk", str(s))
plan = plan_rebalance(schedule, {"2026-10-11": ["Robin Varghese", "Ryan Bielak"]},
                      {"Ryan Bielak": "Pickup"}, TODAY)
s = plan["schedule"][0]
check("a Pickup-only driver takes the pickup leg only",
      s["shuttle_1_pickup"] == "Ryan Bielak", str(s))
check("the return leg is reported as needing someone",
      len(plan["unfilled"]) == 1 and plan["unfilled"][0]["slot"] == "Shuttle 1 return", str(plan["unfilled"]))
check("an unfilled slot keeps its old value rather than going blank",
      s["shuttle_1_return"] == "Old Driver", str(s))

# ---- Split shift: only the leg that changed is replaced ----
schedule = [entry("2026-10-11", "Ryan Bielak", "Robin Varghese", s1p="Ryan Bielak", s1r="Peter Hahn")]
avail = {"2026-10-11": ["Ryan Bielak", "Robin Varghese", "Sangwoo Suk"]}
plan = plan_rebalance(schedule, avail, {}, TODAY)
s = plan["schedule"][0]
check("split shift: the available leg stays, the other is replaced",
      s["shuttle_1_pickup"] == "Ryan Bielak" and s["shuttle_1_return"] == "Sangwoo Suk", str(s))

# ---- Elders: never on the first Sunday of the month ----
check("first Sunday of the month is detected", is_first_sunday("2026-10-04") and is_first_sunday("2026-11-01")
      and is_first_sunday("2026-12-06") and not is_first_sunday("2026-10-11"))
schedule = [entry("2026-10-04", "Peter Hahn", "Yong Wook Kim", "Albert Lee")]
avail = {"2026-10-04": ["Peter Hahn", "Albert Lee", "Yong Wook Kim", "Josiah Chong", "Sangwoo Suk"]}
plan = plan_rebalance(schedule, avail, {}, TODAY)
s = plan["schedule"][0]
check("an elder scheduled on a first Sunday is replaced even though available",
      s["shuttle_1"] not in ("Peter Hahn", "Albert Lee"), str(s))
check("an elder as backup on a first Sunday is replaced",
      s["backup"] not in ("Peter Hahn", "Albert Lee", None), str(s["backup"]))
check("the change says why", any("communion" in c["reason"] for c in plan["changes"]), str(plan["changes"]))
check("the other driver is untouched", s["shuttle_2"] == "Yong Wook Kim")

schedule = [entry("2026-10-04", "Old Driver", "Yong Wook Kim")]
plan = plan_rebalance(schedule, {"2026-10-04": ["Peter Hahn", "Albert Lee", "Yong Wook Kim"]}, {}, TODAY)
check("elders are never chosen as a replacement on a first Sunday",
      plan["schedule"][0]["shuttle_1"] == "Old Driver" and len(plan["unfilled"]) == 1, str(plan["schedule"][0]))

schedule = [entry("2026-10-11", "Old Driver", "Yong Wook Kim")]
plan = plan_rebalance(schedule, {"2026-10-11": ["Peter Hahn", "Albert Lee", "Yong Wook Kim"]}, {}, TODAY)
check("elders can still be chosen on other Sundays",
      plan["schedule"][0]["shuttle_1"] in ("Peter Hahn", "Albert Lee"), str(plan["schedule"][0]))

schedule = [entry("2026-10-18", "Peter Hahn", "Yong Wook Kim")]
plan = plan_rebalance(schedule, {"2026-10-18": ["Peter Hahn", "Yong Wook Kim"]}, {}, TODAY)
check("an elder already scheduled on a normal Sunday is left alone", plan["changes"] == [])

schedule = [entry("2026-10-04", "peter  hahn", "Yong Wook Kim")]
plan = plan_rebalance(schedule, {"2026-10-04": ["Josiah Chong", "Yong Wook Kim"]}, {}, TODAY)
check("names match regardless of case or spacing", plan["schedule"][0]["shuttle_1"] == "Josiah Chong")

# ---- Backup with nobody available becomes empty, not the unavailable person ----
schedule = [entry("2026-10-11", "Yong Wook Kim", "Robin Varghese", "Gone Person")]
plan = plan_rebalance(schedule, {"2026-10-11": ["Yong Wook Kim", "Robin Varghese"]}, {}, TODAY)
check("an unavailable backup with no replacement is cleared", plan["schedule"][0]["backup"] is None)
check("backup change is saved", plan["updates"]["2026-10-11"] == {"backup": None}, str(plan["updates"]))

# ---- Report ----
schedule = [entry("2026-09-27", "Yong Wook Kim", "Robin Varghese")]
plan = plan_rebalance(schedule, {"2026-09-27": ["Robin Varghese", "Sangwoo Suk"]}, {}, TODAY)
text = format_report(plan, applied=False)
check("preview report says nothing was saved", "nothing saved" in text, text)
check("report lists the change", "Yong Wook Kim -> Sangwoo Suk" in text, text)
check("no-change report is clear",
      "No changes" in format_report(plan_rebalance(schedule, {"2026-09-27": ["Yong Wook Kim", "Robin Varghese"]}, {}, TODAY), False))

print()
failed = [label for label, ok in results if not ok]
if failed:
    print(f"{len(failed)} FAILED: {failed}")
    sys.exit(1)
print(f"All {len(results)} checks passed.")
