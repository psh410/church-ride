# Centralized configuration and environment settings for the church ride coordination system.

import os

from dotenv import load_dotenv

# Load variables from a local .env file into the process environment.
# In production (e.g. Cloud Run/Functions), real env vars take precedence
# and this simply becomes a no-op if no .env file is present.
load_dotenv()

# --------------------------------------------------------------------------
# System limits and scheduling constants
# --------------------------------------------------------------------------
# These bound the size of a single ride-coordination run and are used by the
# agents to validate capacity before assigning riders to drivers/routes.
MAX_RIDERS = 100
MAX_ROUTES = 10

# Note: driver counts and per-route rider capacity are dynamic - they are
# stored in Firestore per route (not hardcoded here) since they can change
# week to week as drivers and their vehicle capacities vary.

# Percentage-of-capacity checkpoints at which the monitor agent should raise
# an alert (e.g. "50% of riders assigned").
ALERT_THRESHOLDS = [25, 50, 75, 100]

# Note: routes themselves are also dynamic and stored in Firestore, not
# hardcoded here.

# Time (24-hour "HH:MM", local time) the Saturday ride-coordination run
# is scheduled to start.
SATURDAY_RUN_TIME = "09:00"

# Time (24-hour "HH:MM", local time) of the "dead man's switch" check -
# if the run hasn't completed successfully by this time, escalate/alert.
DEAD_MAN_HOUR = "10:00"

# --------------------------------------------------------------------------
# Secrets and environment-specific configuration (loaded from .env)
# --------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
SHEETS_ID = os.getenv("SHEETS_ID")
OVERSEER_RIDE_EMAIL = os.getenv("OVERSEER_RIDE_EMAIL")
OVERSEER_DRIVER_EMAIL = os.getenv("OVERSEER_DRIVER_EMAIL")
BCC_EMAIL = os.getenv("BCC_EMAIL")
RIDER_SHEET_ID = os.getenv("RIDER_SHEET_ID")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

# OVERSEER_EMAILS is stored as a comma-separated string in .env
# (e.g. "alice@example.com,bob@example.com") and parsed into a list here.
_overseer_emails_raw = os.getenv("OVERSEER_EMAILS", "")
OVERSEER_EMAILS = [
    email.strip() for email in _overseer_emails_raw.split(",") if email.strip()
]

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
# Required env-backed settings that must be present for the system to run.
# OVERSEER_EMAILS is checked separately since an empty list, not None,
# indicates a missing value.
_REQUIRED_SETTINGS = {
    "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
    "GOOGLE_CLOUD_PROJECT": GOOGLE_CLOUD_PROJECT,
    "ADMIN_EMAIL": ADMIN_EMAIL,
    "DISCORD_BOT_TOKEN": DISCORD_BOT_TOKEN,
    "SHEETS_ID": SHEETS_ID,
    "OVERSEER_RIDE_EMAIL": OVERSEER_RIDE_EMAIL,
    "OVERSEER_DRIVER_EMAIL": OVERSEER_DRIVER_EMAIL,
    "BCC_EMAIL": BCC_EMAIL,
    "GMAIL_APP_PASSWORD": GMAIL_APP_PASSWORD,
    "RIDER_SHEET_ID": RIDER_SHEET_ID,
}


def validate() -> None:
    """Ensure all required environment variables are present.

    Raises:
        EnvironmentError: If one or more required settings are missing,
            with a clear message listing exactly which ones.
    """
    missing = [name for name, value in _REQUIRED_SETTINGS.items() if not value]

    if not OVERSEER_EMAILS:
        missing.append("OVERSEER_EMAILS")

    if missing:
        raise EnvironmentError(
            "Missing required environment variable(s): "
            f"{', '.join(missing)}. "
            "Please set them in your .env file or the deployment environment."
        )
