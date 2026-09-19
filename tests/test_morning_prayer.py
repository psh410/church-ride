# The Morning Prayer reminder: which week it covers, and who it reaches.
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
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

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


CHICAGO = ZoneInfo("America/Chicago")


# --------------------------------------------------------------------------
# Which week the email covers
# --------------------------------------------------------------------------
# The bug that actually sent the wrong people. The job fires Saturday
# 6pm Chicago (Cloud Scheduler "0 18 * * 6") but the code was written
# for a Sunday 6pm run, so on a Saturday it walked back six days to the
# previous Sunday. Every email described the week that had just ended.
# The names were read correctly, just off the wrong row, which is why it
# looked like wrong people rather than a broken job.
def _monday_at(year, month, day, hour):
    return mp.get_schedule_monday(
        datetime(year, month, day, hour, 0, tzinfo=CHICAGO)
    )


def test_the_saturday_send_covers_the_week_ahead():
    # Sat 9/12 6pm is the real firing time. The week it must describe is
    # Mon 9/14 through Fri 9/18, not the one just finished.
    got = _monday_at(2026, 9, 12, 18)
    check(got.isoformat() == "2026-09-14",
          f"Saturday's send should cover the following Monday, got {got}")


def test_every_saturday_lands_two_days_out():
    for day in (5, 12, 19, 26):
        got = _monday_at(2026, 9, day, 18)
        want = datetime(2026, 9, day).date() + timedelta(days=2)
        check(got == want, f"Sat 9/{day} should give {want}, got {got}")


def test_a_retry_later_in_the_week_stays_on_the_same_week():
    # A rerun on Tuesday must not skip ahead to a week nobody has
    # reached yet, and must not fall back to the one that has passed.
    for day, hour, label in (
        (13, 18, "Sunday"),
        (14, 9, "Monday morning"),
        (14, 15, "Monday afternoon"),
        (15, 18, "Tuesday"),
        (18, 18, "Friday"),
    ):
        got = _monday_at(2026, 9, day, hour)
        check(got.isoformat() == "2026-09-14",
              f"{label} should still target 2026-09-14, got {got}")


def test_a_naive_datetime_is_read_as_chicago():
    aware = mp.get_schedule_monday(datetime(2026, 9, 12, 18, 0, tzinfo=CHICAGO))
    naive = mp.get_schedule_monday(datetime(2026, 9, 12, 18, 0))
    check(aware == naive, f"naive should match aware, {naive} vs {aware}")


# --------------------------------------------------------------------------
# Worship assignment, which lives in cell colour
# --------------------------------------------------------------------------
# The sheet encodes who leads each day as a background colour, not as
# text. Every leader has a signature colour, shown on their own name
# cell in the directory. A weekday column is normally tinted with its
# regular leader's colour; when somebody covers, that week's cell is
# tinted with the SUBSTITUTE's colour.
#
# None of this was visible to the code, because every other Sheets read
# in this project uses the values API, which returns contents and no
# formatting whatsoever. Week of 9/14/2026: Monday was tinted Kevin
# Kim's colour while Ryan recovered from surgery and Friday was tinted
# Albert Lee's, and the email announced Ryan and Andrew.

RYAN = "#FCE5CD"
KEVIN = "#CFE2F3"
JAMES = "#D9D2E9"
ANDREW = "#F4CCCC"
ALBERT = "#EA9999"
GREY = "#B7B7B7"


def _hex_to_channels(value):
    """Colour as the Sheets API sends it: channels equal to 0 are omitted,
    so pure red is {"red": 1} and black is {}."""
    value = value.lstrip("#")
    channels = {
        "red": int(value[0:2], 16) / 255,
        "green": int(value[2:4], 16) / 255,
        "blue": int(value[4:6], 16) / 255,
    }
    return {name: level for name, level in channels.items() if level}


def _cell(value="", colour=None):
    cell = {"formattedValue": value} if value else {}
    if colour:
        cell["effectiveFormat"] = {"backgroundColor": _hex_to_channels(colour)}
    return cell


def _row(*cells):
    return {"values": list(cells)}


def _grid(day_colours, week_date="9/13/2026"):
    """A miniature worship tab: directory, legend, and one weekly row."""
    blank = _cell()
    directory = [
        ("Ryan Bielak", RYAN, "Mon"),
        ("Kevin Kim", KEVIN, "Tues, Thurs"),
        ("James Park", JAMES, "Wed"),
        ("Andrew Cheun", ANDREW, "Fri"),
        ("Albert Lee", ALBERT, "Backup"),
    ]
    rows = []
    # The weekly row: A=date, B-F=weekdays, G=comments, H-J=directory.
    week_cells = [_cell(week_date)]
    week_cells += [_cell("song", colour) for colour in day_colours]
    week_cells += [blank]
    name, colour, days = directory[0]
    week_cells += [_cell(name, colour), _cell("555"), _cell(days)]
    rows.append(_row(*week_cells))

    for name, colour, days in directory[1:]:
        rows.append(
            _row(blank, blank, blank, blank, blank, blank, blank,
                 _cell(name, colour), _cell("555"), _cell(days))
        )
    # The legend block: a label in H with NOTHING in J, which is what
    # separates it from a real directory entry.
    rows.append(
        _row(blank, blank, blank, blank, blank, blank, blank, _cell("ABSENCE", GREY))
    )
    return rows


NEED_SUB_RED = "#FF0000"
OTHER_SUB_YELLOW = "#FFFF00"
CANCELLED_BLACK = "#000000"


def _real_layout_grid(day_colours):
    """The live tab: every legend swatch has a Hall of Fame name in column
    J beside it, which is what once made them look like leaders."""
    blank = _cell()
    grid = _grid(day_colours)
    for label, colour, hall_of_fame in (
        ("ABSENCE", GREY, "Aaron Chun"),
        ("NEED SUB", NEED_SUB_RED, "Alex Joe"),
        ("OTHER SUB", OTHER_SUB_YELLOW, "Bo Wang"),
        ("CANCELLED", CANCELLED_BLACK, "Bryan Kim"),
    ):
        grid.append(
            _row(blank, blank, blank, blank, blank, blank, blank,
                 _cell(label, colour), blank, _cell(hall_of_fame))
        )
    return grid


def _worship_for(day_colours, week_date="9/13/2026"):
    from datetime import date as _date

    with mock.patch.object(mp, "_worship_grid", return_value=_grid(day_colours, week_date)):
        return mp._get_worship_for_week(_date(2026, 9, 14))


def test_a_normal_week_uses_the_standing_leaders():
    got = _worship_for([RYAN, KEVIN, JAMES, KEVIN, ANDREW])
    check(got["Mon"]["name"] == "Ryan Bielak", f"Monday: {got['Mon']}")
    check(got["Tue"]["name"] == "Kevin Kim", f"Tuesday: {got['Tue']}")
    check(got["Wed"]["name"] == "James Park", f"Wednesday: {got['Wed']}")
    check(got["Fri"]["name"] == "Andrew Cheun", f"Friday: {got['Fri']}")
    check(not any(d["absent"] for d in got.values()), "nobody should be absent")


def test_a_covered_day_names_the_substitute():
    # The real week of 9/14/2026: Kevin covers Monday, Albert covers Friday.
    got = _worship_for([KEVIN, KEVIN, JAMES, KEVIN, ALBERT])
    check(got["Mon"]["name"] == "Kevin Kim",
          f"Monday should be the covering leader, got {got['Mon']}")
    check(got["Fri"]["name"] == "Albert Lee",
          f"Friday should be the covering leader, got {got['Fri']}")
    check(not got["Mon"]["absent"],
          "a covered day is not an absence, somebody is leading it")


def test_a_backup_can_cover_even_though_they_have_no_standing_day():
    # Albert's days cell reads "Backup", so he is in no standing slot.
    # Colour still has to be able to put him on a day.
    got = _worship_for([ALBERT, KEVIN, JAMES, KEVIN, ANDREW])
    check(got["Mon"]["name"] == "Albert Lee", f"Monday: {got['Mon']}")


def test_the_grey_absence_colour_asks_for_a_backup():
    got = _worship_for([GREY, KEVIN, JAMES, KEVIN, ANDREW])
    check(got["Mon"]["absent"], "the grey swatch should mark the day absent")
    check(got["Mon"]["name"] == "Ryan Bielak",
          "the standing leader is still named, so the reader knows who is out")
    rendered = mp._render_name(got["Mon"]["name"], got["Mon"]["absent"])
    check("Backup" in rendered, f"an absent slot should ask for Backup: {rendered!r}")


def test_an_uncoloured_cell_falls_back_to_the_standing_leader():
    got = _worship_for([None, KEVIN, JAMES, KEVIN, ANDREW])
    check(got["Mon"]["name"] == "Ryan Bielak",
          f"no colour should mean business as usual, got {got['Mon']}")


def test_an_unknown_colour_falls_back_rather_than_dropping_the_day():
    got = _worship_for(["#123456", KEVIN, JAMES, KEVIN, ANDREW])
    check(got["Mon"]["name"] == "Ryan Bielak",
          f"an unrecognised colour should not blank the day, got {got['Mon']}")


def test_a_missing_week_row_still_produces_a_schedule():
    got = _worship_for([RYAN, KEVIN, JAMES, KEVIN, ANDREW], week_date="1/1/2030")
    check(got["Tue"]["name"] == "Kevin Kim",
          f"a missing row should fall back to standing, got {got['Tue']}")


def test_the_legend_is_not_read_as_a_person():
    grid = _grid([RYAN, KEVIN, JAMES, KEVIN, ANDREW])
    names = [entry["name"] for entry in mp._worship_directory(grid)]
    check("ABSENCE" not in names, f"the legend leaked into the directory: {names}")
    check(len(names) == 5, f"expected 5 leaders, got {names}")


def test_the_absence_colour_is_read_from_the_sheet_not_hardcoded():
    grid = _grid([RYAN, KEVIN, JAMES, KEVIN, ANDREW])
    check(mp._legend_colour(grid, "ABSENCE") == GREY,
          "the ABSENCE swatch colour should come from the legend cell")


# --------------------------------------------------------------------------
# Day cell parsing
# --------------------------------------------------------------------------
def test_full_day_names_and_separators_all_parse():
    # Every one of these silently matched NOTHING before, which removed
    # that leader from the schedule with no error anywhere.
    cases = {
        "Mon": ["Mon"],
        "Monday": ["Mon"],
        "Tues, Thurs": ["Tue", "Thu"],
        "Mon/Wed": ["Mon", "Wed"],
        "Mon & Wed": ["Mon", "Wed"],
        "Tuesday and Thursday": ["Tue", "Thu"],
    }
    for raw, expected in cases.items():
        got = mp._parse_worship_days(raw)
        check(got == expected, f"{raw!r} should parse to {expected}, got {got}")


def test_backup_and_blanks_parse_to_no_days():
    for raw in ("Backup", "—", "", "n/a"):
        got = mp._parse_worship_days(raw)
        check(got == [], f"{raw!r} should yield no days, got {got}")


# --------------------------------------------------------------------------
# Table layout
# --------------------------------------------------------------------------
def test_columns_never_run_together():
    # "Ryan Bielak (absent)" overflowed a fixed 17-char column and
    # printed straight into the theme with no space.
    schedule = {
        key: {
            "devotional": "Dae-Woung",
            "worship": "Ryan Bielak",
            "theme": mp.PRAYER_THEMES[key],
            "devotional_absent": False,
            "worship_absent": True,
        }
        for key, _ in mp.SCHEDULE_DAYS
    }
    table = mp._schedule_table(schedule)
    for line in table.splitlines()[2:]:
        # Every cell is padded, so the theme is always preceded by at
        # least two spaces no matter how long the worship cell got.
        theme_start = line.index(mp.PRAYER_THEMES["Mon"][:7]) if "Sunday Serm" in line else None
        if theme_start is not None:
            check(line[theme_start - 2:theme_start] == "  ",
                  f"the theme ran into the cell before it: {line!r}")
        check("  " in line, f"columns should stay separated: {line!r}")


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


# --------------------------------------------------------------------------
# A cancelled day is not a person named CANCELLED
# --------------------------------------------------------------------------

CANCELLED_PINK = "#F4CCCC"


def _cancelled_grid(day_colours):
    """The worship tab as it really is: the CANCELLED swatch sits in the
    H-J block with a phone-book style row, so it looks like a leader."""
    blank = _cell()
    grid = _grid(day_colours)
    grid.append(
        _row(blank, blank, blank, blank, blank, blank, blank,
             _cell("CANCELLED", CANCELLED_PINK), blank, _cell("Bryan Kim"))
    )
    return grid


def test_a_cancelled_day_is_not_read_as_a_leader():
    from datetime import date as _date

    grid = _cancelled_grid([RYAN, CANCELLED_PINK, JAMES, KEVIN, ANDREW])
    names = [entry["name"] for entry in mp._worship_directory(grid)]
    check("CANCELLED" not in names, f"CANCELLED leaked into the directory: {names}")

    with mock.patch.object(mp, "_worship_grid", return_value=grid):
        got = mp._get_worship_for_week(_date(2026, 9, 14))
    check(got["Tue"]["name"] is None, f"Tuesday should have no leader: {got['Tue']}")
    check(got["Tue"].get("cancelled") is True, f"Tuesday should be cancelled: {got['Tue']}")
    check(got["Mon"]["name"] == "Ryan Bielak", "other days are unaffected")


def test_a_cancelled_day_is_shown_and_does_not_trigger_the_alert():
    schedule = {
        key: {"devotional": "Ryan", "worship": "Ryan", "theme": "t"}
        for key, _ in mp.SCHEDULE_DAYS
    }
    schedule["Tue"] = {
        "devotional": None, "worship": None, "theme": "t",
        "worship_cancelled": True,
    }
    table = mp._schedule_table(schedule)
    check("Cancelled" in table, f"the schedule should say Cancelled:\n{table}")

    _, problems = mp.resolve_recipients_for_week(schedule, {"ryan": "r@example.com"})
    check(not any("CANCELLED" in p.upper() for p in problems),
          f"a cancelled day should not raise an unmatched name: {problems}")


def test_cancelled_text_in_a_cell_is_treated_as_cancelled():
    for value in ("CANCELLED", "Cancelled", "canceled"):
        check(mp._is_cancelled_marker(value), f"{value!r} should count as cancelled")
    check(not mp._is_cancelled_marker("Ryan Bielak"), "a real name is not a marker")
    check(not mp._is_cancelled_marker(None), "None is not a marker")


def test_a_need_sub_day_is_not_cancelled():
    """Regression: Tuesday was tinted NEED SUB red, arrived as {"red": 1},
    read as white, and white resolved to the last legend row, CANCELLED."""
    from datetime import date as _date

    grid = _real_layout_grid([RYAN, NEED_SUB_RED, JAMES, KEVIN, ANDREW])
    names = [entry["name"] for entry in mp._worship_directory(grid)]
    check(len(names) == 5, f"only the five leaders belong in the directory: {names}")
    with mock.patch.object(mp, "_worship_grid", return_value=grid):
        got = mp._get_worship_for_week(_date(2026, 9, 14))
    check(got["Tue"]["name"] == "Kevin Kim", f"Tuesday names the standing leader: {got['Tue']}")
    check(got["Tue"]["absent"] is True, f"NEED SUB should ask for a backup: {got['Tue']}")
    check(not got["Tue"].get("cancelled"), "NEED SUB is not a cancellation")


def test_every_legend_colour_is_read_as_itself():
    from datetime import date as _date

    for colour, expect in (
        (GREY, {"absent": True}),
        (NEED_SUB_RED, {"absent": True}),
        (OTHER_SUB_YELLOW, {"other_sub": True}),
        (CANCELLED_BLACK, {"cancelled": True}),
    ):
        grid = _real_layout_grid([RYAN, colour, JAMES, KEVIN, ANDREW])
        with mock.patch.object(mp, "_worship_grid", return_value=grid):
            got = mp._get_worship_for_week(_date(2026, 9, 14))["Tue"]
        for key, value in expect.items():
            check(got.get(key) is value, f"{colour} should set {key}: {got}")
        check(got["name"] not in {"ABSENCE", "NEED SUB", "OTHER SUB", "CANCELLED"},
              f"{colour} became a leader named {got['name']}")


def test_colour_channels_the_api_omits_read_as_zero():
    row = _row(_cell("x", NEED_SUB_RED), _cell("y", CANCELLED_BLACK), _cell("z", "#FFFFFF"))
    check(mp._cell_colour(row, 0) == "#FF0000", "red should stay red")
    check(mp._cell_colour(row, 1) == "#000000", "black should stay black")
    check(mp._cell_colour(row, 2) == "#FFFFFF", "white should stay white")


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
    print("All Morning Prayer tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
