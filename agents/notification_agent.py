# Agent that notifies riders and drivers of new or updated ride assignments.
#
# This agent sends notifications to drivers, riders, and overseers via
# email and/or Discord DM, using Claude to draft a warm, personalized
# message for each recipient. It must never send the same notification
# twice, which is enforced by an idempotency check keyed on run_id.

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

import anthropic

from config import settings
from db.firestore_client import get_riders_by_route, get_route, write_run_log
from functions.send_discord import send_discord_dms
from functions.send_email import send_email
from safety.run_log import complete_run, fail_run, has_already_notified, start_run

logger = logging.getLogger(__name__)

# Agent name used for run logging.
AGENT_NAME = "notification_agent"

# Model and token budget for message drafting. Kept small (500 tokens)
# since each message is short and we draft one per recipient.
CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS = 500

SYSTEM_PROMPT = (
    "You are drafting notifications for a church ride coordination "
    "system. Write warm, clear, concise messages in a friendly church "
    "community tone. Always address the person by name. Include only "
    "details relevant to them. Sign off as 'The Ride Coordination Team'"
)

# Notification types this agent knows how to build recipients for.
NOTIF_ASSIGNMENT = "assignment"
NOTIF_CONFIRMATION = "confirmation"
NOTIF_ALERT = "alert"
NOTIF_FAILURE = "failure"

# Email subject lines by recipient role.
_SUBJECTS_BY_ROLE = {
    "driver": "Your Sunday Ride Route Assignment",
    "rider": "Your Sunday Ride Pickup Details",
    "overseer": "Church Rides Alert",
    "admin": "Church Rides System Failure",
}


def run(notification_type: str, data: dict, run_id: Optional[str] = None) -> dict:
    """Draft and send notifications for a given event.

    Args:
        notification_type: One of NOTIF_ASSIGNMENT, NOTIF_CONFIRMATION,
            NOTIF_ALERT, or NOTIF_FAILURE - determines who the
            recipients are and what data they need.
        data: Notification-type-specific payload. See the
            _build_*_recipients() helpers below for the expected shape.
        run_id: If provided, reuse this run_id (e.g. when called by
            another agent that wants notifications tied to its own run).
            If None, a new run is started.

    Returns:
        dict: {"run_id": str, "sent_count": int, "failures": list[dict],
            "skipped": bool}. "skipped" is True if this run's
            notifications were already sent previously.

    Raises:
        ValueError: If notification_type is not recognized.
        Exception: Any other failure (Claude API error, Firestore
            failure, etc.) is logged via fail_run() and re-raised.
    """
    # STEP 1 - Start a new run, or continue one started by another agent
    # (e.g. the assignment agent passing its own run_id through so both
    # agents' log entries line up under the same run).
    if run_id is None:
        run_id = start_run(AGENT_NAME, "event")

    try:
        # STEP 2 - Idempotency check. If this exact run already sent its
        # notifications successfully, do nothing rather than risk
        # duplicate messages (e.g. from a redelivered trigger event).
        if has_already_notified(run_id, AGENT_NAME):
            write_run_log(
                run_id=run_id,
                agent=AGENT_NAME,
                status="skipped",
                message="Notifications for this run were already sent; skipping.",
                details={"notification_type": notification_type},
            )
            return {"run_id": run_id, "sent_count": 0, "failures": [], "skipped": True}

        # STEP 3 - Build the recipient list for this notification type.
        recipients = _build_recipients(notification_type, data)

        # STEP 4 - Draft a personalized message per recipient with Claude.
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        messages = [_draft_message(client, recipient) for recipient in recipients]

        # STEP 5 - Send everything, batching all Discord DMs into one
        # client session.
        send_result = _send_notifications(run_id, recipients, messages)

        # STEP 6 - Log success and return a summary.
        complete_run(
            run_id,
            AGENT_NAME,
            details={
                "notification_type": notification_type,
                "sent_count": send_result["sent_count"],
                "failure_count": len(send_result["failures"]),
            },
        )
        return {
            "run_id": run_id,
            "sent_count": send_result["sent_count"],
            "failures": send_result["failures"],
            "skipped": False,
        }

    except Exception as exc:
        # Covers unknown notification_type, Claude API errors, Firestore
        # failures, etc. fail_run() never raises, so this always
        # finishes logging before the original error propagates.
        fail_run(run_id, AGENT_NAME, exc, details={"notification_type": notification_type})
        raise


# --------------------------------------------------------------------------
# STEP 3 helpers - build a recipient list per notification_type
# --------------------------------------------------------------------------
def _build_recipients(notification_type: str, data: dict) -> list[dict]:
    """Dispatch to the right recipient-building helper for this type.

    Each recipient dict has "name", "email", "discord_username", and a
    role-specific "context" dict used when drafting their message.

    Args:
        notification_type: One of the NOTIF_* constants.
        data: The notification-type-specific payload passed to run().

    Returns:
        list[dict]: The recipients to notify.

    Raises:
        ValueError: If notification_type isn't recognized.
    """
    if notification_type == NOTIF_ASSIGNMENT:
        return _build_assignment_recipients(data)
    if notification_type == NOTIF_CONFIRMATION:
        return _build_confirmation_recipients(data)
    if notification_type == NOTIF_ALERT:
        return _build_alert_recipients(data)
    if notification_type == NOTIF_FAILURE:
        return _build_failure_recipients(data)

    raise ValueError(f"Unknown notification_type: {notification_type!r}")


def _build_assignment_recipients(data: dict) -> list[dict]:
    """Build one recipient per driver, with their route + rider list.

    Args:
        data: Dict with "sunday_date" and "assignments" - each
            assignment dict must include driver_name, driver_email,
            driver_discord_username, route_id, route_name, and
            rider_count.

    Returns:
        list[dict]: One recipient per assignment.
    """
    sunday_date = data.get("sunday_date")
    assignments = data.get("assignments", [])

    recipients = []
    for assignment in assignments:
        route_id = assignment.get("route_id")
        # Pull the live route + rider list from Firestore rather than
        # trusting stale data, in case riders changed since the
        # assignment was made.
        route = get_route(route_id) if route_id else {}
        riders = (
            get_riders_by_route(sunday_date, route_id)
            if sunday_date and route_id
            else []
        )

        recipients.append(
            {
                "name": assignment.get("driver_name"),
                "email": assignment.get("driver_email"),
                "discord_username": assignment.get("driver_discord_username"),
                "context": {
                    "role": "driver",
                    "route_name": assignment.get("route_name") or route.get("name"),
                    "stops": route.get("stops", []),
                    "riders": [
                        {
                            "name": rider.get("name"),
                            "stop": rider.get("stop"),
                            "return_ride": rider.get("return_ride"),
                        }
                        for rider in riders
                    ],
                    "rider_count": assignment.get("rider_count", len(riders)),
                },
            }
        )

    return recipients


def _build_confirmation_recipients(data: dict) -> list[dict]:
    """Build one recipient per rider needing their pickup details.

    Args:
        data: Dict with "riders" - a list of rider dicts (name, email,
            discord_username, route_id, stop, return_ride).

    Returns:
        list[dict]: One recipient per rider.
    """
    riders = data.get("riders", [])

    recipients = []
    for rider in riders:
        route_id = rider.get("route_id")
        route = get_route(route_id) if route_id else {}
        pickup_time = next(
            (
                stop.get("time")
                for stop in route.get("stops", [])
                if stop.get("name") == rider.get("stop")
            ),
            None,
        )

        recipients.append(
            {
                "name": rider.get("name"),
                "email": rider.get("email"),
                "discord_username": rider.get("discord_username"),
                "context": {
                    "role": "rider",
                    "route_name": route.get("name"),
                    "stop": rider.get("stop"),
                    "pickup_time": pickup_time,
                    "return_ride": rider.get("return_ride", False),
                },
            }
        )

    return recipients


def _build_alert_recipients(data: dict) -> list[dict]:
    """Build one recipient per overseer for an internal alert.

    Args:
        data: Dict with "message" - the raw alert text/details.

    Returns:
        list[dict]: One recipient per address in settings.OVERSEER_EMAILS.
    """
    message = data.get("message", "")
    return [
        {
            "name": "Overseer",
            "email": email,
            "discord_username": None,
            "context": {"role": "overseer", "alert_message": message},
        }
        for email in settings.OVERSEER_EMAILS
    ]


def _build_failure_recipients(data: dict) -> list[dict]:
    """Build the single admin recipient for a run-failure notification.

    Args:
        data: Dict describing the failure (e.g. agent, error, traceback).

    Returns:
        list[dict]: A single-item list with the admin as the recipient.
    """
    return [
        {
            "name": "Admin",
            "email": settings.ADMIN_EMAIL,
            "discord_username": None,
            "context": {"role": "admin", "failure_details": data},
        }
    ]


# --------------------------------------------------------------------------
# STEP 4 helper - draft one message with Claude
# --------------------------------------------------------------------------
def _draft_message(client: anthropic.Anthropic, recipient: dict) -> str:
    """Ask Claude to draft one personalized notification message.

    Args:
        client: The shared Anthropic client.
        recipient: A recipient dict with "name" and a role-specific
            "context" dict (see the _build_*_recipients() helpers).

    Returns:
        str: The drafted message text.
    """
    context_json = json.dumps(recipient.get("context", {}), indent=2, default=str)
    user_prompt = (
        f"Write a notification message for {recipient.get('name')}.\n\n"
        f"Their details:\n{context_json}\n\n"
        "If they have questions, they can reach the Ride Coordination "
        f"Team at {settings.ADMIN_EMAIL}."
    )

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return response.content[0].text.strip()


# --------------------------------------------------------------------------
# STEP 5 helper - send everything, batching Discord DMs
# --------------------------------------------------------------------------
def _send_notifications(run_id: str, recipients: list[dict], messages: list[str]) -> dict:
    """Deliver drafted messages, sending all Discord DMs in one client session.

    Args:
        run_id: The run_id these sends belong to (used for logging).
        recipients: The recipient dicts from _build_recipients().
        messages: The drafted message text, index-aligned with
            `recipients`.

    Returns:
        dict: {"sent_count": int, "failures": list[dict]}.
    """
    # Email has no shared connection to batch, so send it immediately.
    # Discord DMs get queued and sent together afterward in one client
    # session (opening a gateway connection per message is slow and
    # unnecessary).
    discord_queue: list[dict] = []
    discord_recipient_index_by_username: dict[str, int] = {}
    channels_used: list[list[str]] = [[] for _ in recipients]

    for i, (recipient, message) in enumerate(zip(recipients, messages)):
        email = recipient.get("email")
        if email:
            try:
                subject = _subject_for(recipient)
                if send_email(email, subject, message):
                    channels_used[i].append("email")
            except Exception as exc:
                logger.error("Failed to email %s: %s", recipient.get("name"), exc)

        discord_username = recipient.get("discord_username")
        if discord_username:
            discord_queue.append({"username": discord_username, "message": message})
            discord_recipient_index_by_username[discord_username] = i

    if discord_queue:
        # One Discord client, one login, all DMs sent before logging out.
        discord_results = send_discord_dms(discord_queue)
        for username, was_sent in discord_results.items():
            if was_sent:
                channels_used[discord_recipient_index_by_username[username]].append(
                    "discord"
                )

    sent_count = 0
    failures: list[dict] = []

    for i, recipient in enumerate(recipients):
        used = channels_used[i]

        if used:
            sent_count += 1
            status = "sent"
        else:
            status = "failed"
            failures.append(
                {
                    "recipient": recipient.get("name"),
                    "error": "No channel succeeded (missing contact info or all sends failed).",
                }
            )

        # Log exactly which channel(s) were used for this recipient, so
        # the audit trail shows delivery details, not just a pass/fail.
        # Each entry gets its own notification_uuid (independent of the
        # run_id) so individual sends can be traced/deduplicated.
        write_run_log(
            run_id=run_id,
            agent=AGENT_NAME,
            status=status,
            message=f"Notification to {recipient.get('name')!r}: channels used = {used or 'none'}.",
            details={
                "notification_uuid": str(uuid.uuid4()),
                "recipient_name": recipient.get("name"),
                "channels_used": used,
            },
        )

    return {"sent_count": sent_count, "failures": failures}


def _subject_for(recipient: dict) -> str:
    """Pick an email subject line based on the recipient's role.

    Args:
        recipient: A recipient dict with a role-specific "context" dict.

    Returns:
        str: The email subject line to use.
    """
    role = recipient.get("context", {}).get("role", "")
    return _SUBJECTS_BY_ROLE.get(role, "Church Ride Coordination Update")


def notify_overseers(message: str, run_id: str) -> dict:
    """Send an urgent alert to all overseers, bypassing idempotency checks.

    Alerts are allowed to repeat - if the dead man's switch fires more
    than once, it's far safer to over-notify overseers than to risk
    staying silent because of a stale idempotency record. This is why
    notify_overseers() does NOT call has_already_notified().

    Args:
        message: The raw alert message/details to relay to overseers.
        run_id: The run_id this alert relates to (used only for logging
            here, not for deduplication).

    Returns:
        dict: {"sent_count": int, "failures": list[dict]}.
    """
    try:
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        user_prompt = (
            "Write a short, urgent alert email for church ride "
            "coordination overseers based on this raw message:\n\n"
            f"{message}\n\n"
            "Keep it concise and clear - overseers need to understand "
            "the problem and any action needed within a few seconds of "
            "reading."
        )
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        drafted_message = response.content[0].text.strip()
    except Exception as exc:
        # An unpolished alert is far better than no alert at all, so
        # fall back to the raw message if Claude drafting fails.
        logger.error("Failed to draft overseer alert with Claude: %s", exc)
        drafted_message = message

    sent_count = 0
    failures: list[dict] = []

    for email in settings.OVERSEER_EMAILS:
        try:
            if send_email(email, _SUBJECTS_BY_ROLE["overseer"], drafted_message):
                sent_count += 1
            else:
                failures.append({"recipient": email, "error": "send_email returned False."})
        except Exception as exc:
            failures.append({"recipient": email, "error": str(exc)})

    write_run_log(
        run_id=run_id,
        agent=AGENT_NAME,
        status="alert_sent",
        message=(
            f"Sent overseer alert to {sent_count} of "
            f"{len(settings.OVERSEER_EMAILS)} overseer(s)."
        ),
        details={"failures": failures},
    )

    return {"sent_count": sent_count, "failures": failures}


def main() -> None:
    """Run the notification agent locally with fake assignment data.

    Builds a small set of fake driver assignments shaped like
    tests/test_data.py's FAKE_ROUTES/FAKE_DRIVERS, then sends an
    "assignment" notification for each one so you can see drafted
    messages and delivery results without a real assignment_agent run.
    """
    fake_assignments = [
        {
            "route_id": "route_north",
            "route_name": "North Route",
            "driver_id": "driver_001",
            "driver_name": "Marcus Johnson",
            "driver_email": "marcus.johnson@example.com",
            "driver_discord_username": "marcusj",
            "reasoning": "Marcus prefers the North Route and was available.",
            "rider_count": 7,
        },
        {
            "route_id": "route_east",
            "route_name": "East Route",
            "driver_id": "driver_002",
            "driver_name": "Aaliyah Washington",
            "driver_email": "aaliyah.washington@example.com",
            "driver_discord_username": "aaliyahw",
            "reasoning": "Aaliyah prefers the East Route and was available.",
            "rider_count": 7,
        },
        {
            "route_id": "route_south",
            "route_name": "South Route",
            "driver_id": "driver_003",
            "driver_name": "DeShawn Carter",
            "driver_email": "deshawn.carter@example.com",
            "driver_discord_username": "deshawnc",
            "reasoning": "DeShawn is the only driver who prefers the South Route.",
            "rider_count": 6,
        },
    ]

    data = {"sunday_date": "2026-08-23", "assignments": fake_assignments}
    # run_id=None so run() calls start_run() itself, exactly like it
    # would if invoked directly (rather than being handed a run_id by
    # another agent).
    result = run(NOTIF_ASSIGNMENT, data, run_id=None)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
