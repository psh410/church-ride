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
SEMESTER_SCHEDULE_COLLECTION = "semester_schedule"
SMS_OPT_OUTS_COLLECTION = "sms_opt_outs"

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
# SEMESTER SCHEDULE
# --------------------------------------------------------------------------
def get_semester_schedule() -> list[dict]:
    """Return the full semester shuttle driver schedule, sorted by date.

    Each document represents one Sunday's shuttle_1/shuttle_2/backup
    driver assignments (see functions/send_semester_schedule.py for how
    the returned entries are used to build the Monday schedule email
    and to look up a given Sunday's drivers).

    Returns:
        list[dict]: Semester schedule documents (including their
            Firestore doc "id"), sorted by "date" ascending. Empty list
            if none are found.

    Raises:
        RuntimeError: If the query fails.
    """
    try:
        client = get_client()
        query = client.collection(SEMESTER_SCHEDULE_COLLECTION).order_by("date")
        return [_doc_to_dict(doc) for doc in query.stream()]
    except Exception as exc:
        raise RuntimeError(f"Failed to get semester schedule: {exc}") from exc


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
# SMS OPT-OUTS
# --------------------------------------------------------------------------
# Twilio already blocks/unblocks carrier-level SMS delivery on
# STOP/CANCEL/etc and START/YES automatically - this collection is our
# own app-level record of that status, keyed by E.164 phone number, so
# scheduled sends (functions/send_sms.py) can skip an opted-out number
# instead of attempting (and failing) a send, and so admins can see who
# opted out without checking Twilio directly.
#
# Despite the name, the document per phone is really "everything we
# track about this number": it also carries disclosure_sent, marking
# whether that number has been sent the program disclosure yet. Kept in
# one document on purpose, so a reply needs one read rather than two.
def is_phone_opted_out(phone: str) -> bool:
    """Return whether a phone number has opted out of SMS.

    Args:
        phone: Phone number in E.164 form, e.g. "+12174023446".

    Returns:
        bool: True if this phone has an opt-out record, False if it
            has never opted out (or has since opted back in).

    Raises:
        RuntimeError: If the lookup fails.
    """
    try:
        client = get_client()
        doc = client.collection(SMS_OPT_OUTS_COLLECTION).document(phone).get()
        if not doc.exists:
            return False
        return bool(doc.to_dict().get("opted_out", False))
    except Exception as exc:
        raise RuntimeError(
            f"Failed to check SMS opt-out status for phone={phone!r}: {exc}"
        ) from exc


def record_sms_opt_out(phone: str, keyword: str) -> bool:
    """Record that a phone number has opted out of SMS.

    Args:
        phone: Phone number in E.164 form, e.g. "+12174023446".
        keyword: The opt-out keyword received (e.g. "STOP", "CANCEL"),
            kept for reference/auditing.

    Returns:
        bool: True if the write succeeded.

    Raises:
        RuntimeError: If the write fails.
    """
    try:
        client = get_client()
        client.collection(SMS_OPT_OUTS_COLLECTION).document(phone).set(
            {
                "phone": phone,
                "opted_out": True,
                "keyword": keyword,
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return True
    except Exception as exc:
        raise RuntimeError(
            f"Failed to record SMS opt-out for phone={phone!r}: {exc}"
        ) from exc


def was_disclosure_sent(phone: str) -> bool:
    """Return whether this phone has already been sent the SMS disclosure.

    Used by functions/send_admin_summary.py: an admin's very first
    UPDATE reply carries the program disclosure and opt-out language,
    since that reply is the initial message to them and Twilio requires
    it there. Every later reply is just the counts.

    Args:
        phone: Phone number in E.164 form, e.g. "+12174023446".

    Returns:
        bool: True if the disclosure has already gone out to this
            number, False if it hasn't (or the number is unknown).

    Raises:
        RuntimeError: If the lookup fails. Callers should treat a
            failure as "not yet sent" and include the disclosure, since
            sending it twice is harmless and skipping it is not.
    """
    try:
        client = get_client()
        doc = client.collection(SMS_OPT_OUTS_COLLECTION).document(phone).get()
        if not doc.exists:
            return False
        return bool(doc.to_dict().get("disclosure_sent", False))
    except Exception as exc:
        raise RuntimeError(
            f"Failed to check disclosure status for phone={phone!r}: {exc}"
        ) from exc


def record_disclosure_sent(phone: str) -> bool:
    """Record that this phone has now been sent the SMS disclosure.

    Args:
        phone: Phone number in E.164 form, e.g. "+12174023446".

    Returns:
        bool: True if the write succeeded.

    Raises:
        RuntimeError: If the write fails.
    """
    try:
        client = get_client()
        client.collection(SMS_OPT_OUTS_COLLECTION).document(phone).set(
            {
                "phone": phone,
                "disclosure_sent": True,
                "disclosure_sent_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return True
    except Exception as exc:
        raise RuntimeError(
            f"Failed to record disclosure for phone={phone!r}: {exc}"
        ) from exc


def record_sms_opt_in(phone: str, keyword: str) -> bool:
    """Record that a phone number has opted back in to SMS.

    Args:
        phone: Phone number in E.164 form, e.g. "+12174023446".
        keyword: The opt-in keyword received (e.g. "START", "YES"),
            kept for reference/auditing.

    Returns:
        bool: True if the write succeeded.

    Raises:
        RuntimeError: If the write fails.
    """
    try:
        client = get_client()
        client.collection(SMS_OPT_OUTS_COLLECTION).document(phone).set(
            {
                "phone": phone,
                "opted_out": False,
                "keyword": keyword,
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return True
    except Exception as exc:
        raise RuntimeError(
            f"Failed to record SMS opt-in for phone={phone!r}: {exc}"
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
