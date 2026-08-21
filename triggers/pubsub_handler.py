# Handles incoming Pub/Sub events and routes them to the appropriate agent.
#
# This module is the entry point Google Cloud calls into - either via a
# Pub/Sub push subscription (handle_pubsub) whenever Firestore data
# changes, or via Cloud Scheduler's Saturday cron job hitting an HTTP
# endpoint (handle_http). Neither entry point should ever raise: Cloud
# Run/Functions treats an unhandled exception as an infra-level failure
# (and may retry-storm the message), so every error is caught and turned
# into a normal {"status": "error", ...} response instead.

from __future__ import annotations

import base64
import json
import logging
from datetime import date, timedelta
from typing import Any

from agents.assignment_agent import run as assignment_run
from agents.monitor_agent import run as monitor_run
from config import settings

logger = logging.getLogger(__name__)

# Event types that should be routed to the monitor agent - anything that
# represents a small, everyday change to riders/drivers.
_MONITOR_EVENT_TYPES = {
    "new_rider_signup",
    "rider_cancellation",
    "driver_availability_change",
}

# Event type that kicks off the full Saturday driver-to-route assignment.
_ASSIGNMENT_EVENT_TYPE = "saturday_assignment_run"


def handle_pubsub(event: dict, context: Any) -> dict:
    """Cloud entry point for Pub/Sub-triggered events.

    Google Cloud invokes this whenever a message is published to the
    subscribed topic (e.g. a Firestore change publishes an event). The
    message payload is base64-encoded JSON containing an "event_type"
    and "event_data".

    Args:
        event: The Pub/Sub event payload. event["data"] is the
            base64-encoded message body.
        context: Cloud Functions/Run event metadata (event ID, timestamp,
            resource, etc.). Not used here, but required by the
            platform's function signature.

    Returns:
        dict: {"status": "ok"|"ignored"|"error", "event_type": str} on
            success/ignored, or {"status": "error", "error": str} if
            anything went wrong. This function never raises.
    """
    try:
        # Required settings must be present before we do anything that
        # depends on them (Firestore project, API keys, etc.).
        settings.validate()

        # STEP 1 - Decode the Pub/Sub message: base64 -> JSON -> dict.
        raw_data = event.get("data", "")
        decoded_bytes = base64.b64decode(raw_data)
        message = json.loads(decoded_bytes)

        # STEP 2 - Extract the event type/data and the Sunday it relates
        # to (event_data carries this rather than the top-level message,
        # since every event type includes it).
        event_type = message.get("event_type")
        event_data = message.get("event_data", {})
        sunday_date = event_data.get("sunday_date")

        # STEP 3 - Route to the right agent based on event_type.
        if event_type in _MONITOR_EVENT_TYPES:
            monitor_run(event_type=event_type, event_data=event_data, sunday_date=sunday_date)
        elif event_type == _ASSIGNMENT_EVENT_TYPE:
            assignment_run(sunday_date=sunday_date)
        else:
            # Unknown event types are not an error - just log and move on
            # so unrelated messages on the same topic don't break anything.
            logger.warning("Received unknown event_type=%r; ignoring.", event_type)
            return {"status": "ignored", "event_type": event_type}

        return {"status": "ok", "event_type": event_type}

    except Exception as exc:
        # Catches base64/JSON decode errors, missing keys, and any error
        # raised by the agent itself. Always return a response - never
        # let this bubble up and crash the function invocation.
        logger.error("Failed to handle Pub/Sub message: %s", exc)
        return {"status": "error", "error": str(exc)}


def handle_http(request: Any) -> dict:
    """Cloud entry point for the Saturday HTTP-triggered assignment run.

    Cloud Scheduler calls this every Saturday at settings.SATURDAY_RUN_TIME
    to kick off the assignment agent for the upcoming Sunday.

    Args:
        request: The incoming HTTP request (a Flask-style Request object,
            as provided by Cloud Run/Functions). Its JSON body may
            optionally include "sunday_date".

    Returns:
        dict: {"status": "ok", "result": dict} with the assignment
            agent's result on success, or {"status": "error", "error":
            str} if anything went wrong. This function never raises.
    """
    try:
        settings.validate()

        # STEP 1 - Parse the request body. An empty/missing body is fine
        # - we just fall back to computing next Sunday ourselves.
        body: dict = {}
        if request is not None:
            body = request.get_json(silent=True) or {}

        # STEP 2 - Use the provided sunday_date, or default to the
        # upcoming Sunday (or today, if today already is Sunday).
        sunday_date = body.get("sunday_date") or get_next_sunday()

        # STEP 3 - Run the assignment agent for that Sunday.
        result = assignment_run(sunday_date)

        return {"status": "ok", "result": result}

    except Exception as exc:
        logger.error("Failed to handle scheduled assignment run: %s", exc)
        return {"status": "error", "error": str(exc)}


def get_next_sunday() -> str:
    """Return the date of the next upcoming Sunday.

    If today is already Sunday, returns today's date rather than the
    Sunday a week from now.

    Returns:
        str: The date in "YYYY-MM-DD" format.
    """
    today = date.today()

    # date.weekday(): Monday=0 ... Sunday=6. This computes how many days
    # to add to reach the next Sunday, treating "0 days away" (today is
    # Sunday) as 0 rather than wrapping to 7.
    days_until_sunday = (6 - today.weekday()) % 7
    next_sunday = today + timedelta(days=days_until_sunday)

    return next_sunday.strftime("%Y-%m-%d")
