# Guards the rules every outbound SMS has to follow: it starts with
# BRAND_PREFIX so recipients know it's from CFC, and (with one
# deliberate exception) it ends with OPT_OUT_NOTICE so they know how to
# stop.
#
# The exception is the admin summary, the reply to an admin's UPDATE
# text. Twilio requires opt-out language in the initial message to a
# recipient, not in every reply within a conversation the recipient
# started, and we keep it off that one on purpose - see the comment in
# functions/send_admin_summary.py. This test pins that decision down so
# nobody adds it back by accident, and so nobody removes it from the
# messages that do need it.
#
# This exists because the wording drifted three separate ways before the
# constants in functions/send_sms.py were introduced: the live senders
# said "CFC:", the registered campaign samples said "CFC Rides:", and
# the admin summary had no branding at all. A test is cheaper than
# noticing again by reading a text message.
#
# Run it directly (no pytest needed), from the repo root with the venv
# active and a .env present:
#
#     python3 tests/test_sms_branding.py
#
# Nothing here touches Twilio, Sheets, Firestore or the network - every
# outbound call is mocked, so it's safe to run any time.

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import functions.send_admin_summary as summary_mod
import functions.send_driver_sms_reminder as driver_mod
import functions.send_rider_confirmation as rider_mod
from functions.send_sms import BRAND_PREFIX, OPT_OUT_NOTICE

# Messages that intentionally omit the opt-out notice.
_NO_OPT_OUT_NOTICE = {"admin summary"}

_STOP_MAP = {"FAR": "shuttle_1", "SDRP": "shuttle_2"}
_ROUTES = [
    {
        "shuttle_id": "shuttle_1",
        "stops": [
            {"stop_name": "FAR", "pickup_time": "9:05 AM"},
            {"stop_name": "SDRP", "pickup_time": "9:10 AM"},
        ],
    }
]
_DRIVERS = [
    {"name": "Sangwoo Kim", "phone": "217-555-0100"},
    {"name": "Daniel Park", "phone": "217-555-0111"},
    {"name": "Yong Kim", "phone": "217-555-0122"},
]
_SCHEDULE = [
    {
        "date": "2026-09-13",
        "shuttle_1": "Sangwoo Kim",
        "shuttle_2": "Peter Hahn",
        "backup": "Yong Kim",
    }
]


def _rider(stop: str, shuttle_id: str | None) -> dict:
    return {
        "name": "Rider",
        "email": None,
        "phone": "",
        "stop": stop,
        "shuttle_id": shuttle_id,
        "grade": "",
        "submitted_at": "",
    }


_RIDERS = (
    [_rider("FAR", "shuttle_1") for _ in range(9)]
    + [_rider("SDRP", "shuttle_2") for _ in range(7)]
    + [_rider("Other", None) for _ in range(11)]
)


def _segments(text: str) -> int:
    """Rough GSM-7 segment count: 160 alone, 153 each once concatenated."""
    return 1 if len(text) <= 160 else -(-len(text) // 153)


def collect_messages() -> dict[str, str]:
    """Build one of every outbound message type, without sending anything."""
    messages: dict[str, str] = {}
    sent: list[str] = []

    def fake_send_sms(to, body):
        sent.append(body)
        return True

    with mock.patch.object(summary_mod, "get_riders_for_sunday", return_value=_RIDERS), \
         mock.patch.object(summary_mod, "get_stop_to_shuttle_map", return_value=_STOP_MAP), \
         mock.patch.object(summary_mod, "get_semester_schedule", return_value=_SCHEDULE):
        messages["admin summary"] = summary_mod.build_admin_summary("2026-09-13")

    sent.clear()
    with mock.patch.object(rider_mod, "send_sms", fake_send_sms), \
         mock.patch.object(rider_mod, "_lookup_pickup_time", return_value="9:10 AM"), \
         mock.patch.object(rider_mod, "get_stop_to_shuttle_map", return_value=_STOP_MAP):
        rider_mod.send_rider_confirmation(
            name="Peter Hahn", phone="+17034010571", stop="SDRP"
        )
    messages["rider confirmation"] = sent[0]

    sent.clear()
    with mock.patch.object(driver_mod, "send_sms", fake_send_sms), \
         mock.patch.object(driver_mod, "is_phone_opted_out", return_value=False):
        # A split week, so the pickup and return-leg wordings both get built.
        driver_mod._remind_shuttle(
            "shuttle_1",
            {"shuttle_1_pickup": "Sangwoo Kim", "shuttle_1_return": "Daniel Park"},
            _ROUTES,
            _DRIVERS,
        )
        driver_mod._remind_backup({"backup": "Yong Kim"}, _DRIVERS)

    messages["driver reminder (pickup)"] = sent[0]
    messages["driver reminder (return leg)"] = sent[1]
    messages["driver reminder (backup)"] = sent[2]
    return messages


def main() -> int:
    messages = collect_messages()
    failures: list[str] = []

    for label, text in messages.items():
        if not text.startswith(BRAND_PREFIX):
            failures.append(f"{label}: missing brand prefix {BRAND_PREFIX!r}")

        if label in _NO_OPT_OUT_NOTICE:
            if OPT_OUT_NOTICE in text:
                failures.append(
                    f"{label}: should NOT carry the opt-out notice "
                    "(it's a reply to a conversation the admin started)"
                )
        elif OPT_OUT_NOTICE not in text:
            failures.append(f"{label}: missing opt-out notice {OPT_OUT_NOTICE!r}")

        print(f"[{_segments(text)} segment(s), {len(text):3d} chars] {label}")
        print(f"    {text.replace(chr(10), chr(10) + '    ')}")

    if len(messages) != 5:
        failures.append(f"expected 5 message types, built {len(messages)}")

    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(
        f"All {len(messages)} message types branded correctly "
        f"({len(messages) - len(_NO_OPT_OUT_NOTICE)} with the opt-out notice, "
        f"{len(_NO_OPT_OUT_NOTICE)} intentionally without)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
