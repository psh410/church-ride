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
def test_build_ride_reply_bare_keyword_gets_format_hint():
    reply = return_ride_mod.build_ride_reply("+15551234", "RIDE")
    check(reply.startswith(BRAND_PREFIX), "format hint should carry the brand prefix")
    check("RIDE followed by" in reply, "format hint should explain the format")


def test_build_ride_reply_normal_request_gets_ack_with_opt_out():
    with mock.patch.object(
        return_ride_mod, "record_return_ride_request",
        return_value={"position": 5, "needs_driver": False, "is_new": True},
    ) as mocked:
        reply = return_ride_mod.build_ride_reply("+15551234", "RIDE John Kim FAR")

    check(mocked.called, "should call record_return_ride_request")
    check(reply.startswith(BRAND_PREFIX), "ack should carry the brand prefix")
    check(OPT_OUT_NOTICE in reply,
          "ack should carry the opt-out notice (may be a rider's first text)")
    check("on the list" in reply, "ack should confirm the request was recorded")

    # The date tells the rider which day they were actually filed under,
    # which is the only signal they get when a late Sunday text rolls
    # into Monday under UTC. Compared against the same helper the reply
    # uses so this doesn't break every time the year turns over.
    today_label = return_ride_mod._format_short_date(return_ride_mod._today())
    check(today_label in reply,
          f"ack should name the date it filed under ({today_label}): {reply!r}")
    # Same message whether they're comfortably under capacity or over it -
    # no reason to tell someone by text whether they got a shuttle seat
    # or a personal driver before anyone has arranged one.
    with mock.patch.object(
        return_ride_mod, "record_return_ride_request",
        return_value={"position": 40, "needs_driver": True, "is_new": True},
    ):
        overflow_reply = return_ride_mod.build_ride_reply("+15559999", "RIDE Tom Suh PAR")
    check(overflow_reply == reply, "reply text must not vary with needs_driver")


def test_build_ride_reply_save_failure_still_replies():
    with mock.patch.object(
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


def test_requests_summary_zero_requested():
    with mock.patch.object(return_ride_mod, "get_return_ride_requests_for_date", return_value=[]):
        summary = return_ride_mod.build_requests_summary("2026-09-13")

    check("0 requested" in summary, f"expected 0 requested, got: {summary!r}")
    check("Over:" not in summary, "should have no Over line at zero requests")


def test_requests_summary_under_capacity_has_no_over_line():
    requests = [_fake_request(i, False, f"Rider {i}") for i in range(1, 11)]
    with mock.patch.object(return_ride_mod, "get_return_ride_requests_for_date", return_value=requests):
        summary = return_ride_mod.build_requests_summary("2026-09-13")

    check("10 requested (10 shuttle, 0 need drivers)" in summary,
          f"unexpected counts line: {summary!r}")
    check("Over:" not in summary, "should have no Over line under capacity")


def test_requests_summary_exactly_at_capacity_has_no_over_line():
    requests = [_fake_request(i, False, f"Rider {i}") for i in range(1, 29)]
    with mock.patch.object(return_ride_mod, "get_return_ride_requests_for_date", return_value=requests):
        summary = return_ride_mod.build_requests_summary("2026-09-13")

    check("28 requested (28 shuttle, 0 need drivers)" in summary,
          f"unexpected counts line: {summary!r}")
    check("Over:" not in summary, "should have no Over line at exactly capacity")


def test_requests_summary_over_capacity_lists_overflow():
    requests = [_fake_request(i, False, f"Rider {i}") for i in range(1, 29)]
    requests += [_fake_request(29, True, "John Kim FAR"), _fake_request(30, True, "Tom Suh PAR")]
    with mock.patch.object(return_ride_mod, "get_return_ride_requests_for_date", return_value=requests):
        summary = return_ride_mod.build_requests_summary("2026-09-13")

    check("30 requested (28 shuttle, 2 need drivers)" in summary,
          f"unexpected counts line: {summary!r}")
    check("Over: John Kim FAR, Tom Suh PAR" in summary,
          f"expected both overflow names listed, got: {summary!r}")


def test_requests_summary_truncates_long_overflow_list():
    overflow = [_fake_request(28 + i, True, f"Rider {i}") for i in range(1, 12)]  # 11 overflow
    requests = [_fake_request(i, False, f"Shuttle rider {i}") for i in range(1, 29)] + overflow
    with mock.patch.object(return_ride_mod, "get_return_ride_requests_for_date", return_value=requests):
        summary = return_ride_mod.build_requests_summary("2026-09-13")

    check("+3 more" in summary,
          f"expected truncation tail for 11 overflow names shown {return_ride_mod.MAX_OVERFLOW_NAMES_SHOWN} "
          f"at a time, got: {summary!r}")
    check(summary.count("Rider 1") + summary.count("Rider 2") <= return_ride_mod.MAX_OVERFLOW_NAMES_SHOWN,
          "should not list more than MAX_OVERFLOW_NAMES_SHOWN full names")


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
    check(OPT_OUT_NOTICE in first_reply, "first-ever reply should carry the disclosure/opt-out notice")

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
