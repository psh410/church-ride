# Flask web app that Cloud Run runs for the church ride coordination
# system. Cloud Scheduler hits one HTTP endpoint per recurring email
# job (Monday schedule, Wednesday reminder, Saturday update, Saturday
# driver assignment) instead of each job running as a separate Cloud
# Function - this keeps all of them behind one deployed service.

import hmac

from flask import Flask, jsonify, redirect
from dotenv import load_dotenv

load_dotenv()

import logging

logging.basicConfig(level=logging.INFO)

from functions.send_weekly_emails import (
    send_wednesday_reminder,
    send_saturday_update,
    send_saturday_driver_assignment,
)
from functions.send_semester_schedule import send_monday_schedule
from functions.read_riders_sheet import get_next_sunday_date

logger = logging.getLogger(__name__)

app = Flask(__name__)

# The canonical, carrier-facing compliance pages. These live on the
# church website, not here, and they are what the approved A2P 10DLC
# campaign registration points at. The /sms-terms and /privacy-policy
# routes below redirect to them.
SMS_TERMS_URL = "https://www.cfchome.org/sms-terms"
PRIVACY_POLICY_URL = "https://www.cfchome.org/privacy-policy"


@app.route("/health", methods=["GET"])
def health() -> tuple:
    """Simple health check endpoint for Cloud Run's readiness/liveness probes.

    Returns:
        tuple: ({"status": "ok"}, 200).
    """
    return jsonify({"status": "ok"}), 200


@app.route("/sms-terms", methods=["GET"])
def sms_terms():
    """Redirect to the live SMS terms page on the church website.

    This used to serve its own copy of the terms. That copy went stale:
    it still told riders they opt in "by providing your mobile number"
    and linked the retired Google Form, while the real flow is an
    optional, unchecked-by-default consent checkbox on
    cfchome.org/Ride-Sign-Up, and the approved A2P campaign points at
    the cfchome.org pages.

    Redirecting rather than deleting on purpose. Inconsistent opt-in
    URLs across the campaign fields and the terms pages were the root
    cause of three campaign rejections, so any stale link still floating
    around should land on the correct page rather than a 404.
    """
    return redirect(SMS_TERMS_URL, code=301)


@app.route("/privacy-policy", methods=["GET"])
def privacy_policy():
    """Redirect to the live privacy policy on the church website.

    See sms_terms() above for why this redirects instead of 404ing.
    """
    return redirect(PRIVACY_POLICY_URL, code=301)


@app.route("/debug-settings", methods=["GET"])
def debug_settings():
    """Temporary debug endpoint to check which settings loaded."""
    from config import settings
    return jsonify({
        "GOOGLE_CLOUD_PROJECT": settings.GOOGLE_CLOUD_PROJECT,
        "RIDER_SHEET_ID": settings.RIDER_SHEET_ID,
        "SHEETS_ID": settings.SHEETS_ID,
        "ADMIN_EMAIL": settings.ADMIN_EMAIL,
        # Count only, never the numbers themselves - this endpoint is
        # public. 0 here means ADMIN_SMS_PHONES didn't load, which is
        # the silent failure that makes the UPDATE keyword ignore
        # everyone (see functions/send_admin_summary.py).
        "ADMIN_SMS_PHONES_count": len(settings.ADMIN_SMS_PHONES),
    })


# ============================================
# TEST ENDPOINTS - safe to call anytime, only
# emails peterhahn410@gmail.com
# ============================================


@app.route("/test-saturday-update", methods=["POST"])
def test_saturday_update() -> tuple:
    """Test endpoint - sends the REAL Saturday update, admin only.

    Doesn't build its own summary - instead calls the real
    send_saturday_update() (same email-building logic as production)
    but temporarily monkey-patches settings.OVERSEER_DRIVER_EMAIL,
    settings.OVERSEER_RIDE_EMAIL, settings.OVERSEER_RIDE_EMAIL_2, and
    settings.BCC_EMAIL to peterhahn410@gmail.com for the duration of
    this one request, so the To/Cc/Bcc all resolve to admin only. The
    originals are always restored in a finally block, even if the call
    raises, so production settings are never left overridden.

    Returns:
        tuple: (result dict plus a "note" key, 200) on success, or
            ({"status": "error", "error": str}, 500) on failure.
    """
    from config import settings

    real_driver_email = settings.OVERSEER_DRIVER_EMAIL
    real_ride_email = settings.OVERSEER_RIDE_EMAIL
    real_ride_email_2 = settings.OVERSEER_RIDE_EMAIL_2
    real_bcc = settings.BCC_EMAIL

    try:
        settings.OVERSEER_DRIVER_EMAIL = "peterhahn410@gmail.com"
        settings.OVERSEER_RIDE_EMAIL = "peterhahn410@gmail.com"
        settings.OVERSEER_RIDE_EMAIL_2 = "peterhahn410@gmail.com"
        settings.BCC_EMAIL = "peterhahn410@gmail.com"

        sunday = get_next_sunday_date()
        result = send_saturday_update(sunday)

        return jsonify(
            {**result, "note": "test only - full content, sent to admin only"}
        ), 200
    except Exception as exc:
        logger.error("Test saturday update failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500
    finally:
        settings.OVERSEER_DRIVER_EMAIL = real_driver_email
        settings.OVERSEER_RIDE_EMAIL = real_ride_email
        settings.OVERSEER_RIDE_EMAIL_2 = real_ride_email_2
        settings.BCC_EMAIL = real_bcc


@app.route("/test-wednesday-reminder", methods=["POST"])
def test_wednesday_reminder() -> tuple:
    """Test endpoint - sends the REAL Wednesday reminder body, admin only.

    Doesn't call send_wednesday_reminder() directly (that would email
    the real drivers) - instead reuses its exact body-building logic
    (_build_wednesday_reminder_body() and friends) so the test email's
    content matches production exactly, but overrides the recipient to
    peterhahn410@gmail.com only.

    Returns:
        tuple: ({"status": "sent" or "failed", "note": str}, 200) on
            success, or ({"status": "error", "error": str}, 500) on
            failure.
    """
    try:
        from db.firestore_client import get_semester_schedule
        from functions.read_sheets import get_routes
        from functions.send_email import send_email
        from functions.send_weekly_emails import (
            _build_assignments_from_schedule,
            _build_wednesday_reminder_body,
            _find_schedule_entry,
        )

        sunday = get_next_sunday_date()
        schedule = get_semester_schedule()
        entry = _find_schedule_entry(schedule, sunday)

        if entry is None:
            body = f"TEST EMAIL - Wednesday Reminder\nSunday: {sunday}\nNo schedule entry found for this date."
        else:
            assignments = _build_assignments_from_schedule(entry)
            routes = get_routes()
            backup = entry.get("backup")
            body = _build_wednesday_reminder_body(sunday, assignments, routes, backup)

        result = send_email(
            to="peterhahn410@gmail.com",
            subject="TEST - Full Wednesday Reminder",
            body=body,
        )
        return jsonify(
            {
                "status": "sent" if result else "failed",
                "note": "test only - full content, sent to admin only",
            }
        ), 200
    except Exception as exc:
        logger.error("Test wednesday reminder failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/test-saturday-driver-assignment", methods=["POST"])
def test_saturday_driver_assignment() -> tuple:
    """Test endpoint - sends a final rider-list summary only to admin.

    Doesn't call send_saturday_driver_assignment() directly (that would
    email the real drivers) - instead builds a plain-text summary of
    that week's driver assignments and rider counts, and emails it to
    peterhahn410@gmail.com only.

    Returns:
        tuple: ({"status": "sent" or "failed", "note": str}, 200) on
            success, or ({"status": "error", "error": str}, 500) on
            failure.
    """
    try:
        from db.firestore_client import get_semester_schedule
        from functions.read_riders_sheet import get_all_riders_for_sunday
        from functions.send_email import send_email

        sunday = get_next_sunday_date()
        schedule = get_semester_schedule()
        entry = next((e for e in schedule if e.get("date") == sunday), None)
        data = get_all_riders_for_sunday(sunday)

        if entry is None:
            drivers_summary = "No schedule entry found for this date."
        else:
            drivers_summary = (
                f"Shuttle 1: {entry.get('shuttle_1')}\n"
                f"Shuttle 2: {entry.get('shuttle_2')}\n"
                f"Backup: {entry.get('backup') or 'None'}"
            )

        body = (
            f"TEST EMAIL - Saturday Driver Assignment\n"
            f"Sunday: {sunday}\n"
            f"Total riders: {data['total']}\n"
            f"Shuttle: {data['shuttle_total']}\n"
            f"Non-shuttle: {data['non_shuttle_total']}\n\n"
            f"{drivers_summary}"
        )

        result = send_email(
            to="peterhahn410@gmail.com",
            subject="TEST - Cloud App Saturday Driver Assignment",
            body=body,
        )
        return jsonify(
            {
                "status": "sent" if result else "failed",
                "note": "test only - not sent to real drivers",
            }
        ), 200
    except Exception as exc:
        logger.error("Test saturday driver assignment failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/test-monday-schedule", methods=["POST"])
def test_monday_schedule() -> tuple:
    """Test endpoint - sends a semester schedule summary only to admin.

    Doesn't call send_monday_schedule() directly (that would email the
    real overseer, Dae) - instead builds a plain-text summary of the
    next few upcoming Sundays' driver assignments and emails it to
    peterhahn410@gmail.com only.

    Returns:
        tuple: ({"status": "sent" or "failed", "note": str}, 200) on
            success, or ({"status": "error", "error": str}, 500) on
            failure.
    """
    try:
        from db.firestore_client import get_semester_schedule
        from functions.send_email import send_email

        schedule = get_semester_schedule()
        upcoming = [entry for entry in schedule if not entry.get("past")]

        lines = [
            "TEST EMAIL - Semester Schedule Summary",
            f"Total upcoming Sundays: {len(upcoming)}",
            "",
        ]
        for entry in upcoming[:3]:
            lines.append(
                f"{entry.get('date')}: Shuttle 1 ({entry.get('shuttle_1')}), "
                f"Shuttle 2 ({entry.get('shuttle_2')}), "
                f"Backup ({entry.get('backup') or 'None'})"
            )
        body = "\n".join(lines)

        result = send_email(
            to="peterhahn410@gmail.com",
            subject="TEST - Cloud App Monday Schedule",
            body=body,
        )
        return jsonify(
            {
                "status": "sent" if result else "failed",
                "note": "test only - not sent to Dae",
            }
        ), 200
    except Exception as exc:
        logger.error("Test monday schedule failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500


# ============================================
# PRODUCTION ENDPOINTS - sends real emails to
# drivers/overseers, only call these when ready
# ============================================


def _scheduled_job_response(result: dict) -> tuple:
    """Turn a job's result dict into a response Cloud Scheduler can act on.

    These endpoints used to return 200 no matter what the job reported.
    That meant a transient failure - a cold-start credentials blip, a
    Sheets hiccup - was reported to Cloud Scheduler as success and never
    retried, even though every job already has retry configured. On
    2026-09-12 the Thursday prayer reminder failed exactly that way
    (metadata server not ready on a cold container) and nobody found out
    until the logs were read by hand two days later. Morning Prayer ran
    36 seconds later on the warm container and was fine.

    Returning 500 on a total failure switches that retry back on.

    Deliberately narrow about what counts as failure, because a retry
    re-runs the whole job and would re-send to anyone already contacted:

    - {"status": "failed"} → 500. Nothing went out, retrying is safe.
    - {"sent_count": 0} with failures listed → 500. Tried and failed.
    - {"sent_count": 0} with no failures → 200. Nothing to do: no
      schedule entry or no drivers assigned. Retrying can't help, and a
      break week would retry forever.
    - "skipped" → 200. A legitimate no-meeting week, not a failure.
    - Anything that sent something → 200, even with partial failures,
      since a retry would duplicate what already went out.

    Args:
        result: The dict returned by the job function.

    Returns:
        tuple: (JSON response, status code) for the route to return.
    """
    status = result.get("status")
    failed = status in {"failed", "error"} or (
        result.get("sent_count") == 0 and result.get("failures")
    )

    if failed:
        logger.error(
            "Scheduled job reported failure; returning 500 so Cloud Scheduler "
            "retries instead of recording a false success: %s",
            result,
        )
        return jsonify(result), 500

    return jsonify(result), 200


@app.route("/send-monday-schedule", methods=["POST"])
def route_send_monday_schedule() -> tuple:
    """Trigger the Monday semester-schedule email to the overseer.

    Returns:
        tuple: (result dict, 200) on success, or
            ({"status": "error", "error": str}, 500) on failure.
    """
    try:
        result = send_monday_schedule()
        return _scheduled_job_response(result)
    except Exception as exc:
        logger.error("send_monday_schedule failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/send-wednesday-reminder", methods=["POST"])
def route_send_wednesday_reminder() -> tuple:
    """Trigger the Wednesday driver reminder email for next Sunday.

    Returns:
        tuple: (result dict, 200) on success, or
            ({"status": "error", "error": str}, 500) on failure.
    """
    try:
        sunday = get_next_sunday_date()
        result = send_wednesday_reminder(sunday)
        return _scheduled_job_response(result)
    except Exception as exc:
        logger.error("send_wednesday_reminder failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/send-saturday-update", methods=["POST"])
def route_send_saturday_update() -> tuple:
    """Trigger the Saturday rider-count status update email for next Sunday.

    Returns:
        tuple: (result dict, 200) on success, or
            ({"status": "error", "error": str}, 500) on failure.
    """
    try:
        sunday = get_next_sunday_date()
        result = send_saturday_update(sunday)
        return _scheduled_job_response(result)
    except Exception as exc:
        logger.error("send_saturday_update failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/send-saturday-driver-assignment", methods=["POST"])
def route_send_saturday_driver_assignment() -> tuple:
    """Trigger the Saturday final rider-list email to drivers for next Sunday.

    Returns:
        tuple: (result dict, 200) on success, or
            ({"status": "error", "error": str}, 500) on failure.
    """
    try:
        sunday = get_next_sunday_date()
        result = send_saturday_driver_assignment(sunday)
        return _scheduled_job_response(result)
    except Exception as exc:
        logger.error("send_saturday_driver_assignment failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/send-driver-sms-reminder", methods=["POST"])
def send_driver_sms_reminder_route():
    """Send SMS reminders to this Sunday's shuttle drivers
    and backup."""
    try:
        from functions.send_driver_sms_reminder import send_driver_sms_reminders
        result = send_driver_sms_reminders()
        return _scheduled_job_response(result)
    except Exception as exc:
        logger.error("Driver SMS reminder failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/send-thursday-prayer-reminder", methods=["POST"])
def send_thursday_prayer_reminder_route():
    """Send the Thursday night prayer meeting reminder to
    this week's speaker and worship leader."""
    try:
        from functions.prayer.thursday import send_thursday_reminder
        result = send_thursday_reminder()
        return _scheduled_job_response(result)
    except Exception as exc:
        logger.error("Thursday prayer reminder failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/send-morning-prayer-reminder", methods=["POST"])
def send_morning_prayer_reminder_route():
    """Send the Morning Prayer weekly reminder combining
    devotional and worship schedules."""
    try:
        from functions.prayer.morning import send_morning_reminder
        result = send_morning_reminder()
        return _scheduled_job_response(result)
    except Exception as exc:
        logger.error("Morning prayer reminder failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/preview-admin-summary", methods=["GET"])
def preview_admin_summary():
    """Return the admin ride summary as plain text without texting anyone.

    Read-only and safe to hit any time - it's the same text an admin
    would get by replying UPDATE, so the wording and counts can be
    checked with curl before (or instead of) sending a real SMS.

    Optional query arg:
        sunday: an ISO "YYYY-MM-DD" Sunday to summarize instead of the
            upcoming one, e.g. /preview-admin-summary?sunday=2026-09-13.
    """
    try:
        from flask import request

        from functions.send_admin_summary import build_admin_summary

        sunday_date = request.args.get("sunday") or None
        summary = build_admin_summary(sunday_date)
        return summary, 200, {"Content-Type": "text/plain; charset=utf-8"}
    except Exception as exc:
        logger.error("Admin summary preview failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/confirm-rider-signup", methods=["POST"])
def confirm_rider_signup():
    """Send the signup confirmation text for one row of the rider sheet.

    Called by the Apps Script attached to the signup form, right after it
    has written and flagged the row. Expects JSON:

        {"row": 42, "secret": "..."}

    Deliberately takes a row number and not a phone number. A public
    endpoint that texts whatever number it's handed is a spam relay
    billed to this Twilio account and sent under this brand's A2P
    registration, so the row is read from the sheet and the phone comes
    from there. The worst an attacker with the secret can do is re-send
    a confirmation to someone who genuinely signed up, and the
    per-(phone, Sunday) record in Firestore stops even that.

    Returns 403 on a bad or missing secret, and 200 with the outcome
    otherwise, including when nothing was sent (no consent, duplicate,
    already confirmed). Those aren't failures, so they shouldn't make
    the Apps Script retry.
    """
    try:
        from flask import request

        from config import settings

        payload = request.get_json(silent=True) or {}
        secret = payload.get("secret", "")

        if not settings.RIDER_CONFIRMATION_SECRET:
            logger.error(
                "RIDER_CONFIRMATION_SECRET is not configured; refusing the request."
            )
            return jsonify({"status": "error", "error": "not configured"}), 403

        if not hmac.compare_digest(str(secret), str(settings.RIDER_CONFIRMATION_SECRET)):
            logger.warning("Rejected /confirm-rider-signup: bad secret.")
            return jsonify({"status": "error", "error": "forbidden"}), 403

        try:
            row = int(payload.get("row", 0))
        except (TypeError, ValueError):
            row = 0
        if row < 2:
            return jsonify({"status": "error", "error": "invalid row"}), 400

        from functions.send_rider_confirmation import confirm_signup

        result = confirm_signup(row)
        return jsonify(result), 200
    except Exception as exc:
        logger.error("Rider signup confirmation failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/sms-webhook", methods=["POST"])
def sms_webhook():
    """Handle incoming SMS from Twilio: opt-out, opt-in, and keywords.

    These kinds of inbound message matter here:

    - Opt-out keywords (STOP/STOPALL/UNSUBSCRIBE/CANCEL/END/QUIT).
      Twilio already blocks carrier-level delivery itself; we
      additionally record the phone in Firestore so our own sends
      (functions/send_sms.py) skip it, and email the admin when the
      number belongs to a DRIVER, since that needs a human to arrange
      shift coverage. A rider opting out just stops their own ride
      texts, so it's recorded silently.
    - Opt-in keywords (START/YES). Recorded so a driver who comes back
      starts receiving reminders again.
    - Admin summary keywords (UPDATE/STATUS). Replies inline with the
      current ride counts, but only to numbers on the
      settings.ADMIN_SMS_PHONES allowlist - anyone else gets no reply
      at all, so the keyword isn't discoverable by outsiders.
    - Admin reset keyword (RESETME). Clears the texting admin's own
      ride confirmation for the coming Sunday so a test signup can be
      run again. Same allowlist as UPDATE, and it can only ever touch
      the number it was sent from.
    - Driver lookup keywords (ROUTE/LIST/SCHEDULE). Replies with that
      driver's stops and live rider counts, their rider names, or the
      semester schedule, for the upcoming Sunday. Authorized off the
      driver roster rather than an allowlist, and only for a driver
      actually assigned that week. LIST was called RIDERS until the
      RIDE keyword below made that prefix ambiguous.
    - Return ride signup (RIDE <name> <dorm/address>). The one keyword
      here open to anyone: no allowlist and no roster check, because
      this is how riders say, in the minutes after service, that they
      want a ride home. Always replies, so a rider never gets silence.
    - Return ride count (REQUESTS). Replies with the live return
      headcount and any names past shuttle capacity, so Dae and Sarah
      can see whether personal drivers are needed. Same allowlist as
      UPDATE.

    Always returns 200 with TwiML (empty unless we're replying) so
    Twilio doesn't retry.
    """
    try:
        from flask import request

        # Twilio sends form-encoded data, not JSON
        from_number = request.form.get("From", "unknown")
        body = request.form.get("Body", "").strip().upper()

        opt_out_keywords = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
        opt_in_keywords = {"START", "YES"}

        from functions.send_sms import normalize_to_e164

        try:
            normalized = normalize_to_e164(from_number)
        except ValueError:
            normalized = from_number

        if body in opt_out_keywords:
            # Only the opt-out branch needs the driver roster (to name
            # them in the alert email), so the Sheets read stays out of
            # every other inbound message's path.
            driver = None
            try:
                from functions.read_sheets import find_driver_by_phone
                driver = find_driver_by_phone(normalized)
            except Exception:
                logger.warning(
                    "Could not check driver roster for %s.",
                    normalized,
                    exc_info=True,
                )

            from db.firestore_client import record_sms_opt_out
            record_sms_opt_out(normalized, body)

            logger.warning(
                "SMS opt-out received from %s (keyword: %s)%s",
                normalized,
                body,
                f" - matches driver {driver['name']}" if driver else "",
            )

            if driver:
                from functions.send_email import send_email
                from config import settings

                send_email(
                    to=settings.BCC_EMAIL,
                    subject=f"SMS Opt-Out: driver {driver['name']}",
                    body=(
                        f"Driver {driver['name']} ({normalized}) has opted out "
                        f"of ride texts (replied {body}).\n\n"
                        "Nothing to fix on the schedule. They're still driving "
                        "and still on the Wednesday reminder and Sunday final "
                        "rider list emails, which send separately. The only "
                        "thing they lose is the Friday text.\n\n"
                        "If they ever can't drive a shift, they'll contact Dae "
                        "directly as usual."
                    ),
                )
            # The email above is deliberately low-key: a driver texting
            # STOP means "don't text me," not "I'm backing out." Drivers
            # are committed and contact Dae directly if they can't make
            # a shift, and they keep getting the Wednesday and Sunday
            # driver emails either way (see send_weekly_emails.py, which
            # mails the assigned drivers directly). So don't reword this
            # into "find a replacement" - that sends Dae chasing a
            # problem that doesn't exist.
            #
            # A rider (or any unrecognized number) opting out needs no
            # admin email at all - it's already recorded above, and it
            # just means that number stops getting ride-status texts.

        elif body in opt_in_keywords:
            from db.firestore_client import record_sms_opt_in
            record_sms_opt_in(normalized, body)

            logger.info(
                "SMS opt-in received from %s (keyword: %s).", normalized, body
            )

        else:
            from functions.send_admin_summary import (
                ADMIN_RESET_KEYWORDS,
                ADMIN_SUMMARY_KEYWORDS,
                build_admin_reply,
                build_reset_reply,
                is_admin_phone,
            )

            from functions.driver_sms_lookup import (
                DRIVER_LOOKUP_KEYWORDS,
                build_driver_lookup_reply,
            )

            from functions.return_ride import (
                REQUESTS_KEYWORDS,
                build_requests_reply,
                build_ride_reply,
                matches_ride_keyword,
            )

            if body in DRIVER_LOOKUP_KEYWORDS:
                # Authorization comes from the driver roster itself: a
                # number that isn't a driver gets nothing, and a driver
                # not assigned this Sunday is told so rather than given
                # someone else's route.
                try:
                    reply = build_driver_lookup_reply(normalized, body)
                except Exception as exc:
                    logger.error("Could not build %s reply: %s", body, exc)
                    reply = (
                        "CFC Rides: couldn't pull your route just now. "
                        "Please try again in a minute."
                    )

                if reply is None:
                    logger.warning(
                        "Ignoring %s keyword from %s: not a known driver.",
                        body,
                        normalized,
                    )
                else:
                    from xml.sax.saxutils import escape

                    logger.info("Replied to %s with %s details.", normalized, body)
                    return (
                        f"<Response><Message>{escape(reply)}</Message></Response>",
                        200,
                        {"Content-Type": "text/xml"},
                    )

            elif body in ADMIN_SUMMARY_KEYWORDS:
                if not is_admin_phone(normalized):
                    # Silence, not an error message - no reason to tell
                    # an unknown number that this keyword exists.
                    logger.warning(
                        "Ignoring %s keyword from non-admin number %s.",
                        body,
                        normalized,
                    )
                else:
                    try:
                        summary = build_admin_reply(normalized)
                        logger.info(
                            "Replied with ride summary to admin %s.", normalized
                        )
                    except Exception as exc:
                        # Reply anyway: an admin who texted and got
                        # nothing back can't tell the difference between
                        # a broken feature and a slow one.
                        logger.error("Could not build admin summary: %s", exc)
                        summary = (
                            "CFC Rides: couldn't pull the ride counts just now. "
                            "Please try again in a minute."
                        )

                    from xml.sax.saxutils import escape

                    return (
                        f"<Response><Message>{escape(summary)}</Message></Response>",
                        200,
                        {"Content-Type": "text/xml"},
                    )

            elif body in ADMIN_RESET_KEYWORDS:
                if not is_admin_phone(normalized):
                    # Same silence as an unauthorized UPDATE: no reason
                    # to tell an unknown number that the keyword exists.
                    logger.warning(
                        "Ignoring %s keyword from non-admin number %s.",
                        body,
                        normalized,
                    )
                else:
                    reply = build_reset_reply(normalized)
                    logger.info("Handled %s for admin %s.", body, normalized)

                    from xml.sax.saxutils import escape

                    return (
                        f"<Response><Message>{escape(reply)}</Message></Response>",
                        200,
                        {"Content-Type": "text/xml"},
                    )

            elif matches_ride_keyword(body):
                # Open to anyone - no allowlist, no roster check. Matched
                # as a whole word (see matches_ride_keyword) so a future
                # keyword sharing this prefix isn't swallowed here.
                try:
                    reply = build_ride_reply(normalized, body)
                except Exception as exc:
                    logger.error(
                        "Could not handle RIDE from %s: %s", normalized, exc
                    )
                    # Unlike the other keywords, never fall through to
                    # silence: the rider is standing in the lobby
                    # waiting to hear that it worked.
                    reply = (
                        "CFC Rides: couldn't save that just now. Please tell "
                        "an usher you need a ride home."
                    )

                from xml.sax.saxutils import escape

                logger.info("Handled RIDE request from %s.", normalized)
                return (
                    f"<Response><Message>{escape(reply)}</Message></Response>",
                    200,
                    {"Content-Type": "text/xml"},
                )

            elif body in REQUESTS_KEYWORDS:
                if not is_admin_phone(normalized):
                    # Same silence as an unauthorized UPDATE.
                    logger.warning(
                        "Ignoring %s keyword from non-admin number %s.",
                        body,
                        normalized,
                    )
                else:
                    try:
                        summary = build_requests_reply(normalized)
                        logger.info(
                            "Replied with return ride count to admin %s.",
                            normalized,
                        )
                    except Exception as exc:
                        logger.error("Could not build REQUESTS reply: %s", exc)
                        summary = (
                            "CFC Rides: couldn't pull the return counts just "
                            "now. Please try again in a minute."
                        )

                    from xml.sax.saxutils import escape

                    return (
                        f"<Response><Message>{escape(summary)}</Message></Response>",
                        200,
                        {"Content-Type": "text/xml"},
                    )

        # Twilio expects a TwiML response (empty means "no reply")
        return "<Response></Response>", 200, {"Content-Type": "text/xml"}
    except Exception as exc:
        logger.error("SMS webhook error: %s", exc)
        # Still return 200 so Twilio doesn't retry endlessly
        return "<Response></Response>", 200, {"Content-Type": "text/xml"}


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
