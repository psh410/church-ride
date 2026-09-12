# Flask web app that Cloud Run runs for the church ride coordination
# system. Cloud Scheduler hits one HTTP endpoint per recurring email
# job (Monday schedule, Wednesday reminder, Saturday update, Saturday
# driver assignment) instead of each job running as a separate Cloud
# Function - this keeps all of them behind one deployed service.

from flask import Flask, jsonify
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


@app.route("/health", methods=["GET"])
def health() -> tuple:
    """Simple health check endpoint for Cloud Run's readiness/liveness probes.

    Returns:
        tuple: ({"status": "ok"}, 200).
    """
    return jsonify({"status": "ok"}), 200


@app.route("/sms-terms", methods=["GET"])
def sms_terms():
    """Serve the SMS program terms and privacy policy page,
    required for A2P 10DLC campaign compliance."""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>CFC Ride Coordination - SMS Terms & Privacy Policy</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: -apple-system, sans-serif; 
                   max-width: 700px; margin: 40px auto; 
                   padding: 0 20px; line-height: 1.6; color: #333; }
            h1 { font-size: 24px; }
            h2 { font-size: 18px; margin-top: 30px; }
            p { margin-bottom: 16px; }
        </style>
    </head>
    <body>
        <h1>Covenant Fellowship Church (CFC) - SMS Program Terms</h1>

        <p>Last updated: September 2026</p>

        <h2>Program Description</h2>
        <p>By opting in, you agree to receive SMS ride notifications 
        and reminders from Covenant Fellowship Church (CFC), 
        including pickup confirmations, ride cancellations, driver 
        assignment reminders, and pickup/dropoff status updates.</p>

        <h2>Who Receives Messages</h2>
        <p>Messages are sent only to people who have voluntarily 
        provided their mobile number and opted in through the CFC 
        ride signup form or driver availability form.</p>

        <h2>Opt-In</h2>
        <p>You opt in by providing your mobile number on the CFC 
        ride signup or driver form at 
        <a href="https://forms.gle/hszPoGWTaLr4t3U69">
        https://forms.gle/hszPoGWTaLr4t3U69</a> and agreeing to 
        receive SMS updates related to church rides.</p>

        <h2>Message Frequency</h2>
        <p>Message frequency varies, but you may receive up to 3 
        messages per week during active shuttle service periods. 
        Message and data rates may apply.</p>

        <h2>Opt-Out</h2>
        <p>You can opt out at any time by replying STOP to any 
        message. You will receive a confirmation and will no longer 
        receive messages from this program.</p>

        <h2>Help</h2>
        <p>For help, reply HELP to any message, or contact us at 
        <a href="mailto:team@cfchome.org">team@cfchome.org</a>.</p>

        <h2>Privacy</h2>
        <p>See our 
        <a href="/privacy-policy">Privacy Policy</a> 
        for information on how we handle and protect your data.</p>

        <h2>Contact Us</h2>
        <p>Covenant Fellowship Church<br>
        2906 Crossing Ct, Champaign, IL<br>
        Email: <a href="mailto:team@cfchome.org">team@cfchome.org</a></p>
    </body>
    </html>
    """
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/privacy-policy", methods=["GET"])
def privacy_policy():
    """Serve the dedicated Privacy Policy page, separate
    from SMS terms, for A2P 10DLC campaign compliance
    (reviewers require two distinct URLs)."""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Covenant Fellowship Church - Privacy Policy</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: -apple-system, sans-serif; 
                   max-width: 700px; margin: 40px auto; 
                   padding: 0 20px; line-height: 1.6; color: #333; }
            h1 { font-size: 24px; }
            h2 { font-size: 18px; margin-top: 30px; }
            p { margin-bottom: 16px; }
        </style>
    </head>
    <body>
        <h1>Covenant Fellowship Church (CFC) - Privacy Policy</h1>
        
        <p>Last updated: September 2026</p>

        <h2>What Information We Collect</h2>
        <p>Covenant Fellowship Church's Ride Coordination 
        program collects your name, phone number, and email 
        address when you voluntarily submit our ride signup 
        or driver availability Google Form.</p>

        <h2>How We Use Your Information</h2>
        <p>We use your phone number solely to send SMS 
        messages related to ride coordination, including 
        pickup confirmations, ride cancellations, driver 
        assignment reminders, and pickup/dropoff status 
        updates. We do not use your information for 
        marketing purposes.</p>

        <h2>Message Frequency and Rates</h2>
        <p>Message frequency varies, but you may receive up 
        to 3 messages per week during active shuttle service 
        periods. Message and data rates may apply.</p>

        <h2>Data Sharing</h2>
        <p>Your phone number and personal information will 
        never be sold, rented, or shared with third parties 
        for their marketing purposes. You will not receive 
        third-party marketing messages through this program.</p>

        <h2>Where Your Data Is Stored</h2>
        <p>Your information is stored securely in Google 
        Sheets and Google Cloud services used to operate the 
        Ride Coordination Agent, and is accessed only by 
        church ride coordinators and the automated systems 
        that send these messages.</p>

        <h2>Opting Out</h2>
        <p>You can opt out of SMS messages at any time by 
        replying STOP to any message. You will receive a 
        confirmation and will no longer receive messages 
        from this program. You may opt back in by texting 
        START or by signing up again on our form.</p>

        <h2>Getting Help</h2>
        <p>For help, reply HELP to any message, or contact 
        us directly at 
        <a href="mailto:team@cfchome.org">team@cfchome.org</a>.</p>

        <h2>Contact Us</h2>
        <p>Covenant Fellowship Church<br>
        2906 Crossing Ct, Champaign, IL<br>
        Email: 
        <a href="mailto:team@cfchome.org">team@cfchome.org</a></p>

        <p><a href="/sms-terms">View SMS Program Terms</a></p>

    </body>
    </html>
    """
    return html, 200, {"Content-Type": "text/html"}


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


@app.route("/send-monday-schedule", methods=["POST"])
def route_send_monday_schedule() -> tuple:
    """Trigger the Monday semester-schedule email to the overseer.

    Returns:
        tuple: (result dict, 200) on success, or
            ({"status": "error", "error": str}, 500) on failure.
    """
    try:
        result = send_monday_schedule()
        return jsonify(result), 200
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
        return jsonify(result), 200
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
        return jsonify(result), 200
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
        return jsonify(result), 200
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
        return jsonify(result), 200
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
        return jsonify(result), 200
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
        return jsonify(result), 200
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


@app.route("/sms-webhook", methods=["POST"])
def sms_webhook():
    """Handle incoming SMS from Twilio: opt-out, opt-in, and UPDATE.

    Three kinds of inbound message matter here:

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
    - Driver lookup keywords (ROUTE/RIDERS). Replies with that driver's
      stops and live rider counts, or their rider names, for the
      upcoming Sunday. Authorized off the driver roster rather than an
      allowlist, and only for a driver actually assigned that week.

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
                ADMIN_SUMMARY_KEYWORDS,
                build_admin_reply,
                is_admin_phone,
            )

            from functions.driver_sms_lookup import (
                DRIVER_LOOKUP_KEYWORDS,
                build_driver_lookup_reply,
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
