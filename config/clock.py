# One definition of "what day is it" for the whole system.
#
# Cloud Run containers run in UTC, so a bare date.today() there is
# already tomorrow from about 7pm Central. Everything this system
# schedules is a church event on a Champaign calendar: which Sunday a
# rider is asking about, which Thursday the prayer meeting falls on,
# which week the Morning Prayer email covers. None of those questions
# have a UTC answer.
#
# Read as UTC, they were all wrong for the last five hours of every day,
# in ways that looked like data problems rather than clock problems: a
# rider texting Wednesday evening filed under Thursday, a Sunday evening
# text rolling onto Monday where nobody would look for it.
#
# Anything asking what day it is locally should come through here rather
# than calling date.today() directly.

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# The church is in Champaign, Illinois. This handles daylight saving on
# its own, which a fixed UTC offset would not.
CHICAGO = ZoneInfo("America/Chicago")


def church_now() -> datetime:
    """The current time in Central, as a timezone-aware datetime."""
    return datetime.now(CHICAGO)


def church_today() -> date:
    """Today's date in Central."""
    return church_now().date()


def next_sunday(today: date | None = None) -> date:
    """The coming Sunday in Central, or today when today is Sunday.

    Today counting as the answer is deliberate: a text during Sunday
    dismissal is about that morning's service, not the one a week out.

    Args:
        today: Optional date override for tests.
    """
    today = today or church_today()
    # weekday(): Monday=0 ... Sunday=6. The modulo keeps "today is
    # Sunday" at zero days away rather than wrapping to seven.
    return today + timedelta(days=(6 - today.weekday()) % 7)
