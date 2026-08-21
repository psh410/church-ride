# Agent that matches riders with drivers and creates ride assignments.
#
# This is the most important agent in the system: it uses the Claude API
# to reason about which volunteer driver should take which route, taking
# driver preferences, rider counts, and conflicts into account, then
# persists the resulting assignments to Firestore.

from __future__ import annotations

import json
import uuid
from datetime import datetime

import anthropic

from config import settings
from db.firestore_client import create_assignment, update_driver_assignment
from functions.read_riders import get_riders
from functions.read_sheets import get_all_drivers_with_history
from functions.read_sheets import get_routes as get_sheet_routes
from safety.run_log import complete_run, fail_run, start_run
from tests.test_data import get_test_state

# Agent name used for run logging - must match what the dead man's switch
# (safety.run_log.check_saturday_run) looks for.
AGENT_NAME = "assignment_agent"

# Model and token budget for the assignment reasoning call.
CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS = 1500

# Church context included in every prompt.
CHURCH_NAME = "Covenant Fellowship Church (CFC)"
CHURCH_LOCATION = "Champaign, IL"

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
        sunday_date: The Sunday date to run assignments for, in ISO
            "YYYY-MM-DD" format, e.g. "2026-08-23".
        use_test_data: If True, use tests.test_data.get_test_state()
            instead of Firestore/Sheets, and skip all Firestore writes.
            Used for local testing (see main() below).

    Returns:
        dict: The assignment result with keys "assignments", "issues",
            and "summary". If there were no riders signed up, this is a
            trivial result with an empty "assignments" list.

    Raises:
        RuntimeError: If no drivers are available for this Sunday.
        ValueError: If Claude's response can't be parsed as the expected
            JSON shape.
        Exception: Any other failure (Claude API error, Sheets/Firestore
            failure, etc.) is logged via fail_run() and re-raised so the
            caller/scheduler knows the run did not succeed.
    """
    # STEP 1 - Start the run. Every subsequent log entry for this run
    # (success or failure) is tied together by this run_id.
    run_id = start_run(AGENT_NAME, "scheduled")

    try:
        # STEP 2 - Load data, either from fake test fixtures (local
        # testing) or from Google Sheets + Firestore (real runs). Driver
        # and route data live in Sheets now, not Firestore - only riders
        # still come from Firestore.
        if use_test_data:
            test_state = get_test_state()
            routes = test_state["routes"]
            drivers = test_state["drivers"]
            riders = test_state["riders"]
        else:
            routes = get_sheet_routes()
            # get_all_drivers_with_history() expects the Sheets tab's
            # "M/D/YY" date format, while the rest of this system (and
            # get_riders()) uses ISO "YYYY-MM-DD" - convert here so run()
            # can keep taking a single ISO sunday_date.
            drivers = get_all_drivers_with_history(_to_sheet_date_format(sunday_date))
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
        prompt = _build_prompt(sunday_date, routes, drivers, riders)
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


def _to_sheet_date_format(iso_date: str) -> str:
    """Convert an ISO "YYYY-MM-DD" date into the Sheets tab's "M/D/YY" format.

    functions.read_sheets reads dates as they appear in the "Available
    Drivers" tab (e.g. "8/23/26"), while the rest of this system uses ISO
    "YYYY-MM-DD". This bridges the two so run() only has to accept one
    format from its callers.

    Args:
        iso_date: A date string in "YYYY-MM-DD" format.

    Returns:
        str: The same date in "M/D/YY" format, e.g. "2026-08-23" ->
            "8/23/26".

    Raises:
        ValueError: If iso_date isn't valid "YYYY-MM-DD".
    """
    parsed = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{parsed.month}/{parsed.day}/{parsed.strftime('%y')}"


def _build_prompt(
    sunday_date: str, routes: list[dict], drivers: list[dict], riders: list[dict]
) -> str:
    """Build the detailed prompt describing this Sunday's assignment problem.

    Args:
        sunday_date: The Sunday date these assignments are for.
        routes: The two fixed shuttles, each with shuttle_id,
            shuttle_name, van, and stops (from
            functions.read_sheets.get_routes()).
        drivers: Available drivers enriched with driving history (from
            functions.read_sheets.get_all_drivers_with_history()).
        riders: Riders needing a ride, each tagged with a route_id/stop.

    Returns:
        str: The full prompt text to send to Claude as the user message.
    """
    routes_json = json.dumps(routes, indent=2)
    drivers_json = json.dumps(
        _summarize_drivers_for_prompt(drivers), indent=2, default=str
    )
    rider_counts_json = json.dumps(_build_rider_counts(routes, riders), indent=2)

    return f"""CONTEXT:
- Church: {CHURCH_NAME}, {CHURCH_LOCATION}
- Sunday being assigned: {sunday_date}
- This church always runs two fixed shuttles (see ROUTES below). Shuttle 1's
  van (Ford Transit) is newer than Shuttle 2's van (GMC Savanna).

ROUTES:
{routes_json}

AVAILABLE DRIVERS:
{drivers_json}

RIDER COUNTS (riders waiting at each stop, by shuttle):
{rider_counts_json}

ASSIGNMENT RULES - reason through ALL of these before deciding:

RULE 1 - AGE RULE (most important):
Drivers who are 25-30 or in their 30s should be assigned to Shuttle 1
(Ford Transit - the newer van, safer for younger/less experienced
drivers). Drivers who are 40+ should be assigned to Shuttle 2 (GMC
Savanna - the older van). If no driver in their 20s/30s is available,
it's acceptable to assign a 40+ driver to both shuttles - clearly
explain why in that assignment's "reasoning" and also note it in
"issues".

RULE 2 - SCARCITY RULE:
Prioritize the driver(s) with the LOWEST total_available_sundays_remaining.
Use up scarce drivers (few Sundays left before they're unavailable) ahead
of drivers who have many more remaining Sundays to be scheduled on later.

RULE 3 - ROTATION RULE:
Avoid assigning the same driver two weeks in a row whenever avoidable.
Check each driver's last_driven_date against sunday_date before assigning
them again.

RULE 4 - FAIRNESS RULE:
Balance times_driven_this_semester across drivers over time - don't let
one person carry a disproportionate share of the driving while others
sit idle.

RULE 5 - COMMENTS RULE:
Read each driver's additional_comments carefully and factor in any
constraints or preferences mentioned (e.g. upcoming unavailability,
schedule conflicts, other commitments).

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


def _summarize_drivers_for_prompt(drivers: list[dict]) -> list[dict]:
    """Pick out exactly the driver fields relevant to Claude's decision.

    Args:
        drivers: Full driver dicts (e.g. from
            get_all_drivers_with_history(), which also includes fields
            like phone/conflict_dates that aren't needed here).

    Returns:
        list[dict]: One dict per driver with name, email, age_range,
            times_driven_this_semester, last_driven_date,
            total_available_sundays_remaining, and additional_comments.
    """
    summarized = []
    for driver in drivers:
        summarized.append(
            {
                "name": driver.get("name"),
                "email": driver.get("email"),
                "age_range": driver.get("age_range"),
                # Support both field names: get_all_drivers_with_history()
                # currently returns "times_driven", while this prompt (and
                # main()'s fixtures) use "times_driven_this_semester".
                "times_driven_this_semester": driver.get(
                    "times_driven_this_semester", driver.get("times_driven", 0)
                ),
                "last_driven_date": driver.get("last_driven_date"),
                "total_available_sundays_remaining": driver.get(
                    "total_available_sundays_remaining"
                ),
                "additional_comments": driver.get("additional_comments"),
            }
        )
    return summarized


def _build_rider_counts(routes: list[dict], riders: list[dict]) -> dict:
    """Count how many riders are waiting at each stop, grouped by shuttle.

    Args:
        routes: The shuttle/route dicts (each with a "shuttle_id" and
            "stops").
        riders: Riders needing a ride, each tagged with a route_id/stop
            matching a route's shuttle_id.

    Returns:
        dict: Maps each route's shuttle_id to
            {"shuttle_name": str, "total_riders": int,
            "riders_by_stop": {stop_name: count}}.
    """
    riders_by_route: dict[str, list[dict]] = {}
    for rider in riders:
        riders_by_route.setdefault(rider.get("route_id"), []).append(rider)

    counts_by_shuttle = {}
    for route in routes:
        shuttle_id = route.get("shuttle_id")
        shuttle_riders = riders_by_route.get(shuttle_id, [])

        riders_by_stop: dict[str, int] = {}
        for rider in shuttle_riders:
            stop = rider.get("stop", "Unknown stop")
            riders_by_stop[stop] = riders_by_stop.get(stop, 0) + 1

        counts_by_shuttle[shuttle_id] = {
            "shuttle_name": route.get("shuttle_name"),
            "total_riders": len(shuttle_riders),
            "riders_by_stop": riders_by_stop,
        }

    return counts_by_shuttle


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
    """Exercise the assignment prompt/reasoning step against realistic fake data.

    Builds fake drivers/routes shaped like what
    get_all_drivers_with_history()/get_sheet_routes() return in
    production (age_range, times_driven_this_semester,
    total_available_sundays_remaining, van names, etc.), plus a small
    fake rider list, then calls Claude directly with the real prompt.

    This calls Claude directly rather than run(sunday_date,
    use_test_data=True), since run()'s test-data path is wired to
    tests.test_data.get_test_state() (older Firestore-shaped fixtures)
    which doesn't match this Sheets-based driver/route schema. Nothing
    here touches Google Sheets, Firestore, or run logging.
    """
    from dotenv import load_dotenv

    load_dotenv()

    sunday_date = "2026-08-23"

    drivers = [
        {
            "name": "Josiah Chong",
            "email": "joseki121@gmail.com",
            "age_range": "25-30",
            "times_driven_this_semester": 0,
            "last_driven_date": None,
            "total_available_sundays_remaining": 12,
            "additional_comments": "Worship team schedule not set yet",
        },
        {
            "name": "Ryan Bielak",
            "email": "ryanjsk17@gmail.com",
            "age_range": "30s",
            "times_driven_this_semester": 0,
            "last_driven_date": None,
            "total_available_sundays_remaining": 14,
            "additional_comments": None,
        },
        {
            "name": "Peter Hahn",
            "email": "peterhahn@cfchome.org",
            "age_range": "40+",
            "times_driven_this_semester": 0,
            "last_driven_date": None,
            "total_available_sundays_remaining": 13,
            "additional_comments": None,
        },
        {
            "name": "Yong Wook Kim",
            "email": "ywkim.312@gmail.com",
            "age_range": "40+",
            "times_driven_this_semester": 0,
            "last_driven_date": None,
            "total_available_sundays_remaining": 14,
            "additional_comments": "May be out of town late Aug/Sep",
        },
        {
            "name": "Robin Varghese",
            "email": "dr_robin_497@hotmail.com",
            "age_range": "40+",
            "times_driven_this_semester": 0,
            "last_driven_date": None,
            "total_available_sundays_remaining": 8,
            "additional_comments": None,
        },
        {
            "name": "Albert Lee",
            "email": "albertlee77@gmail.com",
            "age_range": "40+",
            "times_driven_this_semester": 0,
            "last_driven_date": None,
            "total_available_sundays_remaining": 13,
            "additional_comments": "Availability unknown beyond September",
        },
    ]

    routes = [
        {
            "shuttle_id": "shuttle_1",
            "shuttle_name": "Shuttle 1",
            "van": "Ford Transit (Gray)",
            "stops": [
                {"stop_name": "FAR", "pickup_time": "9:05 AM"},
                {"stop_name": "SDRP", "pickup_time": "9:10 AM"},
            ],
        },
        {
            "shuttle_id": "shuttle_2",
            "shuttle_name": "Shuttle 2",
            "van": "GMC Savanna (Silver)",
            "stops": [
                {"stop_name": "Allen", "pickup_time": "9:00 AM"},
                {"stop_name": "ISR", "pickup_time": "9:05 AM"},
                {"stop_name": "Icon", "pickup_time": "9:10 AM"},
            ],
        },
    ]

    # A small fake rider list matching the shuttles/stops above - no
    # rider fixture was specified, so this just keeps the demo
    # self-consistent (riders reference the same shuttle_ids as routes).
    riders = [
        {"name": "Test Rider 1", "route_id": "shuttle_1", "stop": "FAR"},
        {"name": "Test Rider 2", "route_id": "shuttle_1", "stop": "SDRP"},
        {"name": "Test Rider 3", "route_id": "shuttle_2", "stop": "Allen"},
        {"name": "Test Rider 4", "route_id": "shuttle_2", "stop": "ISR"},
        {"name": "Test Rider 5", "route_id": "shuttle_2", "stop": "Icon"},
    ]

    prompt = _build_prompt(sunday_date, routes, drivers, riders)
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    result = _parse_claude_response(response.content[0].text)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
