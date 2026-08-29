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


# ============================================
# TEST ENDPOINTS - safe to call anytime, only
# emails peterhahn410@gmail.com
# ============================================


@app.route("/test-saturday-update", methods=["POST"])
def test_saturday_update() -> tuple:
    """Test endpoint - sends Saturday update only to admin, not real overseers.

    For safe testing before deployment.

    Returns:
        tuple: ({"status": "sent" or "failed"}, 200) on success, or
            ({"status": "error", "error": str}, 500) on failure.
    """
    try:
        from functions.read_riders_sheet import get_next_sunday_date, get_all_riders_for_sunday
        from functions.send_email import send_email
        sunday = get_next_sunday_date()
        data = get_all_riders_for_sunday(sunday)
        body = f"TEST EMAIL - Sunday: {sunday}\nTotal: {data['total']}\nShuttle: {data['shuttle_total']}\nNon-shuttle: {data['non_shuttle_total']}"
        result = send_email(
            to="peterhahn410@gmail.com",
            subject="TEST - Cloud App Saturday Update",
            body=body
        )
        return jsonify({"status": "sent" if result else "failed"}), 200
    except Exception as exc:
        logger.error("Test saturday update failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/test-wednesday-reminder", methods=["POST"])
def test_wednesday_reminder() -> tuple:
    """Test endpoint - sends a Wednesday reminder summary only to admin.

    Doesn't call send_wednesday_reminder() directly (that would email
    the real drivers) - instead builds a plain-text summary of that
    week's schedule entry and emails it to peterhahn410@gmail.com only.

    Returns:
        tuple: ({"status": "sent" or "failed", "note": str}, 200) on
            success, or ({"status": "error", "error": str}, 500) on
            failure.
    """
    try:
        from db.firestore_client import get_semester_schedule
        from functions.send_email import send_email

        sunday = get_next_sunday_date()
        schedule = get_semester_schedule()
        entry = next((e for e in schedule if e.get("date") == sunday), None)

        if entry is None:
            body = f"TEST EMAIL - Wednesday Reminder\nSunday: {sunday}\nNo schedule entry found for this date."
        else:
            body = (
                f"TEST EMAIL - Wednesday Reminder\n"
                f"Sunday: {sunday}\n"
                f"Shuttle 1: {entry.get('shuttle_1')}\n"
                f"Shuttle 2: {entry.get('shuttle_2')}\n"
                f"Backup: {entry.get('backup') or 'None'}"
            )

        result = send_email(
            to="peterhahn410@gmail.com",
            subject="TEST - Cloud App Wednesday Reminder",
            body=body,
        )
        return jsonify(
            {
                "status": "sent" if result else "failed",
                "note": "test only - not sent to real drivers",
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


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
