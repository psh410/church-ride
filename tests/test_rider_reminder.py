# Covers the Saturday night rider reminder and the SKIP keyword.
#
# Run it directly, from the repo root:
#
#     python3 tests/test_rider_reminder.py
#
# Nothing here touches Twilio, Sheets, Firestore or the network. Every
# outbound call is mocked, so it's safe to run any time.
#
# The case worth reading first is test_repeat_skip_still_confirms. The
# cancellation filter removes a rider from get_riders_for_sunday the
# moment they cancel, so the obvious implementation - look them up, then
# cancel - makes a second SKIP find nobody and go silent, leaving
# someone who just cancelled convinced it failed. That ordering is the
# thing these tests exist to hold in place.

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import functions.rider_reminder as rr

_FAILURES: list[str] = []
_RAN = 0


def check(condition: bool, message: str) -> None:
    if not condition:
        _FAILURES.append(message)


def _rider(name="John Kim", phone="217-555-0100", stop="FAR", consent=True):
    return {
        "name": name,
        "email": None,
        "phone": phone,
        "stop": stop,
        "shuttle_id": "shuttle_1",
        "grade": "",
        "submitted_at": "2026-09-17T10:00:00",
        "sms_consent": consent,
    }


SUNDAY = "2026-09-20"


# --------------------------------------------------------------------------
# Keyword safety
# --------------------------------------------------------------------------
def test_skip_keywords_do_not_collide_with_anything_live():
    import functions.driver_sms_lookup as driver_mod
    import functions.return_ride as return_mod
    import functions.send_admin_summary as summary_mod

    other = (
        driver_mod.DRIVER_LOOKUP_KEYWORDS
        | summary_mod.ADMIN_SUMMARY_KEYWORDS
        | summary_mod.ADMIN_RESET_KEYWORDS
        | return_mod.REQUESTS_KEYWORDS
        | {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT",
           "START", "YES", "HELP", "INFO"}
    )
    overlap = other & rr.SKIP_KEYWORDS
    check(not overlap, f"SKIP keywords collide with existing keyword(s): {overlap}")

    # RIDE is prefix-matched, so prove it doesn't swallow any of these.
    swallowed = sorted(k for k in rr.SKIP_KEYWORDS if return_mod.matches_ride_keyword(k))
    check(not swallowed, f"RIDE swallows SKIP keyword(s): {swallowed}")


def test_cancel_reserved_word_is_not_used():
    # CANCEL is Twilio-reserved: it never reaches the app and unsubscribes
    # the rider from everything. Using it here would silently opt people
    # out of the whole system.
    check("CANCEL" not in rr.SKIP_KEYWORDS,
          "CANCEL is reserved by Twilio and must never be a SKIP keyword")
    check(rr.SKIP_KEYWORD == "SKIP", "SKIP should be the advertised keyword")


def test_ambiguous_words_stay_out():
    # Keywords are global to the number, not scoped to a conversation, so
    # a bare NO would cancel a ride whatever the rider meant by it.
    for word in ("NO", "NAH", "N"):
        check(word not in rr.SKIP_KEYWORDS,
              f"{word!r} is too ambiguous to be a cancellation keyword")


# --------------------------------------------------------------------------
# Message text
# --------------------------------------------------------------------------
def test_reminder_names_stop_time_and_skip():
    msg = rr.build_rider_reminder("John Kim", "FAR", "9:05 AM", SUNDAY)
    check(msg.startswith(rr.BRAND_PREFIX), "reminder should carry the brand prefix")
    check("John" in msg and "Kim" not in msg, "reminder should use the first name only")
    check("FAR" in msg, "reminder should name the stop")
    check("9:05 AM" in msg, "reminder should name the pickup time")
    check("SKIP" in msg, "reminder should advertise SKIP")
    check("STOP to opt out" in msg, "reminder should carry the opt-out notice")
    check(len(msg) <= 160, f"reminder should be one segment, got {len(msg)}")


def test_reminder_omits_time_when_the_stop_has_none():
    msg = rr.build_rider_reminder("John Kim", "Other", None, SUNDAY)
    check("Other" in msg, "reminder should still name the stop")
    check("None" not in msg, f"missing time should be omitted, not printed: {msg!r}")


def test_cancel_confirmation_names_the_date_and_nothing_else():
    msg = rr._cancel_confirmation(SUNDAY)
    check("9/20/26" in msg, "confirmation should name the date cancelled")
    check("STOP to opt out" in msg, "confirmation should carry the opt-out notice")
    check(len(msg) <= 160, f"confirmation should be one segment, got {len(msg)}")


def test_no_message_carries_a_link():
    # Bare URLs in A2P traffic are a common carrier spam-filter trigger,
    # and a dropped message is worse than an inconvenient one. Checked
    # across every rider-facing message, not just the cancellation, so a
    # link can't quietly reappear in the reminder either.
    messages = {
        "cancel confirmation": rr._cancel_confirmation(SUNDAY),
        "reminder": rr.build_rider_reminder("John Kim", "FAR", "9:05 AM", SUNDAY),
    }
    for label, msg in messages.items():
        lowered = msg.lower()
        for marker in ("http", "www.", ".com", ".org", ".net"):
            check(marker not in lowered,
                  f"{label} should carry no link, found {marker!r}: {msg!r}")


# --------------------------------------------------------------------------
# SKIP behavior
# --------------------------------------------------------------------------
def test_signed_up_rider_gets_cancelled_and_confirmed():
    recorded, flagged = [], []
    with mock.patch.object(rr, "get_cancelled_phones_for_sunday", return_value=set()), \
         mock.patch.object(rr, "get_riders_for_sunday", return_value=[_rider()]), \
         mock.patch.object(rr, "record_ride_cancellation",
                           side_effect=lambda *a, **k: recorded.append(a) or {"is_new": True}), \
         mock.patch.object(rr, "find_signup_row_for_phone", return_value=7), \
         mock.patch.object(rr, "flag_signup_cancelled",
                           side_effect=lambda row: flagged.append(row)):
        reply = rr.build_skip_reply("+12175550100", SUNDAY)

    check(reply is not None, "a signed-up rider should get a reply")
    check("cancelled" in reply.lower(), f"reply should confirm cancellation: {reply!r}")
    check(len(recorded) == 1, "the cancellation should be recorded exactly once")
    check(flagged == [7], f"the sheet row should be flagged, got {flagged}")


def test_repeat_skip_still_confirms():
    # The whole reason the already-cancelled check runs BEFORE the rider
    # lookup. get_riders_for_sunday is empty here, standing in for the
    # filter having removed them, and the reply must still come back.
    with mock.patch.object(rr, "get_cancelled_phones_for_sunday",
                           return_value={"+12175550100"}), \
         mock.patch.object(rr, "get_riders_for_sunday", return_value=[]), \
         mock.patch.object(rr, "record_ride_cancellation") as record:
        reply = rr.build_skip_reply("+12175550100", SUNDAY)

    check(reply is not None, "a repeat SKIP must not go silent")
    check("cancelled" in reply.lower(), "a repeat SKIP should confirm again")
    check(not record.called, "a repeat SKIP should not write a second record")


def test_unknown_number_gets_silence():
    with mock.patch.object(rr, "get_cancelled_phones_for_sunday", return_value=set()), \
         mock.patch.object(rr, "get_riders_for_sunday", return_value=[_rider()]):
        reply = rr.build_skip_reply("+19998887777", SUNDAY)

    check(reply is None, f"a number with no signup should get silence, got {reply!r}")


def test_sheet_write_failure_does_not_lose_the_cancellation():
    recorded = []
    with mock.patch.object(rr, "get_cancelled_phones_for_sunday", return_value=set()), \
         mock.patch.object(rr, "get_riders_for_sunday", return_value=[_rider()]), \
         mock.patch.object(rr, "record_ride_cancellation",
                           side_effect=lambda *a, **k: recorded.append(a) or {"is_new": True}), \
         mock.patch.object(rr, "find_signup_row_for_phone", return_value=7), \
         mock.patch.object(rr, "flag_signup_cancelled",
                           side_effect=RuntimeError("sheet is read-only")):
        reply = rr.build_skip_reply("+12175550100", SUNDAY)

    check(len(recorded) == 1, "the Firestore record must still be written")
    check(reply is not None and "cancelled" in reply.lower(),
          "the rider should still be told it worked, because it did")


def test_firestore_failure_tells_the_rider_rather_than_lying():
    with mock.patch.object(rr, "get_cancelled_phones_for_sunday", return_value=set()), \
         mock.patch.object(rr, "get_riders_for_sunday", return_value=[_rider()]), \
         mock.patch.object(rr, "record_ride_cancellation",
                           side_effect=RuntimeError("firestore down")):
        reply = rr.build_skip_reply("+12175550100", SUNDAY)

    check(reply is not None, "a failure should still produce a reply")
    check("couldn't cancel" in reply.lower(),
          f"reply should admit the failure, not confirm: {reply!r}")


def test_phone_matching_survives_sheet_formatting():
    for typed in ("217-555-0100", "(217) 555-0100", "2175550100", "+1 217 555 0100"):
        with mock.patch.object(rr, "get_cancelled_phones_for_sunday", return_value=set()), \
             mock.patch.object(rr, "get_riders_for_sunday",
                               return_value=[_rider(phone=typed)]), \
             mock.patch.object(rr, "record_ride_cancellation", return_value={"is_new": True}), \
             mock.patch.object(rr, "find_signup_row_for_phone", return_value=None):
            reply = rr.build_skip_reply("+12175550100", SUNDAY)
        check(reply is not None, f"should match a sheet phone written as {typed!r}")


# --------------------------------------------------------------------------
# The Saturday send
# --------------------------------------------------------------------------
def _run_send(riders, opted_out=()):
    sent = []
    with mock.patch.object(rr, "get_riders_for_sunday", return_value=riders), \
         mock.patch.object(rr, "get_stop_times_map", return_value={"FAR": "9:05 AM"}), \
         mock.patch.object(rr, "is_phone_opted_out",
                           side_effect=lambda p: p in opted_out), \
         mock.patch.object(rr, "send_sms",
                           side_effect=lambda to, body: sent.append((to, body)) or True):
        result = rr.send_saturday_rider_reminders(SUNDAY)
    return result, sent


def test_only_consenting_riders_are_texted():
    riders = [
        _rider(name="Yes Kim", phone="217-555-0100", consent=True),
        _rider(name="No Park", phone="217-555-0200", consent=False),
    ]
    result, sent = _run_send(riders)
    check(result["sent"] == 1, f"exactly one rider should be texted, got {result['sent']}")
    check(result["skipped"] == 1, f"the non-consenting rider should be skipped")
    check(sent[0][0] == "+12175550100", f"wrong recipient: {sent[0][0]}")


def test_opted_out_riders_are_skipped():
    riders = [_rider(phone="217-555-0100", consent=True)]
    result, sent = _run_send(riders, opted_out={"+12175550100"})
    check(result["sent"] == 0, "an opted-out rider should not be texted")
    check(sent == [], "nothing should have been sent")


def test_unusable_phone_number_is_skipped_not_fatal():
    riders = [
        _rider(name="Bad Number", phone="", consent=True),
        _rider(name="Good One", phone="217-555-0100", consent=True),
    ]
    result, _ = _run_send(riders)
    check(result["sent"] == 1, "the good number should still be texted")
    check(result["skipped"] == 1, "the unusable number should be skipped")
    check(result["failed"] == 0, "a bad number is a skip, not a failure")


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
    print("All rider reminder tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
