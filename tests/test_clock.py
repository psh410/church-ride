# The one definition of "what day is it" for this system.
#
# Run it directly, from the repo root:
#
#     python3 tests/test_clock.py
#
# Cloud Run containers run in UTC, so a bare date.today() there is
# already tomorrow from about 7pm Central. Every date this system cares
# about is a church date on a Champaign calendar, so read as UTC they
# were all wrong for the last five hours of every day, in ways that
# looked like data problems rather than clock problems: a rider texting
# Wednesday evening filed under Thursday, a Sunday evening text rolling
# onto Monday where nobody would look for it.

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import clock

_FAILURES: list[str] = []
_RAN = 0


def check(condition: bool, message: str) -> None:
    if not condition:
        _FAILURES.append(message)


def _at_utc(year, month, day, hour, minute=0):
    """Freeze the clock at a real UTC instant."""
    moment = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment

    return mock.patch.object(clock, "datetime", _Frozen)


def test_evening_central_is_still_today():
    # 23:30 UTC on the 16th is 18:30 Central on the 16th. This is the
    # exact window where the old code answered "the 17th".
    with _at_utc(2026, 9, 16, 23, 30):
        got = clock.church_today()
    check(got == date(2026, 9, 16),
          f"6:30pm Central should still be the 16th, got {got}")


def test_after_midnight_central_rolls_over_properly():
    # 06:00 UTC on the 17th is 01:00 Central on the 17th. Genuinely a
    # new day, and it should say so.
    with _at_utc(2026, 9, 17, 6, 0):
        got = clock.church_today()
    check(got == date(2026, 9, 17),
          f"1am Central should be the 17th, got {got}")


def test_the_returned_time_carries_central():
    with _at_utc(2026, 9, 16, 23, 30):
        now = clock.church_now()
    check(now.tzinfo is not None, "church_now should be timezone-aware")
    check(now.hour == 18, f"should be 6pm Central, got {now.hour}")


def test_next_sunday_from_every_weekday():
    # Mon 9/14 through Sat 9/19 all point at Sunday 9/20.
    for day in range(14, 20):
        got = clock.next_sunday(date(2026, 9, day))
        check(got == date(2026, 9, 20),
              f"9/{day} should point at 9/20, got {got}")


def test_sunday_points_at_itself():
    # A text during dismissal is about THAT morning's service, not the
    # one a week out.
    got = clock.next_sunday(date(2026, 9, 20))
    check(got == date(2026, 9, 20),
          f"Sunday should point at itself, got {got}")


def test_monday_points_at_the_week_ahead():
    got = clock.next_sunday(date(2026, 9, 21))
    check(got == date(2026, 9, 27),
          f"Monday should point six days out, got {got}")


def test_a_sunday_evening_text_stays_on_that_sunday():
    # The failure this replaced. 01:00 UTC Monday is 8pm Central Sunday,
    # and a return ride request then is about the service that morning.
    with _at_utc(2026, 9, 21, 1, 0):
        got = clock.next_sunday()
    check(got == date(2026, 9, 20),
          f"8pm Sunday Central should stay on 9/20, got {got}")


def test_daylight_saving_is_handled_not_hardcoded():
    # A fixed offset would be an hour wrong for part of the year. CST is
    # UTC-6, CDT is UTC-5.
    with _at_utc(2026, 7, 1, 12, 0):
        summer = clock.church_now().utcoffset().total_seconds() / 3600
    with _at_utc(2026, 12, 1, 12, 0):
        winter = clock.church_now().utcoffset().total_seconds() / 3600
    check(summer == -5, f"July should be CDT, UTC-5, got {summer}")
    check(winter == -6, f"December should be CST, UTC-6, got {winter}")


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
    print("All clock tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
