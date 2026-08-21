# Agent that matches riders with drivers and creates ride assignments.
#
# This is the most important agent in the system: it uses the Claude API
# to reason about which volunteer driver should take which route, taking
# driver preferences, rider counts, and conflicts into account, then
# persists the resulting assignments to Firestore.

from __future__ import annotations

import json
import uuid

import anthropic

from config import settings
from db.firestore_client import (
    create_assignment,
    get_routes,
    update_driver_assignment,
)
from functions.read_drivers import get_available_drivers as read_drivers
from functions.read_riders import get_riders
from safety.run_log import complete_run, fail_run, start_run
from tests.test_data import get_test_state

# Agent name used for run logging - must match what the dead man's switch
# (safety.run_log.check_saturday_run) looks for.
AGENT_NAME = "assignment_agent"

# Model and token budget for the assignment reasoning call.
CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS = 1500

# System prompt sets the agent's role and locks the response format to
# JSON so STEP 4 can parse it reliably.
SYSTEM_PROMPT = (
    "You are an assignment agent for a church ride coordination system. "
    "You assign volunteer drivers to routes for Sunday service. Be fair, "
    "respect preferences where possible, and always explain your "
    "reasoning clearly. Respond ONLY in valid JSON."
)


def run(sunday_date: str, use_test_data: bool = False) -> dict:
    """Match available drivers to routes for a given Sunday.

    Loads riders/drivers/routes, asks Claude to reason through the best
    driver-to-route assignments, validates and saves the result, and logs
    the run's outcome (success or failure) via safety.run_log.

    Args:
        sunday_date: The Sunday date to run assignments for, e.g.
            "2026-08-23".
        use_test_data: If True, use tests.test_data.get_test_state()
            instead of Firestore, and skip all Firestore writes. Used for
            local testing (see main() below).

    Returns:
        dict: The assignment result with keys "assignments", "issues",
            and "summary". If there were no riders signed up, this is a
            trivial result with an empty "assignments" list.

    Raises:
        RuntimeError: If no drivers are available for this Sunday.
        ValueError: If Claude's response can't be parsed as the expected
            JSON shape.
        Exception: Any other failure (Claude API error, Firestore write
            failure, etc.) is logged via fail_run() and re-raised so the
            caller/scheduler knows the run did not succeed.
    """
    # STEP 1 - Start the run. Every subsequent log entry for this run
    # (success or failure) is tied together by this run_id.
    run_id = start_run(AGENT_NAME, "scheduled")

    try:
        # STEP 2 - Load data, either from fake test fixtures (local
        # testing) or from Firestore (real runs).
        if use_test_data:
            test_state = get_test_state()
            routes = test_state["routes"]
            drivers = test_state["drivers"]
            riders = test_state["riders"]
        else:
            routes = get_routes()
            drivers = read_drivers(sunday_date)
            riders = get_riders(sunday_date)

        if not drivers:
            raise RuntimeError(
                f"No drivers available for sunday_date={sunday_date!r}. "
                "Cannot make any assignments."
            )

        if not riders:
            # Not an error - just nothing to do this week. Log success
            # (the run did complete, it just had no work) and return
            # early with a trivial result.
            complete_run(
                run_id,
                AGENT_NAME,
                details={"sunday_date": sunday_date, "message": "no riders signed up"},
            )
            return {
                "assignments": [],
                "issues": [],
                "summary": f"No riders signed up for {sunday_date}.",
            }

        # STEP 3 - Ask Claude to reason through the driver-to-route
        # assignments given the full picture of routes, drivers, and
        # riders.
        prompt = _build_prompt(routes, drivers, riders)
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = response.content[0].text

        # STEP 4 - Parse and validate Claude's response. Any failure here
        # (bad JSON, missing keys) is treated as a run failure.
        try:
            result = _parse_claude_response(raw_text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                f"Failed to parse Claude's assignment response: {exc}"
            ) from exc

        # STEP 5 - Persist the assignments. Skipped for local test runs
        # so testing never touches real Firestore data.
        if not use_test_data:
            _save_assignments(result["assignments"], sunday_date)

        # STEP 6 - Log success and hand back the full result.
        complete_run(
            run_id,
            AGENT_NAME,
            details={
                "sunday_date": sunday_date,
                "assignment_count": len(result["assignments"]),
                "summary": result["summary"],
            },
        )
        return result

    except Exception as exc:
        # Catches everything above: no drivers, a Claude API error, a
        # response we couldn't parse, or a Firestore write failure.
        # fail_run() never raises itself, so this always finishes and
        # then re-raises the original error for the caller/scheduler.
        fail_run(run_id, AGENT_NAME, exc, details={"sunday_date": sunday_date})
        raise


def _build_prompt(routes: list[dict], drivers: list[dict], riders: list[dict]) -> str:
    """Build the detailed prompt describing this Sunday's assignment problem.

    Args:
        routes: Active routes with their stops.
        drivers: Available drivers with their route preferences.
        riders: Riders needing a ride, each tagged with a route_id.

    Returns:
        str: The full prompt text to send to Claude as the user message.
    """
    # Group riders by route so Claude can see rider counts/details per
    # route at a glance instead of one flat list.
    riders_by_route: dict[str, list[dict]] = {}
    for rider in riders:
        riders_by_route.setdefault(rider.get("route_id"), []).append(rider)

    routes_json = json.dumps(routes, indent=2)
    drivers_json = json.dumps(drivers, indent=2)
    riders_by_route_json = json.dumps(riders_by_route, indent=2)

    return f"""Assign one volunteer driver to each active route below for this Sunday's service.

ROUTES (with stops):
{routes_json}

AVAILABLE DRIVERS (with route preferences):
{drivers_json}

RIDERS, GROUPED BY ROUTE ID:
{riders_by_route_json}

INSTRUCTIONS:
- Assign exactly one available driver to each route that has riders waiting.
- Respect each driver's "preferred_route" where possible. A preferred_route of
  "either" means that driver is flexible and can take any route.
- If two or more drivers prefer the same route, reason through the conflict
  (e.g. consider fairness, other drivers' flexibility, other routes' needs)
  and clearly explain your decision in that assignment's "reasoning" field.
- If a route has no riders waiting, do NOT assign a driver to it - instead,
  note this in "issues" (e.g. "South Route has no riders; no driver assigned.").
- If a route has riders but no suitable driver is available, note this in
  "issues" rather than silently leaving it unassigned.
- List every problem you notice in "issues", even minor ones (e.g. a driver
  whose preference couldn't be honored, a route close to capacity, etc.).

Respond with ONLY valid JSON in exactly this shape (no markdown fences, no
extra commentary before or after the JSON):
{{
  "assignments": [
    {{
      "route_id": "string",
      "route_name": "string",
      "driver_id": "string",
      "driver_name": "string",
      "reasoning": "string explaining why this driver was chosen for this route",
      "rider_count": number
    }}
  ],
  "issues": ["list of any problems found"],
  "summary": "one paragraph summary of all decisions made"
}}"""


def _parse_claude_response(raw_text: str) -> dict:
    """Parse and validate Claude's JSON assignment response.

    Args:
        raw_text: The raw text content of Claude's response.

    Returns:
        dict: The parsed response, guaranteed to have "assignments",
            "issues", and "summary" keys.

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

    if "assignments" not in parsed or "summary" not in parsed:
        raise ValueError(
            "Claude's response is missing required 'assignments' and/or "
            "'summary' keys."
        )

    # "issues" is optional in the model's response - default to empty so
    # callers can always rely on the key being present.
    parsed.setdefault("issues", [])
    return parsed


def _save_assignments(assignments: list[dict], sunday_date: str) -> None:
    """Persist assignments and update each assigned driver's record.

    Args:
        assignments: The list of assignment dicts from Claude's response.
        sunday_date: The Sunday date these assignments are for.

    Raises:
        RuntimeError: If any Firestore write fails.
    """
    # Save each assignment as its own document first. Each gets a
    # client-generated assignment_uuid (independent of the Firestore doc
    # ID) so assignments can be traced/deduplicated across retries.
    for assignment in assignments:
        record = {
            **assignment,
            "sunday_date": sunday_date,
            "assignment_uuid": str(uuid.uuid4()),
        }
        create_assignment(record)

    # Then update each driver's record with the route they were assigned.
    for assignment in assignments:
        update_driver_assignment(assignment["driver_id"], assignment["route_id"])


def main() -> None:
    """Run the assignment agent locally against fake test data.

    This is how you test the agent end-to-end from the terminal: it uses
    tests.test_data.get_test_state() for routes/drivers/riders, makes a
    real call to the Claude API, but skips all Firestore writes since
    use_test_data=True.
    """
    test_state = get_test_state()
    result = run(sunday_date=test_state["sunday_date"], use_test_data=True)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
