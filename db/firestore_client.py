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
RIDER_CONFIRMATIONS_COLLECTION = "rider_confirmations"
RIDER_REMINDERS_COLLECTION = "rider_reminders_sent"
RETURN_RIDE_REQUESTS_COLLECTION = "return_ride_requests"
RETURN_RIDE_COUNTS_COLLECTION = "return_ride_counts"
RIDE_CANCELLATIONS_COLLECTION = "ride_cancellations"

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
# RIDER SIGNUP CONFIRMATIONS
# --------------------------------------------------------------------------
# One document per (phone, Sunday), written when a signup confirmation
# text goes out. Guards against sending the same rider two confirmations
# for the same week, whether from a double form submission or a retried
# webhook call. Independent of the Apps Script's own "/duplicate" flag on
# purpose: that flag can only catch what the script itself sees.
def was_rider_confirmed(phone: str, sunday_date: str) -> bool:
    """Return whether this phone already got a confirmation for this Sunday.

    Args:
        phone: Phone number in E.164 form.
        sunday_date: The Sunday in ISO "YYYY-MM-DD" form.

    Returns:
        bool: True if a confirmation was already recorded.

    Raises:
        RuntimeError: If the lookup fails.
    """
    try:
        client = get_client()
        doc = (
            client.collection(RIDER_CONFIRMATIONS_COLLECTION)
            .document(f"{phone}_{sunday_date}")
            .get()
        )
        return doc.exists
    except Exception as exc:
        raise RuntimeError(
            f"Failed to check rider confirmation for phone={phone!r}, "
            f"sunday_date={sunday_date!r}: {exc}"
        ) from exc


def record_rider_confirmed(phone: str, sunday_date: str, details: Optional[dict] = None) -> bool:
    """Record that a signup confirmation went out to this phone.

    Args:
        phone: Phone number in E.164 form.
        sunday_date: The Sunday in ISO "YYYY-MM-DD" form.
        details: Optional extra context (stop, category, row number).

    Returns:
        bool: True if the write succeeded.

    Raises:
        RuntimeError: If the write fails.
    """
    try:
        client = get_client()
        client.collection(RIDER_CONFIRMATIONS_COLLECTION).document(
            f"{phone}_{sunday_date}"
        ).set(
            {
                "phone": phone,
                "sunday_date": sunday_date,
                "details": details or {},
                "confirmed_at": firestore.SERVER_TIMESTAMP,
            }
        )
        return True
    except Exception as exc:
        raise RuntimeError(
            f"Failed to record rider confirmation for phone={phone!r}, "
            f"sunday_date={sunday_date!r}: {exc}"
        ) from exc


def claim_rider_reminder(phone: str, sunday_date: str) -> bool:
    """Claim the one Saturday reminder text this phone gets for a Sunday.

    The write only succeeds if nobody has claimed it yet, so two runs of
    the job (a Cloud Scheduler retry, a manual run on top of the
    scheduled one, two containers at once) can never both text the same
    rider. Claim first, send second, and release the claim if the send
    fails, so a retry still reaches the people who were missed.

    Args:
        phone: Phone number in E.164 form.
        sunday_date: The Sunday in ISO "YYYY-MM-DD" form.

    Returns:
        bool: True if this call claimed it (go ahead and send), False if
            the rider was already texted for this Sunday.

    Raises:
        RuntimeError: If Firestore can't be reached. Callers should not
            send when the claim can't be made.
    """
    from google.api_core.exceptions import AlreadyExists

    try:
        client = get_client()
        client.collection(RIDER_REMINDERS_COLLECTION).document(
            f"{sunday_date}_{phone}"
        ).create({"phone": phone, "sunday_date": sunday_date,
                  "claimed_at": firestore.SERVER_TIMESTAMP})
        return True
    except AlreadyExists:
        return False
    except Exception as exc:
        raise RuntimeError(
            f"Failed to claim rider reminder for phone={phone!r}, "
            f"sunday_date={sunday_date!r}: {exc}"
        ) from exc


def release_rider_reminder(phone: str, sunday_date: str) -> None:
    """Give back a claim after a failed send so a retry can try again."""
    try:
        client = get_client()
        client.collection(RIDER_REMINDERS_COLLECTION).document(
            f"{sunday_date}_{phone}"
        ).delete()
    except Exception as exc:
        # Worst case the rider is skipped on retry. Say so loudly.
        logger.error(
            "Could not release rider reminder claim for phone=%r sunday_date=%r: %s",
            phone, sunday_date, exc,
        )


def get_rider_confirmation(phone: str, sunday_date: str) -> Optional[dict]:
    """Return the confirmation record for this phone and Sunday, if any.

    Richer than was_rider_confirmed(): the caller needs the stored stop
    and row so a repeat signup can be answered with the same details the
    rider was originally given, rather than whatever the duplicate row
    happens to say.

    Args:
        phone: Phone number in E.164 form.
        sunday_date: The Sunday in ISO "YYYY-MM-DD" form.

    Returns:
        dict or None: The stored record with its "details" flattened in,
            or None if no confirmation has gone out.

    Raises:
        RuntimeError: If the lookup fails.
    """
    try:
        doc = (
            get_client()
            .collection(RIDER_CONFIRMATIONS_COLLECTION)
            .document(f"{phone}_{sunday_date}")
            .get()
        )
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read rider confirmation for phone={phone!r}, "
            f"sunday_date={sunday_date!r}: {exc}"
        ) from exc

    if not doc.exists:
        return None

    data = doc.to_dict() or {}
    record = dict(data.get("details") or {})
    record.update({k: v for k, v in data.items() if k != "details"})
    return record


def record_duplicate_notice_sent(phone: str, sunday_date: str) -> bool:
    """Mark that the "you already signed up" text went out this week.

    Caps that text at one per rider per Sunday. Someone who submits the
    form five times should be told once, not five times.

    Args:
        phone: Phone number in E.164 form.
        sunday_date: The Sunday in ISO "YYYY-MM-DD" form.

    Returns:
        bool: True if the write succeeded.

    Raises:
        RuntimeError: If the write fails.
    """
    try:
        get_client().collection(RIDER_CONFIRMATIONS_COLLECTION).document(
            f"{phone}_{sunday_date}"
        ).set(
            {
                "duplicate_notice_sent": True,
                "duplicate_notice_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return True
    except Exception as exc:
        raise RuntimeError(
            f"Failed to record duplicate notice for phone={phone!r}, "
            f"sunday_date={sunday_date!r}: {exc}"
        ) from exc


def clear_rider_confirmation(phone: str, sunday_date: str) -> bool:
    """Delete this phone's confirmation record for one Sunday.

    Only used to make a signup testable again: with the record in place
    the confirmation flow correctly refuses to text the same number
    twice, which makes a repeat test look broken.

    Args:
        phone: Phone number in E.164 form.
        sunday_date: The Sunday in ISO "YYYY-MM-DD" form.

    Returns:
        bool: True if a record existed and was deleted, False if there
            was nothing to delete.

    Raises:
        RuntimeError: If the delete fails.
    """
    try:
        ref = (
            get_client()
            .collection(RIDER_CONFIRMATIONS_COLLECTION)
            .document(f"{phone}_{sunday_date}")
        )
        if not ref.get().exists:
            return False
        ref.delete()
        return True
    except Exception as exc:
        raise RuntimeError(
            f"Failed to clear rider confirmation for phone={phone!r}, "
            f"sunday_date={sunday_date!r}: {exc}"
        ) from exc


# --------------------------------------------------------------------------
# RETURN RIDE REQUESTS
# --------------------------------------------------------------------------
# One document per (phone, date) in RETURN_RIDE_REQUESTS_COLLECTION,
# written when someone texts RIDE after service to request a ride home
# (see functions/return_ride.py). A second, one-document-per-date
# collection, RETURN_RIDE_COUNTS_COLLECTION, holds just a running count.
#
# Keying both by date is also what makes the count reset every Sunday
# with no separate cleanup job: next Sunday's date simply doesn't have a
# counter document yet, so its first request starts at position 1
# regardless of how last Sunday ended. Nothing carries over on purpose -
# see test_each_date_gets_its_own_independent_counter in
# tests/test_return_ride.py.
#
# Both are written together inside a single Firestore transaction so two
# people texting RIDE within the same second still get distinct, correct
# positions. Reading "how many are there so far" with a plain query and
# then writing the new document separately would race: both requests
# could read the same count before either write commits, and both get
# assigned the same position - exactly the kind of bug that only shows
# up on a live Sunday when the 28-seat boundary actually matters.
def record_return_ride_request(
    phone: str, sunday_date: str, raw_text: str, capacity: int
) -> dict:
    """Record a return ride request, or update one already on file.

    A first request from a phone number for a given date is assigned
    the next position in that day's count, and gets `needs_driver` set
    based on whether that position is past `capacity`. A second request
    from the same phone on the same date (a corrected address, a
    resend) overwrites the stored text but keeps the position and
    `needs_driver` value assigned the first time around - it does not
    consume another slot.

    Args:
        phone: The requester's phone number, E.164 preferred.
        sunday_date: The date the request was made, in ISO "YYYY-MM-DD"
            form. This is the actual calendar date the text arrived on,
            not necessarily a Sunday.
        raw_text: Everything the rider typed after "RIDE ", stored as
            is rather than parsed into separate name/address fields.
        capacity: The shuttle seat capacity for the return trip
            (settings.RETURN_SHUTTLE_CAPACITY). Passed in rather than
            imported directly here so this stays testable without a
            dependency on settings.

    Returns:
        dict: {"position": int, "needs_driver": bool, "is_new": bool}
            reflecting this request's place in the day's count.

    Raises:
        RuntimeError: If the transaction fails.
    """
    try:
        client = get_client()
        doc_ref = client.collection(RETURN_RIDE_REQUESTS_COLLECTION).document(
            f"{sunday_date}_{phone}"
        )
        counter_ref = client.collection(RETURN_RIDE_COUNTS_COLLECTION).document(
            sunday_date
        )

        transaction = client.transaction()

        @firestore.transactional
        def _run(transaction: firestore.Transaction) -> dict:
            return _apply_return_ride_request(
                transaction, doc_ref, counter_ref, phone, sunday_date, raw_text, capacity
            )

        return _run(transaction)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to record return ride request for phone={phone!r}, "
            f"sunday_date={sunday_date!r}: {exc}"
        ) from exc


def _apply_return_ride_request(
    transaction: Any,
    doc_ref: Any,
    counter_ref: Any,
    phone: str,
    sunday_date: str,
    raw_text: str,
    capacity: int,
) -> dict:
    """The actual read-then-write logic for record_return_ride_request().

    Kept as a plain function, separate from the `@firestore.transactional`
    wrapper above, so it can be unit tested against simple fake
    `transaction`/`doc_ref`/`counter_ref` objects (see
    tests/test_return_ride.py) without needing a live Firestore
    transaction, which google-cloud-firestore's decorator can't fake
    convincingly on its own.

    All reads happen before any write, as Firestore transactions
    require: first the request doc itself (is this phone already on
    file for this date?), then the counter doc if it turns out to be a
    new request.
    """
    existing = doc_ref.get(transaction=transaction)

    if existing.exists:
        data = existing.to_dict() or {}
        transaction.update(
            doc_ref,
            {"raw_text": raw_text, "updated_at": firestore.SERVER_TIMESTAMP},
        )
        return {
            "position": data.get("position", 0),
            "needs_driver": bool(data.get("needs_driver", False)),
            "is_new": False,
        }

    counter_doc = counter_ref.get(transaction=transaction)
    current_count = (
        (counter_doc.to_dict() or {}).get("count", 0) if counter_doc.exists else 0
    )
    position = current_count + 1
    needs_driver = position > capacity

    transaction.set(counter_ref, {"count": position}, merge=True)
    transaction.set(
        doc_ref,
        {
            "phone": phone,
            "date": sunday_date,
            "raw_text": raw_text,
            "position": position,
            "needs_driver": needs_driver,
            "created_at": firestore.SERVER_TIMESTAMP,
        },
    )
    return {"position": position, "needs_driver": needs_driver, "is_new": True}


def get_return_ride_requests_for_date(sunday_date: str) -> list[dict]:
    """Return every return ride request logged for a given date.

    Args:
        sunday_date: The date to fetch requests for, in ISO
            "YYYY-MM-DD" form.

    Returns:
        list[dict]: Request documents (including their Firestore doc
            "id"), sorted by "position" ascending. Sorted in Python
            rather than with a Firestore order_by, so this doesn't need
            a composite index on top of the equality filter. Empty list
            if none are found.

    Raises:
        RuntimeError: If the query fails.
    """
    try:
        client = get_client()
        query = client.collection(RETURN_RIDE_REQUESTS_COLLECTION).where(
            "date", "==", sunday_date
        )
        requests = [_doc_to_dict(doc) for doc in query.stream()]
        requests.sort(key=lambda req: req.get("position", 0))
        return requests
    except Exception as exc:
        raise RuntimeError(
            f"Failed to get return ride requests for "
            f"sunday_date={sunday_date!r}: {exc}"
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


# --------------------------------------------------------------------------
# Ride cancellations (the SKIP keyword)
# --------------------------------------------------------------------------
# A rider who texts SKIP after the Saturday night reminder is recorded
# here, keyed by the Sunday they cancelled and their phone number, the
# same <date>_<phone> shape return_ride_requests uses.
#
# This is the authoritative record even once the sheet write lands. The
# sheet is owned by someone outside this system and a write there can
# fail for reasons we don't control, so a cancellation has to be durable
# here first and reflected in the sheet second.


def record_ride_cancellation(
    phone: str, sunday_date: str, name: str = "", raw_text: str = ""
) -> dict:
    """Record that a rider cancelled their ride for a given Sunday.

    Idempotent. A rider who texts SKIP twice is not an error and does
    not produce a second record; the original cancellation time is kept,
    since that's the moment the seat actually came free.

    Args:
        phone: The rider's phone number, E.164 preferred.
        sunday_date: The Sunday being cancelled, ISO "YYYY-MM-DD".
        name: The rider's name from the signup sheet, stored so an admin
            reading the collection doesn't have to resolve a phone
            number by hand.
        raw_text: What the rider actually texted, kept because SKIP has
            unadvertised aliases and it's useful to know which word
            people really use.

    Returns:
        dict: {"is_new": bool} - False when this phone had already
            cancelled this Sunday.

    Raises:
        RuntimeError: If the write fails.
    """
    doc_id = f"{sunday_date}_{phone}"
    try:
        client = get_client()
        doc_ref = client.collection(RIDE_CANCELLATIONS_COLLECTION).document(doc_id)
        existing = doc_ref.get()
        if existing.exists:
            return {"is_new": False}

        doc_ref.set(
            {
                "phone": phone,
                "date": sunday_date,
                "name": name,
                "raw_text": raw_text,
                "sheet_updated": False,
                "created_at": firestore.SERVER_TIMESTAMP,
            }
        )
        return {"is_new": True}
    except Exception as exc:
        raise RuntimeError(
            f"Failed to record cancellation for phone={phone!r} "
            f"date={sunday_date!r}: {exc}"
        ) from exc


def get_cancelled_phones_for_sunday(sunday_date: str) -> set:
    """Return the set of phone numbers that cancelled a given Sunday.

    A set rather than a list because the only thing any caller does with
    it is membership tests while filtering a rider list.

    Args:
        sunday_date: The Sunday to look up, ISO "YYYY-MM-DD".

    Returns:
        set[str]: Phone numbers exactly as they were recorded.

    Raises:
        RuntimeError: If the query fails. Callers filtering a rider list
            should treat a failure as "nobody cancelled" and carry on -
            showing a cancelled rider is a far smaller problem than
            every ride count in the system failing at once.
    """
    try:
        client = get_client()
        docs = (
            client.collection(RIDE_CANCELLATIONS_COLLECTION)
            .where("date", "==", sunday_date)
            .stream()
        )
        return {doc.to_dict().get("phone", "") for doc in docs} - {""}
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read cancellations for date={sunday_date!r}: {exc}"
        ) from exc


def clear_return_ride_request(phone: str, sunday_date: str) -> bool:
    """Remove one phone's return ride request for a Sunday.

    Exists because admins can use RIDE outside Sunday to test it, which
    puts a real row on a real service's list where it counts against the
    28 seats. A test that quietly inflates Sunday's headcount is worse
    than no test at all.

    The counter is deliberately NOT decremented. Positions are handed
    out once and never reissued, so rolling the counter back would give
    a later rider a position somebody already holds, which is exactly
    the collision the transaction exists to prevent. The count reported
    by REQUESTS comes from the stored requests themselves, not from the
    counter, so removing the row is enough to correct it.

    Args:
        phone: The requester's number, E.164 preferred.
        sunday_date: The service date, ISO "YYYY-MM-DD".

    Returns:
        bool: True if a request was removed, False if there was none.

    Raises:
        RuntimeError: If the delete fails.
    """
    doc_id = f"{sunday_date}_{phone}"
    try:
        client = get_client()
        doc_ref = client.collection(RETURN_RIDE_REQUESTS_COLLECTION).document(doc_id)
        if not doc_ref.get().exists:
            return False
        doc_ref.delete()
        return True
    except Exception as exc:
        raise RuntimeError(
            f"Failed to clear return ride request for phone={phone!r} "
            f"date={sunday_date!r}: {exc}"
        ) from exc
