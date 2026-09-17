# Pins down who gets a signup confirmation text, who gets told they
# already signed up, and who gets nothing at all.
#
# The rules are easy to break by accident because they live in two
# different places. The Apps Script flags a repeat submission in the
# Google Sheet; Firestore separately records that a confirmation went
# out. Deleting the sheet row does not clear Firestore, and the sheet
# flag says nothing about whether a text was ever actually sent. Every
# case below is one of those two sources disagreeing with the other.
#
# Run it directly (no pytest needed), from the repo root with the venv
# active and a .env present:
#
#     python3 tests/test_rider_confirmation.py
#
# Nothing here touches Twilio, Sheets, Firestore or the network.

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import functions.send_admin_summary as summary_mod
import functions.send_rider_confirmation as rider_mod
from functions.send_sms import BRAND_PREFIX, OPT_OUT_NOTICE

SUNDAY = "2026-09-20"
STOP_MAP = {"Sherman Hall": "shuttle_1", "FAR": "shuttle_2"}

results: list[tuple[str, bool]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    results.append((label, bool(condition)))
    print(("PASS" if condition else "FAIL"), "-", label, detail if not condition else "")


def signup(stop: str, consent: bool = True, name: str = "Peter Hahn") -> dict:
    return {
        "name": name,
        "email": "peter@example.com",
        "phone": "703-401-0571",
        "stop": stop,
        "grade": "Career",
        "submitted_at": "9/14/2026 11:13:23",
        "sms_consent": consent,
    }


def run(row: int, row_data: dict, prior: dict | None, sheet_rows=None):
    """Call confirm_signup with everything external mocked out.

    Args:
        sheet_rows: What find_signup_rows_for_phone returns, or a
            RuntimeError instance to simulate an unreadable sheet.
            Defaults to an earlier row plus this one, which is the
            ordinary "they really did sign up twice" case. Pass [row] to
            model an admin having deleted the earlier row, since the
            sheet is what decides whether somebody is signed up.

    Returns:
        tuple: (result dict, list of (to, body) sent, the
            record_rider_confirmed mock, the record_duplicate_notice_sent
            mock).
    """
    sent: list[tuple[str, str]] = []
    if sheet_rows is None:
        sheet_rows = [row - 1, row]
    lookup = (
        mock.patch.object(rider_mod, "find_signup_rows_for_phone",
                          side_effect=sheet_rows)
        if isinstance(sheet_rows, RuntimeError)
        else mock.patch.object(rider_mod, "find_signup_rows_for_phone",
                               return_value=sheet_rows)
    )

    with lookup, \
         mock.patch.object(rider_mod, "get_signup_row", return_value=row_data), \
         mock.patch.object(rider_mod, "get_next_sunday_date", return_value=SUNDAY), \
         mock.patch.object(rider_mod, "get_rider_confirmation", return_value=prior), \
         mock.patch.object(rider_mod, "get_stop_to_shuttle_map", return_value=STOP_MAP), \
         mock.patch.object(rider_mod, "_lookup_pickup_time", return_value="9:05 AM"), \
         mock.patch.object(rider_mod, "record_rider_confirmed") as m_confirmed, \
         mock.patch.object(rider_mod, "record_duplicate_notice_sent") as m_notice, \
         mock.patch.object(
             rider_mod,
             "send_sms",
             side_effect=lambda to, body: (sent.append((to, body)), True)[1],
         ):
        result = rider_mod.confirm_signup(row)

    return result, sent, m_confirmed, m_notice


# --- 1. First signup at a real stop -----------------------------------
result, sent, m_confirmed, m_notice = run(530, signup("Sherman Hall"), None)
check("first signup sends", result["status"] == "sent", str(result))
check("first signup goes to the normalized number",
      bool(sent) and sent[0][0] == "+17034010571", str(sent[:1]))
check("first signup quotes the stop and time",
      bool(sent) and "Sherman Hall" in sent[0][1] and "9:05 AM" in sent[0][1],
      sent[0][1] if sent else "")
check("first signup is recorded", m_confirmed.called)
check("first signup records the row it came from",
      m_confirmed.called and m_confirmed.call_args[0][2]["row"] == 530)

# --- 2. Repeat signup: told once, with the ORIGINAL details -----------
prior = {"row": 530, "stop": "Sherman Hall", "name": "Peter Hahn"}
result, sent, m_confirmed, m_notice = run(611, signup("FAR/duplicate"), prior)
check("repeat signup sends a notice", result["status"] == "sent", str(result))
check("repeat notice says they're already signed up",
      bool(sent) and "already signed up" in sent[0][1], sent[0][1] if sent else "")
check("repeat notice quotes the ORIGINAL stop, not the duplicate row",
      bool(sent) and "Sherman Hall" in sent[0][1] and "FAR" not in sent[0][1],
      sent[0][1] if sent else "")
check("repeat notice is capped by recording it", m_notice.called)
check("repeat signup does not overwrite the original record", not m_confirmed.called)

# --- 3. Third submission: capped, silent ------------------------------
prior_capped = dict(prior, duplicate_notice_sent=True)
result, sent, _, m_notice = run(612, signup("FAR/duplicate"), prior_capped)
check("third submission sends nothing", result["status"] == "skipped", str(result))
check("third submission reason names the cap",
      result.get("reason") == "duplicate notice already sent", str(result))
check("third submission texts nobody", not sent)

# --- 4. Same row twice is a retry, not a repeat signup ----------------
result, sent, _, _ = run(530, signup("Sherman Hall"), prior)
check("retry of the same row sends nothing", result["status"] == "skipped", str(result))
check("retry reason is 'already confirmed'",
      result.get("reason") == "already confirmed this week", str(result))
check("retry texts nobody", not sent)

# --- 5. Flagged duplicate with NO record: this is their first text ----
# Happens when the earlier submission left the consent box unchecked.
# They were never told anything, so the flag is stripped and they get a
# normal confirmation.
result, sent, m_confirmed, _ = run(611, signup("Sherman Hall/duplicate"), None)
check("flagged row with no record still sends", result["status"] == "sent", str(result))
check("flagged row gets a normal confirmation, not a duplicate notice",
      bool(sent) and "already signed up" not in sent[0][1], sent[0][1] if sent else "")
check("the /duplicate flag is stripped off the stop",
      bool(sent) and "Sherman Hall" in sent[0][1] and "duplicate" not in sent[0][1],
      sent[0][1] if sent else "")
check("the stored stop is stripped too",
      m_confirmed.called and m_confirmed.call_args[0][2]["stop"] == "Sherman Hall",
      str(m_confirmed.call_args) if m_confirmed.called else "")

# --- 6. No consent beats everything -----------------------------------
result, sent, _, _ = run(613, signup("Sherman Hall", consent=False), None)
check("no consent sends nothing", result["status"] == "skipped", str(result))
check("no consent reason is consent", result.get("reason") == "no sms consent", str(result))
check("no consent texts nobody", not sent)

result, sent, _, _ = run(614, signup("FAR/duplicate", consent=False), prior)
check("no consent blocks the duplicate notice too", not sent, str(sent))

# --- 7. The three duplicate wordings ----------------------------------
with mock.patch.object(rider_mod, "get_stop_to_shuttle_map", return_value=STOP_MAP), \
     mock.patch.object(rider_mod, "_lookup_pickup_time", return_value="9:05 AM"):
    dup_stop = rider_mod.build_duplicate_message("Peter Hahn", "Sherman Hall")
    dup_full = rider_mod.build_duplicate_message("Peter Hahn", "Sherman Hall/driver")
    dup_off = rider_mod.build_duplicate_message("Peter Hahn", "H mart")
    dup_none = rider_mod.build_duplicate_message("Peter Hahn", "")

check("duplicate/stop names the pickup time", "9:05 AM" in dup_stop, dup_stop)
check("duplicate/full says the shuttle is full", "shuttle is full" in dup_full, dup_full)
check("duplicate/off-route says not on a route", "isn't on a shuttle route" in dup_off, dup_off)
check("duplicate with no stored stop stays vague", "No need to submit again" in dup_none, dup_none)

for label, text in [("stop", dup_stop), ("full", dup_full), ("off-route", dup_off), ("none", dup_none)]:
    check(f"duplicate/{label} is branded", text.startswith(BRAND_PREFIX), text[:40])
    check(f"duplicate/{label} carries the opt-out notice", OPT_OUT_NOTICE in text, text[-50:])

print("\nDuplicate message lengths:")
for label, text in [("stop", dup_stop), ("full", dup_full), ("off-route", dup_off), ("none", dup_none)]:
    segs = 1 if len(text) <= 160 else -(-len(text) // 153)
    print(f"  {segs} segment(s), {len(text):3d} chars - {label}")

# --- 8. RESETME -------------------------------------------------------
with mock.patch.object(summary_mod, "get_next_sunday_date", return_value=SUNDAY), \
     mock.patch.object(summary_mod, "clear_rider_confirmation", return_value=True) as m_clear, \
     mock.patch.object(summary_mod, "clear_return_ride_request", return_value=False):
    reply = summary_mod.build_reset_reply("703-401-0571")
check("RESETME clears the sender's own number",
      m_clear.call_args[0][0] == "+17034010571", str(m_clear.call_args))
check("RESETME clears the coming Sunday", m_clear.call_args[0][1] == SUNDAY)
check("RESETME confirms what it cleared", "Cleared" in reply and SUNDAY in reply, reply)
check("RESETME reply is branded", reply.startswith(BRAND_PREFIX), reply[:40])

with mock.patch.object(summary_mod, "get_next_sunday_date", return_value=SUNDAY), \
     mock.patch.object(summary_mod, "clear_rider_confirmation", return_value=False), \
     mock.patch.object(summary_mod, "clear_return_ride_request", return_value=False):
    reply_empty = summary_mod.build_reset_reply("703-401-0571")
check("RESETME says so when there was nothing to clear",
      "Nothing to clear" in reply_empty, reply_empty)

# RESETME also clears a return ride request, because an admin testing
# RIDE outside Sunday puts a real row on a real service's list where it
# counts against the 28 seats.
with mock.patch.object(summary_mod, "get_next_sunday_date", return_value=SUNDAY), \
     mock.patch.object(summary_mod, "clear_rider_confirmation", return_value=False), \
     mock.patch.object(summary_mod, "clear_return_ride_request", return_value=True) as m_ride:
    reply_ride = summary_mod.build_reset_reply("703-401-0571")
check("RESETME clears the return ride request too",
      "return ride request" in reply_ride, reply_ride)
check("RESETME clears the ride request for the sender's own number",
      m_ride.call_args[0][0] == "+17034010571", str(m_ride.call_args))

with mock.patch.object(summary_mod, "get_next_sunday_date", return_value=SUNDAY), \
     mock.patch.object(summary_mod, "clear_rider_confirmation", return_value=True), \
     mock.patch.object(summary_mod, "clear_return_ride_request", return_value=True):
    reply_both = summary_mod.build_reset_reply("703-401-0571")
check("RESETME names both when it cleared both",
      "signup confirmation" in reply_both and "return ride request" in reply_both,
      reply_both)

# One failing must not leave the other silently unreported.
with mock.patch.object(summary_mod, "get_next_sunday_date", return_value=SUNDAY), \
     mock.patch.object(summary_mod, "clear_rider_confirmation", return_value=True), \
     mock.patch.object(summary_mod, "clear_return_ride_request",
                       side_effect=RuntimeError("firestore down")):
    reply_partial = summary_mod.build_reset_reply("703-401-0571")
check("RESETME reports a partial failure instead of claiming success",
      "Couldn't clear" in reply_partial and "return ride request" in reply_partial,
      reply_partial)

with mock.patch.object(summary_mod, "get_next_sunday_date", return_value=SUNDAY), \
     mock.patch.object(
         summary_mod, "clear_rider_confirmation", side_effect=RuntimeError("firestore down")
     ):
    reply_broken = summary_mod.build_reset_reply("703-401-0571")
check("RESETME reports a Firestore failure instead of claiming success",
      "Couldn't clear" in reply_broken, reply_broken)

check("RESETME is not a Twilio-reserved word",
      not (summary_mod.ADMIN_RESET_KEYWORDS
           & {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "START", "YES", "HELP", "INFO"}),
      str(summary_mod.ADMIN_RESET_KEYWORDS))
from functions.driver_sms_lookup import DRIVER_LOOKUP_KEYWORDS  # noqa: E402

check("RESETME doesn't collide with the other keywords",
      not (summary_mod.ADMIN_RESET_KEYWORDS
           & (summary_mod.ADMIN_SUMMARY_KEYWORDS | DRIVER_LOOKUP_KEYWORDS)))

print()
failed = [label for label, ok in results if not ok]
if failed:
    print(f"{len(failed)} FAILED: {failed}")
    sys.exit(1)
print(f"All {len(results)} checks passed.")


# --- 9. The sheet decides who is signed up ----------------------------
# Firestore records what we have already told somebody. The sheet
# records who signed up. Those are different questions, and admins
# delete rows without Firestore ever hearing about it, so a stored
# confirmation whose row is gone describes a signup that no longer
# exists.
PRIOR = {"row": 500, "stop": "506 E Stoughton", "name": "Peter Hahn"}

result, sent, m_confirmed, m_notice = run(
    530, signup("FAR"), PRIOR, sheet_rows=[530]
)
check("a deleted earlier row means this is a first signup",
      result["status"] == "sent", str(result))
check("and they get the real confirmation, not the duplicate notice",
      sent and "already signed up" not in sent[0][1], sent[0][1] if sent else "(nothing sent)")
check("which names the stop from the NEW row, not the stale record",
      sent and "FAR" in sent[0][1] and "Stoughton" not in sent[0][1],
      sent[0][1] if sent else "(nothing sent)")
check("and the stale record is overwritten", m_confirmed.called)
check("no duplicate notice is recorded", not m_notice.called)

result, sent, _, m_notice = run(530, signup("FAR"), PRIOR)
check("an earlier row that still exists is a real duplicate",
      result["status"] in {"sent", "skipped"}, str(result))
check("and gets the duplicate wording",
      not sent or "already signed up" in sent[0][1],
      sent[0][1] if sent else "(nothing sent)")

result, sent, _, _ = run(
    530, signup("FAR"), PRIOR, sheet_rows=RuntimeError("sheet unreadable")
)
check("an unreadable sheet keeps the stored confirmation authoritative",
      not sent or "already signed up" in sent[0][1],
      sent[0][1] if sent else "(nothing sent)")

print()
print("The sheet decides: a confirmation whose row was deleted no longer")
print("blocks a fresh signup, and an unreadable sheet fails closed rather")
print("than re-texting everyone it could not verify.")
