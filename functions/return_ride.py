# On-demand return ride signup: right after service, anyone texts
# RIDE <name> <dorm/address> to request a ride home, and Dae/Sarah pull
# the live count (and any names past shuttle capacity) any time by
# texting REQUESTS. Handled by the /sms-webhook route in cloud_app.py.
#
# Requests are filed against the coming Sunday's service rather than
# against the day they are sent, so a text at any hour of the week lands
# on the service it is about. This is a standalone signal, independent
# of the morning shuttle signup (the website form feeding the Routes/Shuttles tabs via
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
from datetime import datetime

from config import settings
from config.clock import CHICAGO, is_sunday, next_sunday
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
REQUESTS_KEYWORD = "REQUESTS"

# Kept as a set because cloud_app and the collision tests both read it
# that way, and because a future alias belongs here rather than in a
# second place.
REQUESTS_KEYWORDS = {REQUESTS_KEYWORD}

# Roughly two SMS segments per part. Twilio would happily concatenate a
# much longer body, but a phone showing one enormous bubble is harder to
# read on a sidewalk than a few numbered ones, and a failed segment in
# the middle of a concatenated message loses the whole thing rather than
# one part of it.
MAX_PART_CHARS = 300

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
# Carries the date rather than just "the return ride", so the rider can
# see which service they landed on. Date format matches
# build_requests_summary()'s so the rider's text and the admin reply
# read the same way.
_RIDER_ACK_TEMPLATE = (
    f"{BRAND_PREFIX}\nYou're on the list for the\nreturn ride {{date}}. {OPT_OUT_NOTICE}"
)

# Unlike every other keyword in this system, a bare "RIDE" with nothing
# after it should not go silent: the rider is actively trying to use it
# in the moment they need it to work.
_RIDE_FORMAT_HINT = (
    f"{BRAND_PREFIX}\nText RIDE followed by your name and dorm or address."
)

# RIDE only answers on Sunday. The list is for one service, and a
# request sent on Tuesday would sit on Sunday's list all week counting
# against the 28 seats, long after whoever sent it had forgotten. Rather
# than silently accepting it, say when to come back: the rider is
# clearly trying to use this, and silence or a false confirmation are
# both worse than a plain answer.
#
# Numbers on the ADMIN_SMS_PHONES allowlist are exempt, so this can be
# tested on a Wednesday without waiting for a service. Same allowlist
# that already gates UPDATE and REQUESTS, so it adds no new surface: a
# number that can already read the counts can also put a test row in
# them.
_NOT_OPEN_REPLY = (
    f"{BRAND_PREFIX}\nReturn ride sign-up opens Sunday after service. "
    f"Text RIDE then. {OPT_OUT_NOTICE}"
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


def matches_requests_keyword(body: str) -> bool:
    """Whether an inbound body is a REQUESTS command, with or without an
    argument.

    Whole-word matched for the same reason RIDE is: "REQUESTS" exactly,
    or "REQUESTS " followed by something. A plain "starts with" check
    would swallow any future keyword sharing the prefix.

    Args:
        body: The inbound body, already uppercased and stripped.
    """
    return body == REQUESTS_KEYWORD or body.startswith(f"{REQUESTS_KEYWORD} ")


def wants_full_list(body: str) -> bool:
    """Whether this REQUESTS text asked for the full rider list.

    Any argument at all counts. Deliberately forgiving: an admin who
    types "REQUESTS ALL", "REQUESTS FULL", "REQUESTS NAMES" or fumbles
    it entirely gets the long version, which is never harmful, rather
    than the short one with no hint that the argument was ignored.
    """
    return bool(body[len(REQUESTS_KEYWORD):].strip())


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
    returning the acknowledgment. Requests are bucketed by the coming
    Sunday's service date, computed in Central time.

    Args:
        phone: The requester's phone number, E.164 preferred.
        body: The inbound message body, already uppercased and
            stripped. Expected to satisfy matches_ride_keyword().

    Answers only on Sunday, except for admin numbers, which are let
    through any day so this can be tested without waiting for a service.
    On any other day a normal number gets a reply saying when sign-up
    opens, and nothing is recorded, so a request made days early cannot
    sit on Sunday's list counting against shuttle capacity.

    Returns:
        str: The SMS body to reply with. Always non-empty, so a rider
            who texts this never gets silence back, unlike most other
            keywords in this system.
    """
    # Day check first, ahead of the format hint. Someone texting a bare
    # RIDE on a Wednesday needs to know it is not open yet, not how to
    # format a request they cannot make.
    if not is_sunday() and not _is_test_exempt(phone):
        logger.info("RIDE from %s outside Sunday; replying not open.", phone)
        return _NOT_OPEN_REPLY

    raw_text = parse_ride_command(body)
    if raw_text is None:
        return _RIDE_FORMAT_HINT

    # Read the date once and use it for both the write and the reply, so
    # the rider is never told a different day than the one they were
    # filed under, even across a midnight UTC boundary mid-request.
    request_date = _service_sunday()

    try:
        result = record_return_ride_request(
            phone, request_date, raw_text, settings.RETURN_SHUTTLE_CAPACITY
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

    return _RIDER_ACK_TEMPLATE.format(date=_format_short_date(request_date))


def build_requests_summary(sunday_date: str | None = None) -> str:
    """Build the short REQUESTS reply: how many, and how many need a driver.

    Produces something like:

        CFC Rides:
        Returns for 9/20/26: 31 requested.
        Shuttles hold 28, so 3 need
        a personal driver.

    No names. Seats go to whoever boards first, so the system cannot say
    which particular people will be left for a personal driver, and
    naming any of them would be inventing an answer. What it can say is
    how many rides to arrange. Text "REQUESTS ALL" for everyone's names.

    The overflow is computed from the total against capacity rather than
    by counting stored needs_driver flags. Those flags are written once
    at signup and never revisited, so after a cancellation they describe
    a headcount that no longer exists.

    Args:
        sunday_date: The date to summarize, in ISO "YYYY-MM-DD" form.
            Defaults to the coming Sunday, the same key build_ride_reply
            files requests under.

    Returns:
        str: The multi-line summary text, ready to send as an SMS body.

    Raises:
        RuntimeError: If the requests can't be read from Firestore.
    """
    if sunday_date is None:
        sunday_date = _service_sunday()

    total = len(get_return_ride_requests_for_date(sunday_date))
    capacity = settings.RETURN_SHUTTLE_CAPACITY
    overflow = max(0, total - capacity)
    label = _format_short_date(sunday_date)

    if total == 0:
        body = f"Returns for {label}: nobody yet."
    elif overflow:
        body = (
            f"Returns for {label}: {total} requested. "
            f"Shuttles hold {capacity}, so {overflow} need a personal driver."
        )
    else:
        body = f"Returns for {label}: {total} requested (shuttles hold {capacity})."

    # No opt-out notice on the counts themselves - build_requests_reply()
    # adds the disclosure on a number's first-ever admin reply only.
    return f"{BRAND_PREFIX}\n{body}"


def build_requests_full_lines(sunday_date: str | None = None) -> list[str]:
    """Every rider for a Sunday, in the order they signed up.

    Sorted alphabetically by whatever the rider typed, not by signup
    order, because the only thing anyone does with this list is read it
    while looking for a particular person. Some enter a full name and
    some just a first name; no attempt is made to normalise that, since
    guessing which word is a surname is how the Morning Prayer roster
    started emailing the wrong people.

    One list, with no shuttle and driver split. Seats go to whoever
    boards first, so signup order does not decide who ends up on a
    shuttle and who needs a personal driver, and splitting the list on
    that basis would put a confident label on a guess.

    Numbering is sequential down the printed list, so a reader partway
    through part 2 knows where they are. It is NOT the seat position,
    which is assigned at signup and would jump around once sorted.

    Args:
        sunday_date: Optional ISO "YYYY-MM-DD". Defaults to the coming
            Sunday.

    Returns:
        list[str]: Body lines, without the brand prefix or any part
            numbering. Empty riders produce a single "nobody yet" line.

    Raises:
        RuntimeError: If the requests can't be read from Firestore.
    """
    if sunday_date is None:
        sunday_date = _service_sunday()

    requests = get_return_ride_requests_for_date(sunday_date)
    if not requests:
        return [f"Returns for {_format_short_date(sunday_date)}: nobody yet."]

    def by_name(req):
        # casefold rather than lower, so names entered in other scripts
        # or with accents still sort predictably.
        return _display_text(req.get("raw_text", "")).casefold()

    capacity = settings.RETURN_SHUTTLE_CAPACITY
    overflow = max(0, len(requests) - capacity)

    header = f"Returns for {_format_short_date(sunday_date)}: {len(requests)}"
    if overflow:
        header += f", {overflow} past the {capacity} shuttle seats"

    lines = [header]
    for index, req in enumerate(sorted(requests, key=by_name), start=1):
        lines.append(f"{index}. {_display_text(req.get('raw_text', ''))}")

    return lines


def split_into_parts(lines: list[str], max_chars: int = MAX_PART_CHARS) -> list[str]:
    """Pack lines into numbered SMS parts.

    Splits on line boundaries so a rider's name and address never land
    in different texts. Parts are numbered "(1/3)" only when there is
    more than one, since "(1/1)" on a short reply is just noise.

    A single line longer than max_chars is not broken up: it goes out
    oversized and Twilio concatenates it. Breaking mid-address to honour
    a self-imposed limit would make it less readable, not more.

    Args:
        lines: Body lines, without the brand prefix.
        max_chars: Soft ceiling per part, before the prefix and counter.

    Returns:
        list[str]: Complete message bodies, each already branded.
    """
    if not lines:
        return [f"{BRAND_PREFIX}\nNothing to report."]

    chunks: list[list[str]] = [[]]
    length = 0
    for line in lines:
        # +1 for the newline that will join it to the previous line.
        addition = len(line) + 1
        if chunks[-1] and length + addition > max_chars:
            chunks.append([])
            length = 0
        chunks[-1].append(line)
        length += addition

    total = len(chunks)
    if total == 1:
        return [f"{BRAND_PREFIX}\n" + "\n".join(chunks[0])]

    return [
        f"{BRAND_PREFIX} ({index}/{total})\n" + "\n".join(chunk)
        for index, chunk in enumerate(chunks, start=1)
    ]


def build_requests_reply(
    phone: str, sunday_date: str | None = None, full: bool = False
) -> list[str]:
    """Build the REQUESTS reply for one admin, as one or more messages.

    Mirrors functions.send_admin_summary.build_admin_reply(), including
    sharing its disclosure_sent bookkeeping: the counts are the same for
    everyone, what varies is whether this number has been sent the
    program disclosure yet, from either admin keyword.

    Args:
        phone: The requesting admin's number, E.164 preferred.
        sunday_date: Optional ISO "YYYY-MM-DD" date to summarize.
            Defaults to the coming Sunday.
        full: True for every rider and destination, split across numbered
            parts. False for the counts and the overflow names only.

    Returns:
        list[str]: One or more SMS bodies, in order. A list even when
            there is one, so callers never have to handle both shapes.

    Raises:
        RuntimeError: If the requests can't be read. Disclosure
            bookkeeping never raises: if Firestore is unreachable we err
            toward including the disclosure, since sending it twice is
            harmless and skipping it is the compliance problem.
    """
    if full:
        parts = split_into_parts(build_requests_full_lines(sunday_date))
    else:
        parts = [build_requests_summary(sunday_date)]

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
        return parts

    try:
        record_disclosure_sent(phone)
    except RuntimeError as exc:
        # Send it anyway. The cost of failing to record is that they see
        # the disclosure again next time, which is noise, not a problem.
        logger.warning("Could not record disclosure for %s: %s", phone, exc)

    logger.info("Including first-contact disclosure in REQUESTS reply to %s.", phone)
    # On the last part, so a multi-part reply doesn't put the opt-out
    # instruction somewhere the reader has already scrolled past.
    parts[-1] = f"{parts[-1]}\n{FIRST_CONTACT_DISCLOSURE}"
    return parts


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------
def _is_test_exempt(phone: str) -> bool:
    """Whether this number may use RIDE outside Sunday.

    Admin allowlist only, and it fails closed: if the check itself
    raises, the caller is treated as an ordinary rider and the day gate
    applies. An admin who cannot test is a nuisance; a broken gate that
    lets the whole church sign up on a Tuesday is a real problem.
    """
    try:
        from functions.send_admin_summary import is_admin_phone

        return bool(is_admin_phone(phone))
    except Exception as exc:
        logger.warning("Could not check admin exemption for %s: %s", phone, exc)
        return False


def _service_sunday() -> str:
    """The Sunday this request is for, in ISO "YYYY-MM-DD" form.

    Requests are filed against the service they are for, not against
    the day they happen to be sent. Two reasons that matters.

    Cloud Run runs in UTC, so date.today() there is already tomorrow
    from about 7pm Central. A rider texting Wednesday evening was filed
    under Thursday, and REQUESTS on Sunday morning would have been
    looking at a different bucket than the one Saturday night's texts
    landed in. Computing in Central and rounding to the coming Sunday
    removes the whole class of problem: every text about one service
    lands on one key, whatever hour it is sent.

    It also kills the evening boundary this feature used to carry. A
    text sent 8pm Sunday Central is still Sunday in Chicago, so it stays
    on that service rather than rolling into Monday, where nobody would
    ever have looked for it.

    Today counts as the answer when today IS Sunday, so a text during
    dismissal files against that morning's service rather than the one a
    week out.
    """
    return next_sunday().isoformat()


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
