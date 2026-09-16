# Saturday night rider reminder, and the SKIP keyword that answers it.
#
# At 9:30pm Saturday every consenting shuttle rider gets one text naming
# their stop and pickup time for the morning, ending with "Reply SKIP to
# cancel". A rider who replies gives up their seat: the cancellation is
# recorded in Firestore, their signup row is flagged "/cancelled" in the
# sheet, and they drop out of every count from that moment.
#
# Why a reminder at all: signups close Sunday 9am but most arrive days
# earlier, and plans change. Without a cancellation path a van leaves
# with empty seats while someone who wanted a ride was turned away as
# over capacity. The reminder exists to make cancelling easy enough that
# people actually do it.
#
# SKIP is the advertised word. NORIDE, OUT and CANT are accepted but
# never advertised, because what people actually type when they don't
# re-read the instruction is not the word you chose. SKIP was picked
# over the more idiomatic OUT because a good share of these riders are
# international students and "I'm out" does not travel; see
# claude/sms-keywords.md.
#
# Shuttle riders only. Riders flagged "/driver" have a personal driver
# arranged by hand and none of those details live in this system, so a
# text naming a stop and a pickup time would be wrong for them.

from __future__ import annotations

import logging
from datetime import datetime

from db.firestore_client import (
    get_cancelled_phones_for_sunday,
    is_phone_opted_out,
    record_ride_cancellation,
)
from functions.read_riders_sheet import (
    find_signup_row_for_phone,
    get_next_sunday_date,
    get_riders_for_sunday,
    get_stop_times_map,
)
from functions.send_sms import (
    BRAND_PREFIX,
    OPT_OUT_NOTICE,
    normalize_to_e164,
    send_sms,
)
from functions.write_riders_sheet import flag_signup_cancelled

logger = logging.getLogger(__name__)

# The word the reminder tells riders to use.
SKIP_KEYWORD = "SKIP"

# Everything the webhook accepts as a cancellation. Only SKIP is ever
# printed in a message. NO and NAH are deliberately absent: keywords are
# global to the number rather than scoped to a conversation, so a bare
# "NO" texted for any reason at all would cancel someone's ride.
SKIP_KEYWORDS = {SKIP_KEYWORD, "NORIDE", "OUT", "CANT"}


def build_skip_reply(phone: str, sunday_date: str | None = None) -> str | None:
    """Cancel this phone's ride for the coming Sunday and build the reply.

    Order matters here. The already-cancelled check comes first, before
    the rider lookup, because get_riders_for_sunday() filters cancelled
    riders out: checking the rider list first would make a second SKIP
    find nobody and return silence, leaving someone who just cancelled
    convinced it hadn't worked.

    The Firestore write is what counts. The sheet flag is attempted
    after it and its failure is logged rather than raised, since every
    rider count in the system reads the Firestore record and none of
    them read the flag.

    Args:
        phone: The texter's number, E.164 preferred.
        sunday_date: Optional ISO "YYYY-MM-DD" override. Defaults to the
            coming Sunday.

    Returns:
        str or None: The SMS body to reply with, or None when this
            number has no signup for that Sunday, in which case the
            webhook stays silent the way it does for every other
            unauthorized keyword.
    """
    if sunday_date is None:
        sunday_date = get_next_sunday_date()

    try:
        normalized = normalize_to_e164(phone)
    except ValueError:
        normalized = phone

    # 1. Already cancelled? Say so again rather than going quiet.
    try:
        if normalized in get_cancelled_phones_for_sunday(sunday_date):
            logger.info("Repeat SKIP from %s for %s.", normalized, sunday_date)
            return _cancel_confirmation(sunday_date)
    except RuntimeError as exc:
        # Fall through to the rider lookup. Worst case this is a repeat
        # cancellation, which record_ride_cancellation() handles.
        logger.warning("Could not check existing cancellations: %s", exc)

    # 2. Are they actually signed up? Unfiltered, so a rider mid-cancel
    #    is still findable.
    try:
        riders = get_riders_for_sunday(sunday_date, include_cancelled=True)
    except RuntimeError as exc:
        logger.error("Could not read riders while handling SKIP: %s", exc)
        return (
            f"{BRAND_PREFIX} Couldn't cancel that just now. Please text an "
            f"usher or try again in a minute. {OPT_OUT_NOTICE}"
        )

    rider = _find_rider(riders, normalized)
    if rider is None:
        logger.info(
            "Ignoring %s from %s: no signup for %s.",
            SKIP_KEYWORD,
            normalized,
            sunday_date,
        )
        return None

    # 3. Record it. This is the part that must succeed.
    try:
        result = record_ride_cancellation(
            normalized, sunday_date, name=rider.get("name", "")
        )
    except RuntimeError as exc:
        logger.error("Could not record cancellation for %s: %s", normalized, exc)
        return (
            f"{BRAND_PREFIX} Couldn't cancel that just now. Please text an "
            f"usher or try again in a minute. {OPT_OUT_NOTICE}"
        )

    logger.info(
        "Cancelled %s (%s) for %s (is_new=%s).",
        rider.get("name", ""),
        normalized,
        sunday_date,
        result["is_new"],
    )

    # 4. Mirror it into the sheet. Best effort, never fatal.
    _flag_sheet_row(normalized, sunday_date)

    return _cancel_confirmation(sunday_date)


def build_rider_reminder(
    name: str, stop: str, pickup_time: str | None, sunday_date: str
) -> str:
    """Build one rider's Saturday night reminder.

    Args:
        name: The rider's full name from the sheet; only the first name
            is used, matching every other rider-facing message.
        stop: Their campus stop.
        pickup_time: Time from the Routes tab, or None if the stop has
            no time listed, in which case the time is left out rather
            than guessed at.
        sunday_date: The service date, ISO "YYYY-MM-DD".

    Returns:
        str: The SMS body, one segment.
    """
    when = _format_short_date(sunday_date)
    first = _first_name(name)

    if pickup_time:
        where = f"Pickup at {stop}, {pickup_time}."
    else:
        where = f"Pickup at {stop}."

    return (
        f"{BRAND_PREFIX} Hi {first}, your ride is this Sunday {when}. "
        f"{where} Reply {SKIP_KEYWORD} to cancel. {OPT_OUT_NOTICE}"
    )


def send_saturday_rider_reminders(
    sunday_date: str | None = None, dry_run: bool = False
) -> dict:
    """Text every consenting shuttle rider their pickup details.

    Skips riders with no SMS consent (the consent gate fails closed, so
    an absent consent column means nobody is texted), riders with no
    usable phone number, and riders who have opted out.

    Args:
        sunday_date: Optional ISO "YYYY-MM-DD" override. Defaults to the
            coming Sunday, which on a Saturday 9:30pm Central run is
            already "today" in UTC and so resolves correctly.
        dry_run: If True, builds every message and works out who would
            be skipped, but sends nothing. This exists because the only
            other way to find out what this job does is to text every
            rider on the list, which is not something anyone should do
            to check their own wording.

    Returns:
        dict: {"status", "sunday_date", "sent", "skipped", "failed",
            "details"} for the run log.
    """
    if sunday_date is None:
        sunday_date = get_next_sunday_date()

    riders = get_riders_for_sunday(sunday_date)
    stop_times = get_stop_times_map()

    sent, skipped, failed, details = 0, 0, 0, []

    for rider in riders:
        name = rider.get("name", "")

        if not rider.get("sms_consent"):
            skipped += 1
            details.append(f"{name}: no SMS consent")
            continue

        try:
            phone = normalize_to_e164(rider.get("phone", ""))
        except ValueError:
            skipped += 1
            details.append(f"{name}: unusable phone number")
            continue

        try:
            if is_phone_opted_out(phone):
                skipped += 1
                details.append(f"{name}: opted out")
                continue
        except RuntimeError as exc:
            # Twilio blocks delivery to opted-out numbers at the carrier
            # level anyway, so attempting the send is safe.
            logger.warning("Opt-out check failed for %s: %s", phone, exc)

        body = build_rider_reminder(
            name, rider.get("stop", ""), stop_times.get(rider.get("stop", "")),
            sunday_date,
        )

        if dry_run:
            sent += 1
            details.append(f"{name} ({phone}): WOULD SEND\n    {body}")
            continue

        if send_sms(phone, body):
            sent += 1
            details.append(f"{name}: sent")
        else:
            failed += 1
            details.append(f"{name}: send failed")

    logger.info(
        "Saturday rider reminders for %s%s: %s sent, %s skipped, %s failed.",
        " (DRY RUN)" if dry_run else "",
        sunday_date,
        sent,
        skipped,
        failed,
    )
    return {
        "status": "dry_run" if dry_run else ("success" if failed == 0 else "partial"),
        "sunday_date": sunday_date,
        "sent": sent,
        "skipped": skipped,
        "failed": failed,
        "details": details,
    }


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------
def _cancel_confirmation(sunday_date: str) -> str:
    """The reply a rider gets for a successful or repeated cancellation.

    Names the date, so a rider who cancels the wrong week notices, and
    says nothing else. No signup link: a bare URL in A2P traffic is a
    common trigger for carrier spam filtering, and messages being
    silently dropped would cost more than the link saves. A rider who
    changes their mind signs up again the same way they did the first
    time.
    """
    return (
        f"{BRAND_PREFIX} Ride cancelled for {_format_short_date(sunday_date)}. "
        f"{OPT_OUT_NOTICE}"
    )


def _find_rider(riders: list[dict], normalized_phone: str) -> dict | None:
    """Return the rider whose phone matches, comparing in E.164 both ways."""
    for rider in riders:
        try:
            if normalize_to_e164(rider.get("phone", "")) == normalized_phone:
                return rider
        except ValueError:
            continue
    return None


def _flag_sheet_row(normalized_phone: str, sunday_date: str) -> None:
    """Append "/cancelled" to this rider's signup row. Never raises.

    Deliberately swallows every failure. The sheet belongs to someone
    outside this system and a write can fail for reasons we don't
    control, while the cancellation is already safe in Firestore and
    that's what every count reads.
    """
    try:
        row = find_signup_row_for_phone(normalized_phone, sunday_date)
        if row is None:
            logger.warning(
                "No sheet row found for %s on %s; Firestore record stands alone.",
                normalized_phone,
                sunday_date,
            )
            return
        flag_signup_cancelled(row)
    except Exception as exc:
        logger.error(
            "Could not flag sheet row for %s on %s (%s). "
            "The cancellation is recorded in Firestore and counts are correct; "
            "the sheet row just won't show it.",
            normalized_phone,
            sunday_date,
            exc,
        )


def _format_short_date(iso_date: str) -> str:
    """Convert "2026-09-20" to "9/20/26", matching every other rider message."""
    parsed = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{parsed.month}/{parsed.day}/{parsed.strftime('%y')}"


def _first_name(name: str) -> str:
    return name.strip().split()[0] if name.strip() else "there"
