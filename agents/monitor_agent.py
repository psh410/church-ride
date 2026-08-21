# Agent that monitors for new ride requests and driver availability changes.
#
# This agent is event-driven, not scheduled: it wakes up whenever
# Firestore data changes (a rider signs up, a driver cancels, etc.),
# checks the current state of the system, and uses Claude to decide
# whether the change is significant enough to alert the human overseers.
# It is deliberately conservative - most events should NOT trigger an
# alert.

from __future__ import annotations

import json
from typing import Optional

import anthropic

from agents.notification_agent import notify_overseers
from config import settings
from db.firestore_client import get_available_drivers, get_riders, get_routes
from safety.run_log import complete_run, fail_run, start_run

AGENT_NAME = "monitor_agent"
CLAUDE_MODEL = "claude-sonnet-4-6"

# Small token budget - Claude only needs to return a short decision, not
# an essay.
CLAUDE_MAX_TOKENS = 400

SYSTEM_PROMPT = (
    "You are a monitor agent for a church ride coordination system. "
    "Your job is to decide whether a recent change is significant "
    "enough to alert the coordinators. Be conservative - only alert for "
    "genuinely important changes. Respond ONLY in valid JSON with no "
    "markdown."
)


def run(
    event_type: str,
    event_data: dict,
    sunday_date: str,
    use_test_data: bool = False,
    fake_state: Optional[dict] = None,
) -> dict:
    """Evaluate a Firestore change event and alert overseers if warranted.

    Args:
        event_type: What kind of event triggered this run (e.g.
            "new_rider_signup", "rider_cancellation",
            "driver_cancellation").
        event_data: Details about the specific event (e.g. which rider,
            how many total signups today). Passed straight through to
            Claude as context.
        sunday_date: The Sunday date this event relates to, e.g.
            "2026-08-23".
        use_test_data: If True, use `fake_state` instead of querying
            Firestore. Used for local testing (see main() below).
        fake_state: Only used when use_test_data=True. A dict with
            "riders", "drivers", and "routes" lists standing in for what
            would otherwise be loaded from Firestore.

    Returns:
        dict: {"run_id": str, "should_alert": bool, "reason": str,
            "urgency": str, "notified": bool}.

    Raises:
        ValueError: If Claude's response can't be parsed as the expected
            JSON shape.
        Exception: Any other failure (Claude API error, Firestore
            failure, etc.) is logged via fail_run() and re-raised.
    """
    # STEP 1 - Start the run. Every log entry below is tied together by
    # this run_id, and it's also passed to notify_overseers() so the
    # resulting alert (if any) is traceable back to this event.
    run_id = start_run(AGENT_NAME, "event")

    try:
        # STEP 2 - Load the current state of the system and summarize it.
        if use_test_data:
            state = fake_state or {}
            riders = state.get("riders", [])
            drivers = state.get("drivers", [])
            routes = state.get("routes", [])
        else:
            riders = get_riders(sunday_date)
            drivers = get_available_drivers(sunday_date)
            routes = get_routes()

        state_summary = _build_state_summary(riders, drivers, routes)

        # STEP 3 - Ask Claude whether this event is significant enough to
        # alert the overseers.
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        prompt = _build_prompt(state_summary, event_type, event_data, sunday_date)

        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = response.content[0].text

        try:
            decision = _parse_claude_decision(raw_text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                f"Failed to parse Claude's monitor decision: {exc}"
            ) from exc

        # STEP 4 - Act on the decision. Alert overseers if warranted;
        # otherwise stay silent. Either way, the reason is captured below
        # in complete_run()'s details so "no alert needed" cases are
        # still logged for the audit trail - we just don't send anything.
        notified = False
        if decision["should_alert"]:
            notify_overseers(decision["message"], run_id)
            notified = True

        # STEP 5 - Log the outcome and return a summary.
        complete_run(
            run_id,
            AGENT_NAME,
            details={
                "sunday_date": sunday_date,
                "event_type": event_type,
                "should_alert": decision["should_alert"],
                "reason": decision["reason"],
                "urgency": decision["urgency"],
                "notified": notified,
                "state_summary": state_summary,
            },
        )

        return {
            "run_id": run_id,
            "should_alert": decision["should_alert"],
            "reason": decision["reason"],
            "urgency": decision["urgency"],
            "notified": notified,
        }

    except Exception as exc:
        # Covers Claude API errors, Firestore read failures, and parse
        # failures. fail_run() never raises, so this always finishes
        # logging before the original error propagates.
        fail_run(
            run_id,
            AGENT_NAME,
            exc,
            details={"event_type": event_type, "sunday_date": sunday_date},
        )
        raise


def _build_state_summary(riders: list[dict], drivers: list[dict], routes: list[dict]) -> dict:
    """Summarize the current riders/drivers/routes into counts Claude can reason about.

    Args:
        riders: All riders for the Sunday in question.
        drivers: Available drivers for the Sunday in question.
        routes: All active routes.

    Returns:
        dict: Counts and flags describing the current state, plus the
            configured alert thresholds for context.
    """
    confirmed_riders = sum(1 for rider in riders if rider.get("status") == "confirmed")
    pending_riders = sum(1 for rider in riders if rider.get("status") == "pending")
    cancelled_riders = sum(1 for rider in riders if rider.get("status") == "cancelled")

    routes_without_drivers = [
        route.get("name") for route in routes if not route.get("driver_id")
    ]

    return {
        "total_riders": len(riders),
        "confirmed_riders": confirmed_riders,
        "pending_riders": pending_riders,
        "cancelled_riders": cancelled_riders,
        "available_drivers": len(drivers),
        "total_routes": len(routes),
        "routes_without_drivers": routes_without_drivers,
        "alert_thresholds": settings.ALERT_THRESHOLDS,
    }


def _build_prompt(
    state_summary: dict, event_type: str, event_data: dict, sunday_date: str
) -> str:
    """Build the prompt describing the event and current state for Claude.

    Args:
        state_summary: The dict from _build_state_summary().
        event_type: What kind of event triggered this run.
        event_data: Details about the specific event.
        sunday_date: The Sunday date this event relates to.

    Returns:
        str: The full prompt text to send to Claude as the user message.
    """
    state_json = json.dumps(state_summary, indent=2)
    event_data_json = json.dumps(event_data, indent=2, default=str)

    return f"""An event just occurred in the church ride coordination system for Sunday, {sunday_date}.

EVENT TYPE: {event_type}

EVENT DATA:
{event_data_json}

CURRENT STATE SUMMARY:
{state_json}

Decide whether this event is significant enough to alert the ride coordination overseers, using these rules:
- A new signup pushes total_riders over one of the alert_thresholds (25, 50, 75, or 100) -> alert.
- Any route listed in "routes_without_drivers" still has no confirmed driver by Thursday -> alert.
- A rider cancels within 48 hours of Sunday -> alert.
- A driver cancels within 48 hours of Sunday -> alert.
- Otherwise -> stay silent (should_alert: false).

Be conservative: only alert for genuinely important changes, not routine signups or cancellations that stay well within normal thresholds.

Respond with ONLY valid JSON in exactly this shape (no markdown fences, no extra commentary):
{{
  "should_alert": true or false,
  "reason": "string",
  "urgency": "low" | "medium" | "high",
  "message": "string to send to overseers if alerting"
}}"""


def _parse_claude_decision(raw_text: str) -> dict:
    """Parse and validate Claude's JSON alert decision.

    Args:
        raw_text: The raw text content of Claude's response.

    Returns:
        dict: The parsed decision with "should_alert", "reason",
            "urgency", and "message" keys.

    Raises:
        json.JSONDecodeError: If the text isn't valid JSON.
        ValueError: If the parsed JSON is missing required keys.
    """
    cleaned = raw_text.strip()

    # Defensive: strip markdown code fences in case the model wraps its
    # JSON in a ```json ... ``` block despite being told not to.
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    parsed = json.loads(cleaned)

    required_keys = {"should_alert", "reason", "urgency", "message"}
    missing = required_keys - parsed.keys()
    if missing:
        raise ValueError(
            f"Claude's response is missing required key(s): {sorted(missing)}"
        )

    return parsed


def main() -> None:
    """Run the monitor agent locally against a fake state.

    Simulates a "new_rider_signup" event where the 26th rider just
    signed up - one over the 25-rider alert threshold - without touching
    real Firestore data (use_test_data=True).
    """
    fake_riders = [
        {"id": f"rider_{i:03d}", "name": f"Test Rider {i}", "status": "confirmed"}
        for i in range(1, 27)  # 26 riders - just over the 25 threshold.
    ]
    fake_drivers = [{"id": "driver_001", "name": "Marcus Johnson"}]
    fake_routes = [
        {"id": "route_north", "name": "North Route", "driver_id": "driver_001"},
        {"id": "route_east", "name": "East Route", "driver_id": None},
        {"id": "route_south", "name": "South Route", "driver_id": None},
    ]

    fake_state = {
        "riders": fake_riders,
        "drivers": fake_drivers,
        "routes": fake_routes,
    }
    event_data = {"new_rider_name": "Test Rider 26", "signup_count_today": 1}

    result = run(
        event_type="new_rider_signup",
        event_data=event_data,
        sunday_date="2026-08-23",
        use_test_data=True,
        fake_state=fake_state,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
