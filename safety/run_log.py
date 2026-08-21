# Logs agent runs and actions for auditing and safety review.
#
# This module is the safety net that wraps every agent run: agents call
# start_run() before doing any work, complete_run()/fail_run() when they
# finish, and the dead man's switch calls check_saturday_run() to confirm
# the assignment agent actually completed successfully for a given Sunday.

from __future__ import annotations

import traceback
import uuid
from typing import Optional

from db.firestore_client import RUN_LOGS_COLLECTION, get_client, write_run_log

# Name used by the assignment agent when it calls start_run()/complete_run().
# The dead man's switch looks for a "success" entry from this exact agent
# name, so it must match whatever assignment_agent.py passes as `agent`.
ASSIGNMENT_AGENT_NAME = "assignment_agent"

# Valid values for the `trigger` argument to start_run().
SCHEDULED_TRIGGER = "scheduled"
EVENT_TRIGGER = "event"


def start_run(agent: str, trigger: str) -> str:
    """Record the start of an agent run and return its run ID.

    Args:
        agent: Name of the agent starting the run (e.g.
            "assignment_agent").
        trigger: What caused this run to start - either "scheduled"
            (e.g. the Saturday cron job) or "event" (e.g. a Pub/Sub
            message).

    Returns:
        str: A newly generated unique run_id. The caller should pass this
            same run_id to every subsequent log entry (including
            complete_run/fail_run) for this run.

    Raises:
        RuntimeError: If the run log entry cannot be written. Unlike
            fail_run(), a failure here is allowed to propagate because if
            we can't even record that a run started, the caller needs to
            know immediately rather than proceeding blind.
    """
    run_id = str(uuid.uuid4())

    write_run_log(
        run_id=run_id,
        agent=agent,
        status="started",
        message=f"{agent} run started (trigger={trigger}).",
        details={"trigger": trigger},
    )

    return run_id


def complete_run(run_id: str, agent: str, details: Optional[dict] = None) -> bool:
    """Record that an agent run finished successfully.

    Args:
        run_id: The run_id returned by start_run() for this run.
        agent: Name of the agent completing the run.
        details: Optional extra structured data about the successful run
            (e.g. counts of riders/drivers assigned, sunday_date).

    Returns:
        bool: True, since the write succeeded.

    Raises:
        RuntimeError: If the run log entry cannot be written. As with
            start_run(), this is allowed to propagate so a broken audit
            trail doesn't go unnoticed.
    """
    write_run_log(
        run_id=run_id,
        agent=agent,
        status="success",
        message=f"{agent} run completed successfully.",
        details=details or {},
    )

    return True


def fail_run(
    run_id: str,
    agent: str,
    error: Exception,
    details: Optional[dict] = None,
) -> bool:
    """Record that an agent run failed, capturing the full traceback.

    fail_run() is intentionally called from inside exception-handling code
    (e.g. a top-level `except` block in an agent). It deliberately NEVER
    raises an exception itself: if it did, a failure while trying to log
    a failure would replace/mask the original error with a new one,
    causing the real root cause to be lost. Instead, if writing the log
    entry fails, we print to the console (which is captured by Cloud
    Logging/stdout in production) so the original error is never
    swallowed, and simply return False.

    Args:
        run_id: The run_id returned by start_run() for this run.
        agent: Name of the agent reporting the failure.
        error: The exception that caused the run to fail.
        details: Optional extra structured data about the failure (e.g.
            which rider/driver/route was being processed).

    Returns:
        bool: Always False, whether or not the log write itself succeeded.
    """
    failure_details = {
        **(details or {}),
        "error_message": str(error),
        "traceback": traceback.format_exc(),
    }

    try:
        write_run_log(
            run_id=run_id,
            agent=agent,
            status="failure",
            message=f"{agent} run failed: {error}",
            details=failure_details,
        )
    except Exception as log_exc:
        # Logging the failure itself failed. Print instead of raising so
        # the original `error` is never hidden behind a secondary error.
        print(
            f"[run_log] CRITICAL: failed to write failure log for "
            f"run_id={run_id!r}, agent={agent!r}. "
            f"Original error: {error!r}. Logging error: {log_exc!r}"
        )

    return False


def has_already_notified(run_id: str, agent: str = "notification_agent") -> bool:
    """Check whether this agent already logged success for a run.

    Used by the notification agent to guarantee idempotency: if
    notifications for this run_id were already sent successfully once
    (e.g. this function is being called again because the triggering
    event was redelivered), it should never send them a second time.

    Args:
        run_id: The run_id to check.
        agent: The agent name to check for (defaults to
            "notification_agent").

    Returns:
        bool: True if a "success" run log entry already exists for this
            run_id/agent pair, False otherwise.

    Raises:
        RuntimeError: If the query fails.
    """
    try:
        client = get_client()
        query = (
            client.collection(RUN_LOGS_COLLECTION)
            .where("run_id", "==", run_id)
            .where("agent", "==", agent)
            .where("status", "==", "success")
            .limit(1)
        )
        return len(list(query.stream())) > 0
    except Exception as exc:
        raise RuntimeError(
            f"Failed to check prior notifications for run_id={run_id!r}, "
            f"agent={agent!r}: {exc}"
        ) from exc


def check_saturday_run(sunday_date: str) -> bool:
    """Check whether the assignment agent completed successfully.

    Used by the dead man's switch: if no matching "success" entry exists
    by DEAD_MAN_HOUR, something is wrong and an alert should be raised.

    Args:
        sunday_date: The Sunday date to check, e.g. "2026-08-23".

    Returns:
        bool: True if a "success" run log entry from the assignment agent
            exists for this sunday_date, False otherwise.

    Raises:
        RuntimeError: If the query fails.
    """
    try:
        client = get_client()
        query = (
            client.collection(RUN_LOGS_COLLECTION)
            .where("agent", "==", ASSIGNMENT_AGENT_NAME)
            .where("status", "==", "success")
            .where("details.sunday_date", "==", sunday_date)
            .limit(1)
        )
        return len(list(query.stream())) > 0
    except Exception as exc:
        raise RuntimeError(
            f"Failed to check saturday run status for "
            f"sunday_date={sunday_date!r}: {exc}"
        ) from exc
