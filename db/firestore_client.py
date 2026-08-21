# Firestore client initialization and shared database access helpers.
#
# This module is the single connection point between all agents and
# Firestore. No other module should import `google.cloud.firestore`
# directly - everything goes through the functions defined here so that
# collection names, query shapes, and error handling stay consistent.

from __future__ import annotations

from typing import Any, Optional

from google.cloud import firestore

from config import settings

# --------------------------------------------------------------------------
# Collection names
# --------------------------------------------------------------------------
# Centralizing these avoids typos scattered across the codebase and makes
# it easy to rename a collection in one place.
RIDERS_COLLECTION = "riders"
DRIVERS_COLLECTION = "drivers"
ROUTES_COLLECTION = "routes"
ASSIGNMENTS_COLLECTION = "assignments"
RUN_LOGS_COLLECTION = "run_logs"

# --------------------------------------------------------------------------
# Client initialization
# --------------------------------------------------------------------------
# A single Firestore client is created lazily and reused by every function
# in this module (and therefore by every agent that calls into it). The
# Firestore client already manages its own connection pool internally, so
# one shared instance is the recommended usage pattern.
_client: Optional[firestore.Client] = None


def get_client() -> firestore.Client:
    """Return the shared Firestore client, creating it on first use.

    Returns:
        firestore.Client: The shared Firestore client instance.

    Raises:
        RuntimeError: If the Firestore client cannot be initialized
            (e.g. missing/invalid Google Cloud credentials or project).
    """
    global _client

    if _client is None:
        try:
            _client = firestore.Client(project=settings.GOOGLE_CLOUD_PROJECT)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to initialize Firestore client: {exc}"
            ) from exc

    return _client


# --------------------------------------------------------------------------
# RIDERS
# --------------------------------------------------------------------------
def get_riders(sunday_date: str) -> list[dict]:
    """Return all riders for a given Sunday date.

    Args:
        sunday_date: The Sunday date to fetch riders for, e.g. "2026-08-23".

    Returns:
        list[dict]: Rider documents (including their Firestore doc "id"),
            or an empty list if none are found.

    Raises:
        RuntimeError: If the query fails.
    """
    try:
        client = get_client()
        query = client.collection(RIDERS_COLLECTION).where(
            "sunday_date", "==", sunday_date
        )
        return [_doc_to_dict(doc) for doc in query.stream()]
    except Exception as exc:
        raise RuntimeError(
            f"Failed to get riders for sunday_date={sunday_date!r}: {exc}"
        ) from exc


def get_riders_by_route(sunday_date: str, route_id: str) -> list[dict]:
    """Return riders for a specific route on a given Sunday.

    Args:
        sunday_date: The Sunday date to filter by, e.g. "2026-08-23".
        route_id: The Firestore document ID of the route.

    Returns:
        list[dict]: Rider documents assigned to the given route, or an
            empty list if none are found.

    Raises:
        RuntimeError: If the query fails.
    """
    try:
        client = get_client()
        query = (
            client.collection(RIDERS_COLLECTION)
            .where("sunday_date", "==", sunday_date)
            .where("route_id", "==", route_id)
        )
        return [_doc_to_dict(doc) for doc in query.stream()]
    except Exception as exc:
        raise RuntimeError(
            f"Failed to get riders for sunday_date={sunday_date!r}, "
            f"route_id={route_id!r}: {exc}"
        ) from exc


def update_rider_status(rider_id: str, status: str) -> bool:
    """Update a rider's status.

    Args:
        rider_id: The Firestore document ID of the rider.
        status: The new status, one of "pending", "confirmed", or
            "cancelled".

    Returns:
        bool: True if the update succeeded.

    Raises:
        RuntimeError: If the update fails.
    """
    try:
        client = get_client()
        client.collection(RIDERS_COLLECTION).document(rider_id).update(
            {
                "status": status,
                "status_updated_at": firestore.SERVER_TIMESTAMP,
            }
        )
        return True
    except Exception as exc:
        raise RuntimeError(
            f"Failed to update status for rider_id={rider_id!r} to "
            f"status={status!r}: {exc}"
        ) from exc


# --------------------------------------------------------------------------
# DRIVERS
# --------------------------------------------------------------------------
def get_available_drivers(sunday_date: str) -> list[dict]:
    """Return all available drivers for a given Sunday.

    Args:
        sunday_date: The Sunday date to fetch drivers for, e.g.
            "2026-08-23".

    Returns:
        list[dict]: Driver documents marked available for that date, or an
            empty list if none are found.

    Raises:
        RuntimeError: If the query fails.
    """
    try:
        client = get_client()
        query = (
            client.collection(DRIVERS_COLLECTION)
            .where("sunday_date", "==", sunday_date)
            .where("available", "==", True)
        )
        return [_doc_to_dict(doc) for doc in query.stream()]
    except Exception as exc:
        raise RuntimeError(
            f"Failed to get available drivers for "
            f"sunday_date={sunday_date!r}: {exc}"
        ) from exc


def update_driver_assignment(driver_id: str, route_id: str) -> bool:
    """Record which route a driver is assigned to.

    Args:
        driver_id: The Firestore document ID of the driver.
        route_id: The Firestore document ID of the route being assigned.

    Returns:
        bool: True if the update succeeded.

    Raises:
        RuntimeError: If the update fails.
    """
    try:
        client = get_client()
        client.collection(DRIVERS_COLLECTION).document(driver_id).update(
            {
                "route_id": route_id,
                "assigned_at": firestore.SERVER_TIMESTAMP,
            }
        )
        return True
    except Exception as exc:
        raise RuntimeError(
            f"Failed to assign route_id={route_id!r} to "
            f"driver_id={driver_id!r}: {exc}"
        ) from exc


# --------------------------------------------------------------------------
# ROUTES
# --------------------------------------------------------------------------
def get_routes() -> list[dict]:
    """Return all active routes from Firestore.

    Routes are dynamic (created/edited by admins), so they always come
    from Firestore rather than any hardcoded config.

    Returns:
        list[dict]: Active route documents, or an empty list if none are
            found.

    Raises:
        RuntimeError: If the query fails.
    """
    try:
        client = get_client()
        query = client.collection(ROUTES_COLLECTION).where("active", "==", True)
        return [_doc_to_dict(doc) for doc in query.stream()]
    except Exception as exc:
        raise RuntimeError(f"Failed to get active routes: {exc}") from exc


def get_route(route_id: str) -> dict:
    """Return a single route by ID.

    Args:
        route_id: The Firestore document ID of the route.

    Returns:
        dict: The route document, including its "id" field. Returns an
            empty dict if no route with that ID exists.

    Raises:
        RuntimeError: If the lookup fails.
    """
    try:
        client = get_client()
        doc = client.collection(ROUTES_COLLECTION).document(route_id).get()
        return _doc_to_dict(doc) if doc.exists else {}
    except Exception as exc:
        raise RuntimeError(f"Failed to get route_id={route_id!r}: {exc}") from exc


# --------------------------------------------------------------------------
# ASSIGNMENTS
# --------------------------------------------------------------------------
def create_assignment(assignment: dict) -> str:
    """Create a new assignment document.

    Args:
        assignment: The assignment data to store (e.g. rider_id,
            driver_id, route_id, sunday_date).

    Returns:
        str: The Firestore document ID of the newly created assignment.

    Raises:
        RuntimeError: If the write fails.
    """
    try:
        client = get_client()
        payload = {
            **assignment,
            "created_at": firestore.SERVER_TIMESTAMP,
        }
        _, doc_ref = client.collection(ASSIGNMENTS_COLLECTION).add(payload)
        return doc_ref.id
    except Exception as exc:
        raise RuntimeError(f"Failed to create assignment: {exc}") from exc


def get_assignment(sunday_date: str) -> list[dict]:
    """Return all assignments for a given Sunday.

    Args:
        sunday_date: The Sunday date to fetch assignments for, e.g.
            "2026-08-23".

    Returns:
        list[dict]: Assignment documents for that date, or an empty list
            if none are found.

    Raises:
        RuntimeError: If the query fails.
    """
    try:
        client = get_client()
        query = client.collection(ASSIGNMENTS_COLLECTION).where(
            "sunday_date", "==", sunday_date
        )
        return [_doc_to_dict(doc) for doc in query.stream()]
    except Exception as exc:
        raise RuntimeError(
            f"Failed to get assignments for sunday_date={sunday_date!r}: {exc}"
        ) from exc


# --------------------------------------------------------------------------
# RUN LOG
# --------------------------------------------------------------------------
def write_run_log(
    run_id: str,
    agent: str,
    status: str,
    message: str,
    details: Optional[dict] = None,
) -> bool:
    """Write a run log entry to Firestore.

    Used by the safety/run_log module and each agent to record what
    happened during a run, so failures can be audited after the fact.

    Args:
        run_id: Identifier shared by all log entries from the same run.
        agent: Name of the agent writing the log entry (e.g.
            "monitor_agent").
        status: Status of this log entry (e.g. "started", "success",
            "error").
        message: Human-readable description of what happened.
        details: Optional extra structured data to store alongside the
            entry (e.g. counts, IDs involved, error traceback).

    Returns:
        bool: True if the write succeeded.

    Raises:
        RuntimeError: If the write fails.
    """
    try:
        client = get_client()
        entry: dict[str, Any] = {
            "run_id": run_id,
            "agent": agent,
            "status": status,
            "message": message,
            "details": details or {},
            "timestamp": firestore.SERVER_TIMESTAMP,
        }
        client.collection(RUN_LOGS_COLLECTION).add(entry)
        return True
    except Exception as exc:
        raise RuntimeError(
            f"Failed to write run log for run_id={run_id!r}, "
            f"agent={agent!r}: {exc}"
        ) from exc


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------
def _doc_to_dict(doc: firestore.DocumentSnapshot) -> dict:
    """Convert a Firestore document snapshot into a plain dict.

    The document's ID is included as an "id" key so callers never need to
    reach back into the Firestore SDK to know which document they're
    looking at.

    Args:
        doc: The Firestore document snapshot to convert.

    Returns:
        dict: The document's data with an added "id" field.
    """
    data = doc.to_dict() or {}
    data["id"] = doc.id
    return data
