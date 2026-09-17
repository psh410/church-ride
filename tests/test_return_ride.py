# Tests for the return ride signup feature (RIDE / REQUESTS keywords),
# see functions/return_ride.py and claude/return-ride-keyword.md in the
# Ride App Claude project for the full design.
#
# Two layers get tested here, matching the two places the risk actually
# lives:
#
# - db.firestore_client._apply_return_ride_request: the core position
#   and needs_driver logic, exercised against small fake transaction/
#   doc-ref objects rather than a live Firestore transaction (which
#   google-cloud-firestore's @firestore.transactional decorator can't
#   be faked into running without a real backend). This is where the
#   28-seat boundary and the "don't double count a resend" behavior
#   actually live, so it gets tested directly rather than through a
#   mock that could quietly diverge from the real implementation.
# - functions.return_ride: parsing, reply text, and REQUESTS formatting,
#   with db.firestore_client's functions mocked out.
#
# Run it directly (no pytest needed), from the repo root with the venv
# active:
#
#     python3 tests/test_return_ride.py
#
# Nothing here touches Firestore, Twilio, or the network.

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db.firestore_client as db_mod
import functions.return_ride as return_ride_mod
from functions.send_sms import BRAND_PREFIX, OPT_OUT_NOTICE

_FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        _FAILURES.append(message)


# --------------------------------------------------------------------------
# Fakes for db.firestore_client._apply_return_ride_request
# --------------------------------------------------------------------------
class _FakeSnapshot:
    def __init__(self, exists: bool, data: dict | None = None):
        self.exists = exists
        self._data = data or {}

    def to_dict(self) -> dict:
        return dict(self._data)


class _FakeDocRef:
    """Stands in for a Firestore DocumentReference. Starts empty."""

    def __init__(self):
        self._data: dict | None = None

    def get(self, transaction=None) -> _FakeSnapshot:
        if self._data is None:
            return _FakeSnapshot(False)
        return _FakeSnapshot(True, self._data)


class _FakeTransaction:
    """Stands in for a Firestore Transaction: applies writes immediately
    (no batching/commit semantics needed for these tests, since we're
    testing the decision logic, not Firestore's own atomicity)."""

    def set(self, doc_ref: _FakeDocRef, data: dict, merge: bool = False) -> None:
        if merge and doc_ref._data:
            doc_ref._data.update(data)
        else:
            doc_ref._data = dict(data)

    def update(self, doc_ref: _FakeDocRef, data: dict) -> None:
        doc_ref._data.update(data)


def test_first_request_gets_position_one():
    doc_ref = _FakeDocRef()
    counter_ref = _FakeDocRef()
    txn = _FakeTransaction()

    result = db_mod._apply_return_ride_request(
        txn, doc_ref, counter_ref, "+15550001", "2026-09-13", "John Kim FAR", 28
    )

    check(result == {"position": 1, "needs_driver": False, "is_new": True},
          f"first request expected position 1/not needs_driver, got {result}")
    check(doc_ref._data.get("raw_text") == "John Kim FAR", "raw_text not stored")
    check(counter_ref._data.get("count") == 1, "counter not incremented to 1")


def test_sequential_requests_get_distinct_increasing_positions():
    """Two calls in a row (simulating what two near-simultaneous texters
    would each cause) must not both land on the same position - this is
    the behavior that protects the 28-seat boundary from a race."""
    counter_ref = _FakeDocRef()

    positions = []
    for i in range(30):
        doc_ref = _FakeDocRef()  # a distinct phone each time
        result = db_mod._apply_return_ride_request(
            _FakeTransaction(), doc_ref, counter_ref, f"+1555{i:04d}", "2026-09-13",
            f"Rider {i}", 28,
        )
        positions.append(result["position"])

    check(positions == list(range(1, 31)),
          f"expected positions 1..30 in order, got {positions}")


def test_request_28_is_shuttle_29_needs_driver():
    counter_ref = _FakeDocRef()
    last_result = None
    for i in range(29):
        doc_ref = _FakeDocRef()
        last_result = db_mod._apply_return_ride_request(
            _FakeTransaction(), doc_ref, counter_ref, f"+1555{i:04d}", "2026-09-13",
            f"Rider {i}", 28,
        )
        if i == 27:  # this is the 28th request (0-indexed)
            check(last_result["needs_driver"] is False,
                  f"request 28 should not need a driver, got {last_result}")

    check(last_result["position"] == 29 and last_result["needs_driver"] is True,
          f"request 29 should need a driver, got {last_result}")


def test_duplicate_from_same_phone_does_not_consume_another_slot():
    counter_ref = _FakeDocRef()
    doc_ref = _FakeDocRef()

    first = db_mod._apply_return_ride_request(
        _FakeTransaction(), doc_ref, counter_ref, "+15551234", "2026-09-13",
        "John Kim FAR", 28,
    )
    second = db_mod._apply_return_ride_request(
        _FakeTransaction(), doc_ref, counter_ref, "+15551234", "2026-09-13",
        "John Kim, corrected: SDRP", 28,
    )

    check(first["is_new"] is True, "first request should be new")
    check(second["is_new"] is False, "resend should not be treated as new")
    check(second["position"] == first["position"],
          "resend should keep the original position")
    check(second["needs_driver"] == first["needs_driver"],
          "resend should keep the original needs_driver value")
    check(counter_ref._data.get("count") == 1,
          "counter should not increment for a resend")
    check(doc_ref._data.get("raw_text") == "John Kim, corrected: SDRP",
          "resend should still update the stored text")


def test_each_date_gets_its_own_independent_counter():
    """The whole point of keying everything by date: next Sunday's count
    must start fresh at zero, not carry over whatever last Sunday ended
    at. There's no explicit "reset" step anywhere in this feature - it's
    a consequence of the counter document ID being the date itself, so a
    new Sunday simply doesn't have a counter document yet until its
    first request creates one."""
    sunday_1_counter = _FakeDocRef()
    sunday_2_counter = _FakeDocRef()

    # Fill last Sunday (2026-09-13) up past capacity.
    last_result = None
    for i in range(30):
        doc_ref = _FakeDocRef()
        last_result = db_mod._apply_return_ride_request(
            _FakeTransaction(), doc_ref, sunday_1_counter, f"+1555{i:04d}",
            "2026-09-13", f"Rider {i}", 28,
        )
    check(last_result["position"] == 30,
          f"sanity check: last Sunday should reach position 30, got {last_result}")
    check(sunday_1_counter._data.get("count") == 30,
          "sanity check: last Sunday's counter should read 30")

    # First request of the NEW Sunday (2026-09-20) uses a fresh,
    # never-before-seen counter document - simulating what actually
    # happens in Firestore, where "2026-09-20" simply doesn't exist as
    # a document until this moment.
    first_of_new_week = db_mod._apply_return_ride_request(
        _FakeTransaction(), _FakeDocRef(), sunday_2_counter, "+15550000",
        "2026-09-20", "First rider of the new week", 28,
    )

    check(first_of_new_week["position"] == 1,
          f"new Sunday should start at position 1 regardless of last week's "
          f"total, got {first_of_new_week}")
    check(first_of_new_week["needs_driver"] is False,
          "new Sunday's first request should not need a driver")
    check(sunday_2_counter._data.get("count") == 1,
          "new Sunday's counter must be independent of last Sunday's")


# --------------------------------------------------------------------------
# functions.return_ride: keyword matching and parsing
# --------------------------------------------------------------------------
def test_matches_ride_keyword_is_whole_word_only():
    check(return_ride_mod.matches_ride_keyword("RIDE"), "bare RIDE should match")
    check(return_ride_mod.matches_ride_keyword("RIDE JOHN KIM FAR"),
          "RIDE plus argument should match")
    check(not return_ride_mod.matches_ride_keyword("RIDERS"),
          "RIDERS must not match RIDE (regression: the old collision)")
    check(not return_ride_mod.matches_ride_keyword("LIST"),
          "LIST (the renamed driver keyword) must not match RIDE")
    check(not return_ride_mod.matches_ride_keyword("RIDESHARE"),
          "a hypothetical future keyword sharing the prefix must not match")


def test_no_keyword_overlaps_with_other_live_keywords():
    # A cheap regression check for the exact bug this feature's rename
    # was meant to close: nothing return_ride.py owns should collide
    # with anything driver_sms_lookup.py or send_admin_summary.py own.
    import functions.driver_sms_lookup as driver_mod
    import functions.send_admin_summary as summary_mod

    other_keywords = (
        driver_mod.DRIVER_LOOKUP_KEYWORDS
        | summary_mod.ADMIN_SUMMARY_KEYWORDS
        # RESETME arrived after this feature was first written. Pulled
        # from the module rather than hardcoded so the next keyword
        # added there is covered here for free.
        | summary_mod.ADMIN_RESET_KEYWORDS
        | __import__("functions.rider_reminder", fromlist=["x"]).SKIP_KEYWORDS
        | {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "START", "YES", "HELP"}
    )
    overlap = other_keywords & return_ride_mod.REQUESTS_KEYWORDS
    check(not overlap, f"REQUESTS collides with existing keyword(s): {overlap}")

    # The RIDERS bug in reverse: RIDE is prefix-matched, so prove it
    # doesn't swallow any keyword another module owns.
    swallowed = sorted(k for k in other_keywords if return_ride_mod.matches_ride_keyword(k))
    check(not swallowed, f"RIDE swallows existing keyword(s): {swallowed}")
    check("RIDERS" not in driver_mod.DRIVER_LOOKUP_KEYWORDS,
          "RIDERS should be fully retired from the driver keyword set")
    check("LIST" in driver_mod.LIST_KEYWORDS, "LIST should be the driver keyword now")


def test_rides_is_an_alias_for_ride():
    for body in ("RIDES", "RIDES John Kim FAR"):
        check(return_ride_mod.matches_ride_keyword(body), f"{body!r} should match")


def test_the_plural_ride_is_not_parsed_as_the_singular_plus_an_s():
    # Why longest-first matters here rather than being tidiness: matching
    # RIDE against "RIDES John Kim FAR" would file a rider named
    # "S JOHN KIM FAR".
    check(return_ride_mod._matched_ride_keyword("RIDES JOHN KIM FAR") == "RIDES",
          "the longer spelling should win")
    check(return_ride_mod.parse_ride_command("RIDES JOHN KIM FAR") == "JOHN KIM FAR",
          f"got {return_ride_mod.parse_ride_command('RIDES JOHN KIM FAR')!r}")
    check(return_ride_mod.parse_ride_command("RIDE JOHN KIM FAR") == "JOHN KIM FAR",
          "the singular should still parse the same way")
    check(return_ride_mod.parse_ride_command("RIDES") is None,
          "a bare plural has no argument either")


def test_parse_ride_command():
    check(return_ride_mod.parse_ride_command("RIDE John Kim FAR") == "John Kim FAR",
          "should strip the keyword and leading space")
    check(return_ride_mod.parse_ride_command("RIDE") is None,
          "bare RIDE should parse to None")
    check(return_ride_mod.parse_ride_command("RIDE   ") is None,
          "RIDE with only trailing whitespace should also parse to None")


# --------------------------------------------------------------------------
# functions.return_ride: build_ride_reply (Firestore mocked)
# --------------------------------------------------------------------------
# RIDE only answers on Sunday, so every test below pins the day rather
# than inheriting whatever day it happens to run on. Without that these
# passed on Sundays and failed the rest of the week, which is a suite
# that lies to you in both directions.
def _on_sunday():
    return mock.patch.object(return_ride_mod, "is_sunday", return_value=True)


def _not_sunday():
    return mock.patch.object(return_ride_mod, "is_sunday", return_value=False)


def _not_admin():
    return mock.patch.object(return_ride_mod, "_is_test_exempt", return_value=False)


def test_ride_outside_sunday_says_when_to_come_back():
    with _not_sunday(), _not_admin(), \
         mock.patch.object(return_ride_mod, "record_return_ride_request") as record:
        reply = return_ride_mod.build_ride_reply("+15551234", "RIDE John Kim FAR")

    check("opens Sunday" in reply,
          f"a weekday request should say when sign-up opens: {reply!r}")
    check(not record.called,
          "a weekday request must not be recorded, or it sits on Sunday's "
          "list all week counting against the 28 seats")
    check(OPT_OUT_NOTICE in reply, "the reply should carry the opt-out notice")


def test_a_bare_ride_outside_sunday_gets_the_day_answer_not_the_format_hint():
    # Someone texting on a Wednesday needs to know it is not open yet,
    # not how to format a request they cannot make.
    with _not_sunday(), _not_admin():
        reply = return_ride_mod.build_ride_reply("+15551234", "RIDE")
    check("opens Sunday" in reply, f"expected the day answer, got {reply!r}")


def test_an_admin_can_test_on_any_day():
    with _not_sunday(), \
         mock.patch.object(return_ride_mod, "_is_test_exempt", return_value=True), \
         mock.patch.object(
             return_ride_mod, "record_return_ride_request",
             return_value={"position": 1, "needs_driver": False, "is_new": True},
         ) as record:
        reply = return_ride_mod.build_ride_reply("+17034010571", "RIDE Peter Hahn FAR")

    check(record.called, "an admin should be able to exercise this off-Sunday")
    check("on the list" in reply, f"admin should get the real ack, got {reply!r}")


def test_the_exemption_fails_closed():
    # If the allowlist check itself breaks, an ordinary rider must still
    # be gated. An admin who cannot test is a nuisance; a gate that
    # lets the whole church sign up on a Tuesday is a real problem.
    with mock.patch.dict("sys.modules", {"functions.send_admin_summary": None}):
        check(return_ride_mod._is_test_exempt("+15551234") is False,
              "a broken admin check must not open the gate")


def test_build_ride_reply_bare_keyword_gets_format_hint():
    with _on_sunday():
        reply = return_ride_mod.build_ride_reply("+15551234", "RIDE")
    check(reply.startswith(BRAND_PREFIX), "format hint should carry the brand prefix")
    check("RIDE followed by" in reply, "format hint should explain the format")


def test_build_ride_reply_normal_request_gets_ack_with_opt_out():
    with _on_sunday(), mock.patch.object(
        return_ride_mod, "record_return_ride_request",
        return_value={"position": 5, "needs_driver": False, "is_new": True},
    ) as mocked:
        reply = return_ride_mod.build_ride_reply("+15551234", "RIDE John Kim FAR")

    check(mocked.called, "should call record_return_ride_request")
    check(reply.startswith(BRAND_PREFIX), "ack should carry the brand prefix")
    check(OPT_OUT_NOTICE in reply,
          "ack should carry the opt-out notice (may be a rider's first text)")
    check("on the list" in reply, "ack should confirm the request was recorded")

    # The date tells the rider which SERVICE they were filed against,
    # not which day they happened to text. Compared against the same
    # helper the reply uses so this doesn't break every time the year
    # turns over.
    label = return_ride_mod._format_short_date(return_ride_mod._service_sunday())
    check(label in reply,
          f"ack should name the service date ({label}): {reply!r}")
    # Same message whether they're comfortably under capacity or over it -
    # no reason to tell someone by text whether they got a shuttle seat
    # or a personal driver before anyone has arranged one.
    with _on_sunday(), mock.patch.object(
        return_ride_mod, "record_return_ride_request",
        return_value={"position": 40, "needs_driver": True, "is_new": True},
    ):
        overflow_reply = return_ride_mod.build_ride_reply("+15559999", "RIDE Tom Suh PAR")
    check(overflow_reply == reply, "reply text must not vary with needs_driver")


def test_build_ride_reply_save_failure_still_replies():
    with _on_sunday(), mock.patch.object(
        return_ride_mod, "record_return_ride_request",
        side_effect=RuntimeError("firestore hiccup"),
    ):
        reply = return_ride_mod.build_ride_reply("+15551234", "RIDE John Kim FAR")
    check(reply.startswith(BRAND_PREFIX), "failure reply should still carry the brand prefix")
    check("usher" in reply.lower(), "failure reply should point to a human fallback")


# --------------------------------------------------------------------------
# functions.return_ride: build_requests_summary / build_requests_reply
# --------------------------------------------------------------------------
def _fake_request(position: int, needs_driver: bool, raw_text: str) -> dict:
    return {"position": position, "needs_driver": needs_driver, "raw_text": raw_text}


def _rows(names, capacity=28):
    return [
        {"position": i, "raw_text": n, "needs_driver": i > capacity}
        for i, n in enumerate(names, start=1)
    ]


def _full_lines(names, capacity=28):
    with mock.patch.object(
        return_ride_mod, "get_return_ride_requests_for_date",
        return_value=_rows(names, capacity),
    ):
        return return_ride_mod.build_requests_full_lines("2026-09-20")


def test_requests_keyword_matches_with_and_without_an_argument():
    for body in ("REQUESTS", "REQUEST", "REQUESTS ALL", "REQUEST FULL",
                 "REQUESTS names"):
        check(return_ride_mod.matches_requests_keyword(body), f"{body!r} should match")
    for body in ("REQUESTSALL", "REQUESTED", "REQ", "RIDE"):
        check(not return_ride_mod.matches_requests_keyword(body),
              f"{body!r} should NOT match")


def test_the_singular_is_an_alias_for_the_plural():
    # Nobody should have to remember whether the keyword has an S while
    # standing in a church lobby.
    check(return_ride_mod.matches_requests_keyword("REQUEST"),
          "REQUEST should work as well as REQUESTS")
    check(not return_ride_mod.wants_full_list("REQUEST"),
          "bare REQUEST should give the short summary, same as REQUESTS")
    check(return_ride_mod.wants_full_list("REQUEST ALL"),
          "REQUEST ALL should give the full list")


def test_the_plural_is_not_read_as_the_singular_plus_an_argument():
    # REQUESTS begins with REQUEST, so matching the short spelling first
    # would leave "S ALL" as the argument.
    check(return_ride_mod._matched_requests_keyword("REQUESTS ALL") == "REQUESTS",
          "the longer spelling should win")
    check(return_ride_mod._matched_requests_keyword("REQUESTS") == "REQUESTS",
          "a bare plural should match the plural")


def test_only_an_argument_asks_for_the_full_list():
    for body in ("REQUESTS", "REQUEST"):
        check(not return_ride_mod.wants_full_list(body),
              f"bare {body!r} should give the short summary")
    for body in ("REQUESTS ALL", "REQUEST FULL", "REQUESTS anything"):
        check(return_ride_mod.wants_full_list(body),
              f"{body!r} should ask for the full list")


def test_the_full_list_is_sorted_by_what_the_rider_typed():
    # Some enter a full name, some only a first name, and the casing is
    # whatever their keyboard did. Sorted on the raw text, case-folded.
    lines = _full_lines(["tom suh PAR", "Amy Ko ISR", "Dan", "john kim FAR"])
    entries = [l.split(". ", 1)[1] for l in lines if l[0].isdigit()]
    check(entries == ["Amy Ko ISR", "Dan", "john kim FAR", "tom suh PAR"],
          f"should be alphabetical regardless of case: {entries}")


def test_the_full_list_is_not_split_into_shuttle_and_driver():
    # Seats go to whoever boards first, so signup order does not decide
    # who ends up on a shuttle. Splitting the list on that basis would
    # put a confident label on a guess.
    names = [f"Rider {i:02d}" for i in range(1, 31)]
    lines = _full_lines(names, capacity=28)
    check("Need driver:" not in lines,
          f"there should be no shuttle/driver split: {lines[:3]}")
    entries = [l for l in lines if l[0].isdigit()]
    check(len(entries) == 30, f"all 30 riders should be listed, got {len(entries)}")


def test_the_full_list_header_still_says_how_many_are_past_capacity():
    names = [f"Rider {i:02d}" for i in range(1, 31)]
    header = _full_lines(names, capacity=28)[0]
    check("30" in header, f"header should carry the total: {header!r}")
    check("2 past the 28" in header,
          f"header should say how many exceed the seats: {header!r}")


def test_the_full_list_header_stays_quiet_under_capacity():
    header = _full_lines([f"Rider {i:02d}" for i in range(1, 11)])[0]
    check("past" not in header, f"nothing exceeds capacity: {header!r}")


def test_numbering_runs_down_the_printed_list_not_by_seat_position():
    # Seat positions are assigned at signup and jump around once sorted,
    # so the printed numbers are a reading aid and must stay sequential.
    lines = _full_lines(["zoe", "amy", "dan"])
    numbers = [int(l.split(".", 1)[0]) for l in lines if l[0].isdigit()]
    check(numbers == [1, 2, 3], f"numbering should be sequential, got {numbers}")


def test_an_empty_sunday_says_so_rather_than_printing_an_empty_list():
    lines = _full_lines([])
    check(len(lines) == 1 and "nobody yet" in lines[0], f"got {lines}")


def test_a_short_list_is_one_unnumbered_part():
    parts = return_ride_mod.split_into_parts(["Returns: 2", "1. Amy", "2. Dan"])
    check(len(parts) == 1, f"should fit in one part, got {len(parts)}")
    check("(1/1)" not in parts[0], f"a single part should not be numbered: {parts[0]!r}")


def test_a_long_list_is_split_and_every_part_is_numbered():
    lines = [f"{i}. Rider Number {i:02d} Somewhere Hall" for i in range(1, 41)]
    parts = return_ride_mod.split_into_parts(lines)
    check(len(parts) > 1, "40 riders should need more than one part")
    total = len(parts)
    for index, part in enumerate(parts, start=1):
        check(f"({index}/{total})" in part,
              f"part {index} should be labelled ({index}/{total}): {part[:40]!r}")
        check(part.startswith(return_ride_mod.BRAND_PREFIX),
              "every part should be branded, since each arrives as its own text")


def test_splitting_never_breaks_a_rider_across_two_texts():
    lines = [f"{i}. Rider Number {i:02d} Somewhere Hall" for i in range(1, 41)]
    parts = return_ride_mod.split_into_parts(lines)
    rebuilt = []
    for part in parts:
        rebuilt.extend(part.split("\n")[1:])  # drop each part's brand line
    check(rebuilt == lines,
          "every line should survive splitting, whole and in order")


def test_an_oversized_single_line_is_not_chopped_mid_address():
    long_line = "1. " + "A Very Long Address " * 30
    parts = return_ride_mod.split_into_parts([long_line])
    check(len(parts) == 1, "one line cannot be split across parts")
    check(long_line in parts[0], "the line should survive intact")


def test_the_disclosure_lands_on_the_last_part():
    lines = [f"{i}. Rider Number {i:02d} Somewhere Hall" for i in range(1, 41)]
    with mock.patch.object(return_ride_mod, "build_requests_full_lines", return_value=lines), \
         mock.patch.object(return_ride_mod, "was_disclosure_sent", return_value=False), \
         mock.patch.object(return_ride_mod, "record_disclosure_sent"):
        parts = return_ride_mod.build_requests_reply("+12246594130", full=True)

    check(len(parts) > 1, "this should be a multi-part reply")
    check(OPT_OUT_NOTICE in parts[-1],
          "the opt-out instruction belongs on the part the reader ends on")
    check(OPT_OUT_NOTICE not in parts[0],
          "and not buried in an earlier part they have scrolled past")


def _summary_for(count):
    requests = [_fake_request(i, i > 28, f"Rider {i:02d}") for i in range(1, count + 1)]
    with mock.patch.object(
        return_ride_mod, "get_return_ride_requests_for_date", return_value=requests
    ):
        return return_ride_mod.build_requests_summary("2026-09-13")


def test_requests_summary_zero_requested():
    summary = _summary_for(0)
    check("nobody yet" in summary, f"expected nobody yet, got: {summary!r}")


def test_requests_summary_under_capacity_reports_the_count_and_the_seats():
    summary = _summary_for(10)
    check("10 requested" in summary, f"unexpected counts line: {summary!r}")
    check("shuttles hold 28" in summary,
          f"should say what the shuttles hold: {summary!r}")
    check("personal driver" not in summary,
          "nobody needs a driver under capacity")


def test_requests_summary_at_exactly_capacity_needs_no_driver():
    summary = _summary_for(28)
    check("28 requested" in summary, f"unexpected counts line: {summary!r}")
    check("personal driver" not in summary,
          f"28 fits, so no driver is needed: {summary!r}")


def test_requests_summary_over_capacity_says_how_many_drivers():
    summary = _summary_for(31)
    check("31 requested" in summary, f"unexpected counts line: {summary!r}")
    check("3 need a personal driver" in summary,
          f"should say how many rides to arrange: {summary!r}")


def test_requests_summary_names_nobody():
    # Seats go to whoever boards first, so the system cannot know which
    # particular people will be left for a personal driver. Naming any
    # of them would be inventing an answer, which is what the old
    # "Over: ..." line did.
    summary = _summary_for(31)
    for forbidden in ("Over:", "Rider 29", "Rider 30", "Rider 31"):
        check(forbidden not in summary,
              f"the short summary should name nobody, found {forbidden!r}: {summary!r}")


def test_overflow_is_recomputed_rather_than_read_from_stored_flags():
    # needs_driver is written once at signup and never revisited, so
    # after a cancellation it describes a headcount that no longer
    # exists. Here every stored flag says "needs a driver" while the
    # actual total fits comfortably.
    requests = [_fake_request(i, True, f"Rider {i:02d}") for i in range(1, 6)]
    with mock.patch.object(
        return_ride_mod, "get_return_ride_requests_for_date", return_value=requests
    ):
        summary = return_ride_mod.build_requests_summary("2026-09-13")
    check("personal driver" not in summary,
          f"5 riders fit in 28 seats whatever the stored flags say: {summary!r}")


def test_requests_summary_never_carries_opt_out_notice_inline():
    # Matches build_admin_summary()'s equivalent rule: the bare counts
    # never carry it directly - build_requests_reply() below adds the
    # disclosure (which itself carries OPT_OUT_NOTICE) on first contact
    # only.
    with mock.patch.object(return_ride_mod, "get_return_ride_requests_for_date", return_value=[]):
        summary = return_ride_mod.build_requests_summary("2026-09-13")
    check(OPT_OUT_NOTICE not in summary,
          "bare REQUESTS summary should not carry the opt-out notice directly")


def test_requests_reply_first_contact_gets_disclosure_once():
    with mock.patch.object(return_ride_mod, "get_return_ride_requests_for_date", return_value=[]), \
         mock.patch.object(return_ride_mod, "was_disclosure_sent", return_value=False) as was_sent, \
         mock.patch.object(return_ride_mod, "record_disclosure_sent") as record_sent:
        first_reply = return_ride_mod.build_requests_reply("+12246594130", "2026-09-13")

    check(was_sent.called, "should check disclosure status")
    check(record_sent.called, "should record that disclosure was sent")
    # A list of parts now, and the disclosure rides on the LAST one so a
    # multi-part reply doesn't bury the opt-out instruction above the
    # part the reader ends on.
    check(OPT_OUT_NOTICE in first_reply[-1],
          "first-ever reply should carry the disclosure/opt-out notice")

    with mock.patch.object(return_ride_mod, "get_return_ride_requests_for_date", return_value=[]), \
         mock.patch.object(return_ride_mod, "was_disclosure_sent", return_value=True):
        second_reply = return_ride_mod.build_requests_reply("+12246594130", "2026-09-13")

    check(OPT_OUT_NOTICE not in second_reply,
          "a number already disclosed to (via REQUESTS or UPDATE) should not see it again")


def main() -> int:
    tests = [obj for name, obj in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()

    print(f"Ran {len(tests)} test functions.")
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILED:")
        for failure in _FAILURES:
            print(f"  - {failure}")
        return 1

    print("All return ride tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
