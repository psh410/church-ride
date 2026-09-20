# Pins down that a route edited in the sheet reaches the next email or
# text instead of waiting for the Cloud Run container to restart.
#
# The Routes tab used to be cached for the life of the process. The live
# container stays warm for hours, so when FAR moved from Shuttle 1 to
# Shuttle 2 and pickup times shifted, riders were still grouped by the old
# stop-to-shuttle map and the emails and texts kept the old assignment.
#
# Run it directly, from the repo root:
#
#     python3 tests/test_route_cache.py
#
# Nothing here touches Sheets or the network.

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import functions.read_riders_sheet as rs

results: list[tuple[str, bool]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    results.append((label, bool(condition)))
    print(("PASS" if condition else "FAIL"), "-", label, detail if not condition else "")


BEFORE = [
    {"shuttle_id": "shuttle_1", "stops": [
        {"stop_name": "FAR", "pickup_time": "9:05 AM"},
        {"stop_name": "SDRP", "pickup_time": "9:10 AM"}]},
    {"shuttle_id": "shuttle_2", "stops": [
        {"stop_name": "Allen", "pickup_time": "9:00 AM"},
        {"stop_name": "ISR", "pickup_time": "9:05 AM"},
        {"stop_name": "Icon", "pickup_time": "9:10 AM"}]},
]
AFTER = [
    {"shuttle_id": "shuttle_1", "stops": [
        {"stop_name": "SDRP", "pickup_time": "9:10 AM"}]},
    {"shuttle_id": "shuttle_2", "stops": [
        {"stop_name": "FAR", "pickup_time": "9:00 AM"},
        {"stop_name": "Allen", "pickup_time": "9:05 AM"},
        {"stop_name": "ISR", "pickup_time": "9:10 AM"},
        {"stop_name": "Icon", "pickup_time": "9:15 AM"}]},
]


def fresh_module():
    rs.clear_route_caches()


fresh_module()
clock = {"now": 1000.0}
with mock.patch.object(rs, "_time") as fake_time, \
     mock.patch.object(rs, "get_routes", side_effect=[BEFORE, AFTER, AFTER]) as get_routes:
    fake_time.monotonic.side_effect = lambda: clock["now"]

    first = rs.get_stop_to_shuttle_map()
    check("FAR starts on shuttle_1", first["FAR"] == "shuttle_1", str(first))

    clock["now"] += 10
    again = rs.get_stop_to_shuttle_map()
    rs.get_stop_times_map()
    check("lookups inside the window reuse one read", get_routes.call_count == 1,
          f"{get_routes.call_count} reads")

    clock["now"] += rs.ROUTE_CACHE_TTL_SECONDS + 1
    later = rs.get_stop_to_shuttle_map()
    check("after the window an edited route is picked up",
          later["FAR"] == "shuttle_2", str(later))
    check("pickup times follow the edit",
          rs.get_stop_times_map()["FAR"] == "9:00 AM", str(rs.get_stop_times_map()))
    check("the stop map and time map come from the same read",
          get_routes.call_count == 2, f"{get_routes.call_count} reads")

fresh_module()
with mock.patch.object(rs, "get_routes", side_effect=[BEFORE, AFTER]):
    rs.get_stop_to_shuttle_map()
    rs.clear_route_caches()
    check("clear_route_caches forces a re-read",
          rs.get_stop_to_shuttle_map()["FAR"] == "shuttle_2")

print()
failed = [label for label, ok in results if not ok]
if failed:
    print(f"{len(failed)} FAILED: {failed}")
    sys.exit(1)
print(f"All {len(results)} checks passed.")
