# Sends SMS notifications to riders and drivers about ride status.
#
# Uses the Twilio REST API through the "CFC Communications" Messaging
# Service (settings.TWILIO_MESSAGING_SERVICE_SID) rather than a bare
# From number, so outbound texts go out under the approved A2P 10DLC
# campaign. Same True/False + log-on-failure pattern as
# functions/send_email.py.

from __future__ import annotations

import logging
import re

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from config import settings
from db.firestore_client import is_phone_opted_out

logger = logging.getLogger(__name__)

# Contact info included in a couple of the approved sample messages
# below - kept as a constant so it's defined in exactly one place.
_SUPPORT_EMAIL = "team@cfchome.org"

# Every outbound message starts with BRAND_PREFIX and ends with
# OPT_OUT_NOTICE, so recipients always know who is texting them and how
# to stop. These live here, in the one module every sender imports, so
# the wording can't drift between rider confirmations, driver reminders
# and the admin summary (it already had, three different ways, before
# these existed).
#
# "CFC Rides" deliberately matches the sample messages registered with
# the approved A2P 10DLC campaign - copy that drifts from the registered
# samples is a carrier-filtering risk.
#
# On the opt-out notice: Twilio's messaging policy requires opt-out
# language in the INITIAL message to a recipient and treats periodic
# reminders after that as best practice rather than a hard rule. We
# include it on every message anyway, since it's simpler to guarantee
# than tracking who has already been told.
BRAND_PREFIX = "CFC Rides:"
OPT_OUT_NOTICE = "Reply HELP for help, STOP to opt out."


def normalize_to_e164(phone: str) -> str:
    """Normalize a messy US phone number to E.164 (+1XXXXXXXXXX).

    Accepts common form-input shapes such as "217-402-3446",
    "(217) 402-3446", "2174023446", and "+12174023446". Non-digit
    characters are stripped (a leading + is kept). A bare 10-digit
    number gets "+1"; an 11-digit number starting with 1 gets "+".

    Args:
        phone: The raw phone number string.

    Returns:
        str: The number in E.164 form, e.g. "+12174023446".

    Raises:
        ValueError: If phone is empty or does not look like a valid
            US number (not 10 digits, and not 11 digits starting
            with 1).
    """
    if phone is None or not str(phone).strip():
        raise ValueError(f"Phone number is empty: {phone!r}")

    stripped = str(phone).strip()
    has_plus = stripped.startswith("+")
    digits = re.sub(r"\D", "", stripped)
    if not digits:
        raise ValueError(f"Phone number has no digits: {phone!r}")

    if has_plus:
        # Already marked international - keep the +, then validate US.
        if len(digits) == 10:
            return f"+1{digits}"
        if len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"
        raise ValueError(
            f"Phone number {phone!r} is not a valid US number "
            f"(got {len(digits)} digits after cleaning)."
        )

    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"

    raise ValueError(
        f"Phone number {phone!r} is not a valid US number "
        f"(got {len(digits)} digits after cleaning)."
    )


def send_sms(to: str, body: str) -> bool:
    """Send a single SMS via the CFC Communications Messaging Service.

    Creates a Twilio REST client from settings.TWILIO_ACCOUNT_SID and
    settings.TWILIO_AUTH_TOKEN, normalizes `to` to E.164, and sends
    through settings.TWILIO_MESSAGING_SERVICE_SID (not a raw From
    number) so the message is tied to the approved A2P campaign.

    Args:
        to: Recipient phone number in any format normalize_to_e164()
            accepts (e.g. "217-402-3446" or "+12174023446").
        body: The message text to send.

    Returns:
        bool: True if Twilio accepted the message for sending, False
            otherwise. "Accepted" is not the same as "delivered" -
            carrier filtering can still fail after this returns True.
    """
    try:
        normalized = normalize_to_e164(to)
    except ValueError as exc:
        logger.error("Cannot send SMS: %s", exc)
        return False

    # Twilio already blocks delivery to a number that has opted out at
    # the carrier level, but checking our own record first means we
    # don't waste an API call/log a confusing Twilio-side failure, and
    # callers (e.g. send_driver_sms_reminder.py) see a clear "opted
    # out" reason instead of a generic send failure. If the check
    # itself fails (e.g. Firestore hiccup), fail open and attempt the
    # send anyway - Twilio's own opt-out enforcement is the real
    # safety net here, not this lookup.
    try:
        if is_phone_opted_out(normalized):
            logger.warning(
                "Skipping SMS to %s: this number has opted out.", normalized
            )
            return False
    except RuntimeError as exc:
        logger.error(
            "Could not check opt-out status for %s (%s); sending anyway.",
            normalized,
            exc,
        )

    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        logger.error(
            "Cannot send SMS to %s: TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN "
            "are not configured.",
            normalized,
        )
        return False

    if not settings.TWILIO_MESSAGING_SERVICE_SID:
        logger.error(
            "Cannot send SMS to %s: TWILIO_MESSAGING_SERVICE_SID is not configured.",
            normalized,
        )
        return False

    try:
        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        message = client.messages.create(
            to=normalized,
            messaging_service_sid=settings.TWILIO_MESSAGING_SERVICE_SID,
            body=body,
        )
        logger.info("Sent SMS to %s (sid=%s).", normalized, message.sid)
        return True
    except TwilioRestException as exc:
        logger.error(
            "Twilio API error sending SMS to %s: code=%s message=%s",
            normalized,
            exc.code,
            exc.msg,
        )
        return False
    except Exception as exc:
        logger.error("Unexpected error sending SMS to %s: %s", normalized, exc)
        return False


def send_sms_batch(recipients: list[dict]) -> dict:
    """Send many SMS messages, continuing even if individual sends fail.

    Args:
        recipients: A list of {"to": phone, "body": message} dicts.

    Returns:
        dict: {"sent": int, "failed": int, "failures": [phone, ...]}
            where failures lists the original `to` values that did
            not send.
    """
    sent = 0
    failed = 0
    failures: list[str] = []

    for entry in recipients:
        to = entry.get("to", "")
        body = entry.get("body", "")
        if send_sms(to, body):
            sent += 1
        else:
            failed += 1
            failures.append(to)

    logger.info(
        "SMS batch finished: sent=%s failed=%s.",
        sent,
        failed,
    )
    return {"sent": sent, "failed": failed, "failures": failures}


# --------------------------------------------------------------------------
# Message-type helpers - bodies reproduce the sample messages registered
# with the A2P campaign, word for word. Nothing calls these right now;
# the live senders (send_rider_confirmation.py,
# send_driver_sms_reminder.py, send_admin_summary.py) build their own
# bodies from BRAND_PREFIX + OPT_OUT_NOTICE above. Leave the wording
# here alone: it's a record of what was registered, not general-purpose
# copy to edit.
# --------------------------------------------------------------------------
def send_ride_confirmation(
    phone: str,
    name: str,
    pickup_location: str,
    date_str: str,
    time_str: str,
) -> bool:
    """Send a rider their pickup confirmation.

    Matches approved sample: "CFC Rides: Hi [Name], you're confirmed
    for pickup at [Pickup Location] on [Date] at [Time]. Reply STOP
    to opt out."
    """
    body = (
        f"CFC Rides: Hi {name}, you're confirmed for pickup at "
        f"{pickup_location} on {date_str} at {time_str}. Reply STOP to opt out."
    )
    return send_sms(phone, body)


def send_ride_cancellation(phone: str, date_str: str, time_str: str) -> bool:
    """Notify a rider that their ride has been cancelled.

    Matches approved sample: "CFC Rides: Your ride on [Date] at
    [Time] has been cancelled. Questions? Email team@cfchome.org.
    Reply STOP to opt out."
    """
    body = (
        f"CFC Rides: Your ride on {date_str} at {time_str} has been "
        f"cancelled. Questions? Email {_SUPPORT_EMAIL}. Reply STOP to opt out."
    )
    return send_sms(phone, body)


def send_dropoff_confirmation(
    phone: str,
    driver_name: str,
    rider_name: str,
    location: str,
    time_str: str,
) -> bool:
    """Notify that a drop-off completed.

    Matches approved sample: "CFC Rides: [Driver Name] confirms
    drop-off complete for [Rider Name] at [Location], [Time]."
    """
    body = (
        f"CFC Rides: {driver_name} confirms drop-off complete for "
        f"{rider_name} at {location}, {time_str}."
    )
    return send_sms(phone, body)
