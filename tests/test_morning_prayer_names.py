# Name matching for the Morning Prayer reminder.
#
# Run it directly, from the repo root:
#
#     python3 tests/test_morning_prayer_names.py
#
# Nothing here touches Sheets, Gmail or the network.
#
# This exists because the reminder emailed the wrong people. Three
# functions each decided "is this the same human being?" with a
# different loose rule, and on a roster where a large share of surnames
# are Kim, Lee or Park, all three were wrong in ways nothing surfaced:
#
#   - _lookup_roster_email returned the first roster entry sharing ANY
#     token, in sheet row order, so "Ryan Kim" got Ryan Bielak's email
#     and reordering the Servants tab changed the answer.
#   - _name_is_absent used the same test, so marking one Kim away
#     struck out every Kim on the schedule.
#   - _names_mentioned matched substrings, so "Sunday" marked Sun
#     absent.
#
# All three now go through _same_person, and anything ambiguous is
# refused rather than guessed.

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import functions.prayer.morning as mp

_FAILURES: list[str] = []
_RAN = 0


def check(condition: bool, message: str) -> None:
    if not condition:
        _FAILURES.append(message)


# A roster shaped like this church's directory: repeated surnames and
# repeated given names, which is precisely what broke the old matching.
ROSTER = {
    "David Sun": "davidsun@example.com",
    "David Lee": "davidlee@example.com",
    "Ryan Bielak": "ryanb@example.com",
    "Justin Kim": "justin@example.com",
    "Kristin Kim": "kristin@example.com",
    "Dae-Woung Kang": "dae@example.com",
}


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------
def test_same_given_and_surname_is_the_same_person():
    check(mp._same_person("Justin Kim", "justin kim"), "case should not matter")
    check(mp._same_person("Dae-Woung Kang", "Dae Woung Kang"),
          "a hyphen should read the same as a space")


def test_a_bare_given_name_matches_the_full_name():
    check(mp._same_person("Justin", "Justin Kim"),
          "schedules carry bare first names; the roster carries full ones")
    check(mp._same_person("Kristin K.", "Kristin Kim"),
          "an initial should stand for the surname it begins")


def test_a_shared_surname_is_not_the_same_person():
    # The bug that started this.
    check(not mp._same_person("Justin Kim", "Kristin Kim"),
          "sharing a surname does not make two people one")
    check(not mp._same_person("Ryan Kim", "Ryan Bielak"),
          "sharing a given name does not make two people one either")


def test_a_bare_surname_identifies_nobody():
    # Tempting to allow, and exactly how one person's email reached
    # another.
    check(not mp._same_person("Kim", "Justin Kim"),
          "a bare surname must not resolve to anyone")


def test_an_initial_does_not_match_the_wrong_surname():
    check(not mp._same_person("Kristin K.", "Kristin Park"),
          "K should not match Park")


# --------------------------------------------------------------------------
# Email resolution
# --------------------------------------------------------------------------
def test_exact_and_unambiguous_names_resolve():
    for name, expected in (
        ("Justin Kim", "justin@example.com"),
        ("Justin", "justin@example.com"),
        ("Kristin K.", "kristin@example.com"),
        ("Dae-Woung Kang", "dae@example.com"),
    ):
        got, reason = mp.resolve_roster_email(name, ROSTER)
        check(got == expected, f"{name!r} should resolve to {expected}, got {got} ({reason})")


def test_an_ambiguous_name_is_refused_not_guessed():
    got, reason = mp.resolve_roster_email("David", ROSTER)
    check(got is None, f"'David' is ambiguous and must not resolve, got {got}")
    check("David Lee" in reason and "David Sun" in reason,
          f"the reason should name the candidates so the sheet can be fixed: {reason!r}")


def test_an_unknown_name_is_refused_with_a_useful_reason():
    got, reason = mp.resolve_roster_email("Ryan Kim", ROSTER)
    check(got is None, "a name matching nobody must not resolve")
    check("not on the Servants tab" in reason, f"unhelpful reason: {reason!r}")


def test_resolution_does_not_depend_on_roster_order():
    # The old code returned the first token match in sheet row order, so
    # reordering the Servants tab silently changed who got the email.
    reversed_roster = dict(reversed(list(ROSTER.items())))
    for name in ("Justin", "Kristin K.", "David", "Ryan Kim"):
        a, _ = mp.resolve_roster_email(name, ROSTER)
        b, _ = mp.resolve_roster_email(name, reversed_roster)
        check(a == b, f"{name!r} resolved differently after reordering: {a} vs {b}")


# --------------------------------------------------------------------------
# Absences
# --------------------------------------------------------------------------
def test_one_person_absent_does_not_strike_out_their_namesakes():
    absent = {"Justin Kim"}
    check(mp._name_is_absent("Justin Kim", absent), "the absent person should be absent")
    check(mp._name_is_absent("Justin", absent), "their bare first name too")
    for other in ("Kristin Kim", "David Lee", "Ryan Bielak"):
        check(not mp._name_is_absent(other, absent),
              f"{other} should not be marked absent because a Kim is away")


def test_no_name_is_never_absent():
    check(not mp._name_is_absent(None, {"Justin Kim"}), "None is not absent")
    check(not mp._name_is_absent("", {"Justin Kim"}), "empty is not absent")


# --------------------------------------------------------------------------
# Comment scanning
# --------------------------------------------------------------------------
def test_ordinary_words_no_longer_flag_people_absent():
    names = {"Sun", "Min", "Joy", "An"}
    for text in (
        "No prayer this Sunday",
        "Please arrive 10 minutes early",
        "Enjoy the week",
        "Any questions let me know",
    ):
        found = mp._names_mentioned(text, names)
        check(not found, f"{text!r} should flag nobody, flagged {sorted(found)}")


def test_real_mentions_are_still_caught():
    names = {"Sun", "Justin Kim", "Joy"}
    check(mp._names_mentioned("Sun is away Tuesday", names) == {"Sun"},
          "a real mention should still be found")
    check("Justin Kim" in mp._names_mentioned("Justin Kim is out", names),
          "a full name should still be found")
    check("Justin Kim" in mp._names_mentioned("justin cannot make it", names),
          "a given name should still be found")
    check(mp._names_mentioned("Joy, please cover Monday", names) == {"Joy"},
          "punctuation should not prevent a match")


# --------------------------------------------------------------------------
# The alert
# --------------------------------------------------------------------------
def _schedule_with(devotional, worship=None):
    key = mp.SCHEDULE_DAYS[0][0]
    return {key: {"devotional": devotional, "worship": worship}}


def test_unresolved_names_are_reported_not_swallowed():
    emails, problems = mp.resolve_recipients_for_week(
        _schedule_with("David", "Justin Kim"), ROSTER
    )
    check(emails == ["justin@example.com"],
          f"the resolvable name should still be emailed, got {emails}")
    check(len(problems) == 1, f"the ambiguous name should be reported, got {problems}")
    check("David" in problems[0], f"the problem should name who: {problems}")


def test_a_failed_alert_does_not_break_a_sent_reminder():
    # The reminder already went out; an alert that can't send must not
    # turn a delivered email into a failed job.
    with mock.patch.object(mp, "send_email", side_effect=RuntimeError("gmail down")):
        mp._alert_unresolved_names(["Monday devotional: 'David' could be any of: ..."])
    check(True, "unreachable")


def test_the_alert_goes_only_to_the_coordinator():
    captured = {}
    with mock.patch.object(
        mp, "send_email",
        side_effect=lambda **kw: captured.update(kw) or True,
    ):
        mp._alert_unresolved_names(["Monday devotional: 'David' is ambiguous"])
    check(captured.get("to") == mp.ALWAYS_BCC,
          f"alert should go to the coordinator, went to {captured.get('to')}")
    check(captured.get("bcc") is None, "the alert should not BCC the whole team")
    check("David" in captured.get("body", ""), "the alert should say which name failed")


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
    print("All Morning Prayer name matching tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
