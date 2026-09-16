# On-demand return ride signup: right after service, anyone texts
# RIDE <name> <dorm/address> to request a ride home, and Dae/Sarah pull
# the live count (and any names past shuttle capacity) any time by
# texting REQUESTS. Handled by the /sms-webhook route in cloud_app.py.
#
# This is a same-day, standalone signal, independent of the morning
# shuttle signup (the website form feeding the Routes/Shuttles tabs via
# functions/read_riders_sheet.py). Only about half of morning shuttle
# riders actually take the return shuttle, and people who didn't ride
# the shuttle that morning often want it going home, so the return
# headcount can't be read off the morning signup list - it has to come
# from people saying so after service.
#
# RIDE is open to anyone, no allowlist and no driver-roster check,
# unlike every other keyword in this system. It also takes a free-text
# argument rather than matching a fixed set, so matches_ride_keyword()
# below does its own check instead of a plain membership test - see its
# docstring for why that has to be a whole-word match.
#
# See claude/return-ride-keyword.md in the Ride App Claude project for
# the full design discussion, including why raw_text is stored as typed
# rather than parsed into separate name/address fields, and why the 28
# seat threshold is a pull (REQUESTS), not a push, alert.

from __future__ import annotations

import logging
from datetime import date, datetime

from config import settings
from db.firestore_client import (
    get_return_ride_requests_for_date,
    record_disclosure_sent,
    record_return_ride_request,
    was_disclosure_sent,
)
from functions.send_sms import BRAND_PREFIX, OPT_OUT_NOTICE

logger = logging.getLogger(__name__)

# The keyword a rider texts to request a return ride, always followed by
# a name and dorm/address, e.g. "RIDE John Kim FAR".
RIDE_KEYWORD = "RIDE"

# What Dae/Sarah text to pull the live count. Admin-allowlist gated, same
# as ADMIN_SUMMARY_KEYWORDS (UPDATE/STATUS) in functions/send_admin_summary.py.
REQUESTS_KEYWORDS = {"REQUESTS"}

# How many names to list on the "Over" line before truncating, so a busy
# Sunday doesn't blow REQUESTS past a couple of SMS segments.
MAX_OVERFLOW_NAMES_SHOWN = 8

# Appended to a number's FIRST reply from ANY admin keyword, not just
# this one - disclosure_sent is a single flag per phone in Firestore,
# shared with UPDATE/STATUS (functions/send_admin_summary.py), because
# whichever keyword an admin happens to text first is the one that makes
# that reply their initial message, which is where Twilio's policy
# actually requires opt-out language. An admin who already got the
# disclosure from a prior UPDATE does not see it again just for trying
# REQUESTS first, and vice versa.
FIRST_CONTACT_DISCLOSURE = (
    f"Text REQUESTS any time for the return count. "
    f"Msg & data rates may apply. {OPT_OUT_NOTICE}"
)

# Every rider gets this back regardless of whether they landed inside or
# past the shuttle capacity - there's no reason to tell someone by text
# whether they got a shuttle seat or a personal driver before anyone has
# actually arranged one.
_RIDER_ACK = f"{BRAND_PREFIX}\nYou're on the list for\nthe return ride. {OPT_OUT_NOTICE}"

# Unlike every other keyword in this system, a bare "RIDE" with nothing
# after it should not go silent: the rider is actively trying to use it
# in the moment they need it to work.
_RIDE_FORMAT_HINT = (
    f"{BRAND_PREFIX}\nText RIDE followed by your name and dorm or address."
)

_SAVE_FAILED_REPLY = (
    f"{BRAND_PREFIX}\nCouldn't save that just now. Please tell an usher "
    f"you need a ride home."
)


def matches_ride_keyword(body: str) -> bool:
    """Return whether an inbound message body is a RIDE request.

    RIDE has to be matched as a whole word - either exactly RIDE with
    nothing else, or RIDE followed by a space - rather than a plain
    "starts with" check. The driver keyword this used to collide with
    (RIDERS) is now LIST, so there's nothing live sharing this prefix
    today, but matching it properly here means the next keyword that
    happens to start with the same letters doesn't get silently
    swallowed by this one.

    Args:
        body: The inbound message body, already uppercased and
            stripped, the way cloud_app.py's webhook prepares it before
            any keyword check runs.

    Returns:
        bool: True if this message should be handled as a RIDE request.
    """
    return body == RIDE_KEYWORD or body.startswith(f"{RIDE_KEYWORD} ")


def parse_ride_command(body: str) -> str | None:
    """Return the text after "RIDE ", or None if there isn't any.

    Args:
        body: The inbound message body, already uppercased and
            stripped. Expected to satisfy matches_ride_keyword().

    Returns:
        str or None: The trailing name/address text, or None when the
            rider texted the bare keyword with nothing after it.
    """
    remainder = body[len(RIDE_KEYWORD):].strip()
    return remainder or None


def build_ride_reply(phone: str, body: str) -> str:
    """Build the reply to a RIDE text: an acknowledgment, or a format hint.

    Records the request in Firestore (unless it's a bare "RIDE" with no
    argument, in which case there's nothing to record yet) before
    returning the acknowledgment. Requests are bucketed by today's
    actual calendar date, not the next upcoming Sunday - this is a
    same-day request, not an advance one.

    Args:
        phone: The requester's phone number, E.164 preferred.
        body: The inbound message body, already uppercased and
            stripped. Expected to satisfy matches_ride_keyword().

    Returns:
        str: The SMS body to reply with. Always non-empty, so a rider
            who texts this never gets silence back, unlike most other
            keywords in this system.
    """
    raw_text = parse_ride_command(body)
    if raw_text is None:
        return _RIDE_FORMAT_HINT

    try:
        result = record_return_ride_request(
            phone, _today(), raw_text, settings.RETURN_SHUTTLE_CAPACITY
        )
        logger.info(
            "Recorded return ride request from %s (position=%s, needs_driver=%s, is_new=%s).",
            phone,
            result["position"],
            result["needs_driver"],
            result["is_new"],
        )
    except RuntimeError as exc:
        logger.error("Could not record return ride request from %s: %s", phone, exc)
        return _SAVE_FAILED_REPLY

    return _RIDER_ACK


def build_requests_summary(sunday_date: str | None = None) -> str:
    """Build the REQUESTS reply: live return ride count, with any overflow.

    Produces something like:

        CFC Rides:
        Returns for 9/13/26: 31
        requested (28 shuttle, 3
        need drivers)
        Over: John Kim FAR, Sarah
        Lee 1002 S Lincoln, Tom
        Suh PAR

    The "Over" line is left off entirely when nobody has gone past
    capacity yet, and truncates to MAX_OVERFLOW_NAMES_SHOWN entries plus
    a "+N more" tail on a busy day.

    Args:
        sunday_date: The date to summarize, in ISO "YYYY-MM-DD" form.
            Defaults to today's actual calendar date, since these are
            same-day requests, not advance ones.

    Returns:
        str: The multi-line summary text, ready to send as an SMS body.

    Raises:
        RuntimeError: If the requests can't be read from Firestore.
    """
    if sunday_date is None:
        sunday_date = _today()

    requests = get_return_ride_requests_for_date(sunday_date)
    total = len(requests)
    overflow = [req for req in requests if req.get("needs_driver")]
    shuttle_count = total - len(overflow)

    lines = [
        BRAND_PREFIX,
        f"Returns for {_format_short_date(sunday_date)}: {total} requested "
        f"({shuttle_count} shuttle, {len(overflow)} need drivers)",
    ]

    if overflow:
        names = [_display_text(req.get("raw_text", "")) for req in overflow]
        shown = names[:MAX_OVERFLOW_NAMES_SHOWN]
        remaining = len(names) - len(shown)
        line = f"Over: {', '.join(shown)}"
        if remaining > 0:
            line += f", +{remaining} more"
        lines.append(line)

    # No opt-out notice on the counts themselves - build_requests_reply()
    # below adds the disclosure on a number's first-ever admin reply
    # only. Don't append OPT_OUT_NOTICE here, matching
    # build_admin_summary()'s equivalent comment.
    return "\n".join(lines)


def build_requests_reply(phone: str, sunday_date: str | None = None) -> str:
    """Build the full REQUESTS reply to send one admin, disclosure included if due.

    Mirrors functions.send_admin_summary.build_admin_reply() exactly,
    including sharing its disclosure_sent bookkeeping: the counts are
    the same for everyone, what varies is whether this phone number has
    been sent the program disclosure yet, from either admin keyword.

    Args:
        phone: The requesting admin's number, E.164 preferred.
        sunday_date: Optional ISO "YYYY-MM-DD" date to summarize.
            Defaults to today's actual calendar date.

    Returns:
        str: The SMS body to reply with.

    Raises:
        RuntimeError: If the requests can't be read (from
            build_requests_summary). Disclosure bookkeeping never
            raises: if Firestore is unreachable we err toward including
            the disclosure, since sending it twice is harmless and
            skipping it is the compliance problem.
    """
    summary = build_requests_summary(sunday_date)

    try:
        already_disclosed = was_disclosure_sent(phone)
    except RuntimeError as exc:
        logger.warning(
            "Could not check disclosure status for %s (%s); "
            "including the disclosure to be safe.",
            phone,
            exc,
        )
        already_disclosed = False

    if already_disclosed:
        return summary

    try:
        record_disclosure_sent(phone)
    except RuntimeError as exc:
        # Send it anyway. The cost of failing to record is that they see
        # the disclosure again next time, which is noise, not a problem.
        logger.warning("Could not record disclosure for %s: %s", phone, exc)

    logger.info("Including first-contact disclosure in REQUESTS reply to %s.", phone)
    return f"{summary}\n{FIRST_CONTACT_DISCLOSURE}"


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------
def _today() -> str:
    """Today's calendar date in ISO "YYYY-MM-DD" form.

    Same date.today() the rest of this codebase already uses (see
    functions/read_riders_sheet.get_next_sunday_date and
    functions/driver_sms_lookup._build_schedule_reply), which means it
    inherits the same known quirk: Cloud Run runs in UTC, so a text sent
    late Sunday night Central time (already Monday UTC) gets bucketed
    under Monday rather than that Sunday. That's a pre-existing,
    project-wide issue, not something new here - see
    claude/return-ride-keyword.md for the note.
    """
    return date.today().isoformat()


def _display_text(raw_text: str) -> str:
    """Best-effort label for the "Over" list from a stored RIDE request.

    raw_text is stored exactly as the rider typed it (see
    db.firestore_client.record_return_ride_request) rather than parsed
    into separate name/address fields, since people don't format it
    consistently enough to parse reliably. So this just trims it - Dae
    and Sarah are the ones deciding what to do with it anyway.
    """
    return raw_text.strip()


def _format_short_date(iso_date: str) -> str:
    """Convert "2026-09-13" to "9/13/26"."""
    parsed = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{parsed.month}/{parsed.day}/{parsed.strftime('%y')}"
