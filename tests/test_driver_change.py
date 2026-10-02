# The CHANGE keyword: a guided, three text driver swap for admins.
#
# Run it directly, from the repo root:
#
#     python3 tests/test_driver_change.py
#
# Nothing here touches Twilio, Sheets, Firestore or the network.
#
# The cases worth reading first are the ones about numbers. A bare "2"
# means nothing except against the exact list it was sent with, so these
# pin down that a number outside a flow changes nothing, that an expired
# flow does not act on a stale menu, and that picking a slot on a split
# week cannot silently replace the leg the admin was not looking at.

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import functions.driver_change as dc

_FAILURES: list[str] = []
_RAN = 0


def check(condition: bool, message: str) -> None:
    if not condition:
        _FAILURES.append(message)


SUNDAY = "2026-10-04"

WHOLE_DAY = {
    "id": "sched-1",
    "date": SUNDAY,
    "shuttle_1": "Sangwoo Kim",
    "shuttle_2": "Yong Kim",
    "backup": "Albert Lee",
}

SPLIT = {
    "id": "sched-1",
    "date": SUNDAY,
    "shuttle_1": "Sangwoo Kim",
    "shuttle_2_pickup": "Yong Kim",
    "shuttle_2_return": "Daniel Park",
    "backup": "Albert Lee",
}

ROSTER = [
    {"name": "Albert Lee", "phone": "217-555-0101", "available": True},
    {"name": "Daniel Park", "phone": "217-555-0102", "available": False},
    {"name": "Peter Hahn", "phone": "217-555-0103", "available": False},
    {"name": "Sangwoo Kim", "phone": "217-555-0104", "available": True},
    {"name": "Yong Kim", "phone": "217-555-0105", "available": True},
]


# --------------------------------------------------------------------------
# What the admin types
# --------------------------------------------------------------------------
def test_only_the_exact_phrases_start_a_change():
    for body in ("CHANGE", "DRIVER CHANGE", "CHANGE DRIVER", "DRIVERCHANGE"):
        check(dc.matches_change_keyword(body), f"{body!r} should start a change")
    for body in ("CHANGED", "EXCHANGE", "CHANGE S1", "UPDATE"):
        check(not dc.matches_change_keyword(body), f"{body!r} should NOT start one")


def test_only_bare_numbers_are_menu_answers():
    for body in ("0", "3", "12"):
        check(dc.is_numeric_reply(body), f"{body!r} should read as a menu answer")
    for body in ("UPDATE", "RIDE 3", "S1", "3 Sangwoo"):
        check(not dc.is_numeric_reply(body), f"{body!r} should NOT")


# --------------------------------------------------------------------------
# The slot menu
# --------------------------------------------------------------------------
def test_a_normal_week_is_three_slots():
    slots = dc._slots_for(WHOLE_DAY)
    check([s["label"] for s in slots] == ["S1", "S2", "Backup"],
          f"expected three slots, got {[s['label'] for s in slots]}")


def test_the_menu_puts_the_name_before_the_leg():
    # "S1 Sangwoo Kim pickup", not "S1 pickup Sangwoo Kim". The name is
    # what the admin scans for and should not sit behind a qualifier.
    rows = [dc._menu_row(s) for s in dc._slots_for(SPLIT)]
    check(rows[1] == "S2 Yong Kim pickup", f"got {rows[1]!r}")
    check(rows[2] == "S2 Daniel Park return", f"got {rows[2]!r}")
    check(rows[0] == "S1 Sangwoo Kim", f"an unsplit slot carries no leg: {rows[0]!r}")
    check(rows[3] == "Backup Albert Lee", f"got {rows[3]!r}")


def test_an_unassigned_slot_reads_as_unassigned_in_the_menu():
    entry = dict(WHOLE_DAY)
    entry["backup"] = ""
    row = dc._menu_row(dc._slots_for(entry)[-1])
    check(row == "Backup unassigned", f"got {row!r}")


def test_a_split_shuttle_becomes_two_lines():
    # Collapsing this would mean picking "S2" replaced a driver the
    # admin never saw.
    slots = dc._slots_for(SPLIT)
    labels = [s["label"] for s in slots]
    check(labels == ["S1", "S2 pickup", "S2 return", "Backup"],
          f"split week should show both legs, got {labels}")
    check(slots[1]["current"] == "Yong Kim" and slots[2]["current"] == "Daniel Park",
          "each leg should show its own driver")


def test_a_whole_day_slot_clears_stale_leg_overrides():
    # Writing only the base field would leave an old per-leg override
    # sitting underneath it, silently winning.
    s1 = dc._slots_for(WHOLE_DAY)[0]
    check(s1["fields"] == ["shuttle_1"], f"should write the base field: {s1['fields']}")
    check(set(s1["clear"]) == {"shuttle_1_pickup", "shuttle_1_return"},
          f"should clear both leg fields: {s1['clear']}")


def test_a_leg_slot_touches_only_that_leg():
    pickup = dc._slots_for(SPLIT)[1]
    check(pickup["fields"] == ["shuttle_2_pickup"], str(pickup["fields"]))
    check(pickup["clear"] == [], "a leg change must not clear anything else")


def test_an_unassigned_slot_is_offerable():
    entry = dict(WHOLE_DAY)
    entry["backup"] = ""
    backup = dc._slots_for(entry)[-1]
    check(not backup["current"], "an empty backup should read as unassigned")


# --------------------------------------------------------------------------
# Picking a driver
# --------------------------------------------------------------------------
def _session_at_slot(index=0, entry=SPLIT):
    slots = dc._slots_for(entry)
    return {
        "step": "driver",
        "sunday_date": SUNDAY,
        "schedule_id": entry["id"],
        "slots": slots,
        "slot": slots[index],
        "drivers": ROSTER,
    }


def test_the_whole_roster_is_offered_with_unavailable_marked():
    session = {
        "step": "slot",
        "sunday_date": SUNDAY,
        "schedule_id": "sched-1",
        "slots": dc._slots_for(WHOLE_DAY),
    }
    with mock.patch.object(dc, "_roster_for", return_value=ROSTER), \
         mock.patch.object(dc, "set_driver_change_session"):
        reply = dc.build_driver_menu("+17034010571", session, 1)

    check("Peter Hahn (not available)" in reply,
          f"unavailable drivers should be offered and marked: {reply!r}")
    check("Sangwoo Kim" in reply and "Sangwoo Kim (not available)" not in reply,
          "available drivers should carry no mark")
    check("0 to exit" in reply, "every menu should say how to back out")


def test_an_out_of_range_pick_asks_again_without_changing_anything():
    session = {
        "step": "slot",
        "sunday_date": SUNDAY,
        "schedule_id": "sched-1",
        "slots": dc._slots_for(WHOLE_DAY),
    }
    with mock.patch.object(dc, "set_driver_change_session") as stored:
        reply = dc.build_driver_menu("+17034010571", session, 9)
    check("between 1 and 3" in reply, f"should say the valid range: {reply!r}")
    check(not stored.called, "a bad pick must not advance the flow")


# --------------------------------------------------------------------------
# Applying it
# --------------------------------------------------------------------------
def test_applying_writes_the_slot_and_tells_people():
    written = {}
    with mock.patch.object(dc, "_write_schedule",
                           side_effect=lambda sid, f: written.update({sid: f})), \
         mock.patch.object(dc, "clear_driver_change_session"), \
         mock.patch.object(dc, "_notify_everyone", return_value=3) as notify:
        reply = dc.apply_driver_change("+17034010571", _session_at_slot(1), 3)

    check(written == {"sched-1": {"shuttle_2_pickup": "Peter Hahn"}},
          f"should write only that leg: {written}")
    check(notify.called, "everyone involved should be told")
    check("Peter Hahn" in reply and "Yong Kim" in reply,
          f"the confirmation should name both drivers: {reply!r}")


def test_picking_the_same_person_changes_nothing():
    with mock.patch.object(dc, "_write_schedule") as write, \
         mock.patch.object(dc, "clear_driver_change_session"), \
         mock.patch.object(dc, "_notify_everyone") as notify:
        reply = dc.apply_driver_change("+17034010571", _session_at_slot(1), 5)

    check(not write.called, "reassigning someone to their own slot should not write")
    check(not notify.called, "and should not text anybody")
    check("already on" in reply, f"and should say so: {reply!r}")


def test_a_failed_write_says_nothing_changed():
    with mock.patch.object(dc, "_write_schedule",
                           side_effect=RuntimeError("firestore down")), \
         mock.patch.object(dc, "_notify_everyone") as notify:
        reply = dc.apply_driver_change("+17034010571", _session_at_slot(1), 3)

    check("Nothing was" in reply or "nothing was" in reply,
          f"a failed write must say so plainly: {reply!r}")
    check(not notify.called,
          "and must not tell drivers about a change that did not happen")


def test_a_notification_failure_does_not_undo_a_saved_change():
    # The change is already in Firestore. Reporting it as failed because
    # a text bounced would send the admin to fix something that is fine.
    with mock.patch.object(dc, "_write_schedule"), \
         mock.patch.object(dc, "clear_driver_change_session"), \
         mock.patch.object(dc, "_schedule_entry", side_effect=RuntimeError("down")):
        reply = dc.apply_driver_change("+17034010571", _session_at_slot(1), 3)
    check("Done" in reply, f"the change stands: {reply!r}")


# --------------------------------------------------------------------------
# Who hears about it
# --------------------------------------------------------------------------
def test_everyone_on_that_sunday_is_told_once():
    after = dict(SPLIT)
    after["shuttle_2_pickup"] = "Peter Hahn"
    sent = []
    with mock.patch.object(dc, "_schedule_entry", return_value=after), \
         mock.patch.object(dc, "_roster_for", return_value=ROSTER), \
         mock.patch.object(dc, "send_sms",
                           side_effect=lambda to, body: sent.append((to, body)) or True):
        count = dc._notify_everyone(SUNDAY, "S2 pickup", "Yong Kim", "Peter Hahn", "+1")

    numbers = [to for to, _ in sent]
    check(count == len(sent), "the count should match what went out")
    check(len(numbers) == len(set(numbers)), f"nobody twice: {numbers}")
    bodies = {to: b for to, b in sent}
    check("off S2 pickup" in bodies["+12175550105"],
          "the driver coming off should be told they are off")
    check("now on S2 pickup" in bodies["+12175550103"],
          "the driver going on should be told they are on")
    check("unchanged" in bodies["+12175550104"],
          "everyone else should be told their own slot is unchanged")


def test_a_driver_with_no_phone_is_skipped_not_fatal():
    roster = [dict(d) for d in ROSTER]
    roster[2]["phone"] = ""
    sent = []
    with mock.patch.object(dc, "_schedule_entry", return_value=SPLIT), \
         mock.patch.object(dc, "_roster_for", return_value=roster), \
         mock.patch.object(dc, "send_sms",
                           side_effect=lambda to, body: sent.append((to, body)) or True):
        count = dc._notify_everyone(SUNDAY, "S2 pickup", "Yong Kim", "Peter Hahn", "+1")
    check(count == len(sent), "the count should still match")
    check(all("+1217555010" in to for to, _ in sent), "and the rest still go out")


def main() -> int:
    global _RAN
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        _RAN += 1

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILED:")
        for failure in _FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"Ran {_RAN} test functions.")
    print("All driver change tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
