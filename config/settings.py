# Centralized configuration and environment settings for the church ride coordination system.

import os

from dotenv import load_dotenv

# Load variables from a local .env file into the process environment.
# In production (e.g. Cloud Run/Functions), real env vars take precedence
# and this simply becomes a no-op if no .env file is present.
load_dotenv()


def _get_secret(name: str, default: str = None) -> str:
    """Get a config value - from .env locally, from Secret Manager in the cloud.

    Checks the process environment first (populated from .env locally
    by load_dotenv() above, or set directly as real env vars in a cloud
    deployment). If the variable isn't set there, falls back to Google
    Secret Manager - this lets production read secrets that were never
    put in .env/real env vars at all, without requiring a local
    Secret Manager setup for everyday development.

    Args:
        name: The environment variable / secret name to look up, e.g.
            "ANTHROPIC_API_KEY".
        default: The value to return if name isn't found in either the
            environment or Secret Manager. Defaults to None.

    Returns:
        str: The resolved value, or default if it can't be found
            anywhere.
    """
    value = os.getenv(name)
    if value:
        return value

    # Not found and we're not guaranteed to be running in a cloud
    # environment with Secret Manager access - try anyway, but fall
    # back to default rather than raising if it's unavailable (e.g.
    # local dev with no Secret Manager credentials, or the secret
    # simply doesn't exist).
    try:
        from google.cloud import secretmanager

        project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "church-rides")
        client = secretmanager.SecretManagerServiceClient()
        secret_path = f"projects/{project_id}/secrets/{name}/versions/latest"
        response = client.access_secret_version(request={"name": secret_path})
        return response.payload.data.decode("UTF-8")
    except Exception:
        return default


def _setup_service_account_key() -> str | None:
    """Write the service account key from Secret Manager to a temp file, for use where a local key file is expected (Gmail domain-wide delegation).

    Returns the temp file path, or None if running locally with
    GOOGLE_APPLICATION_CREDENTIALS already pointing to a real file.
    """
    import os

    existing_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if existing_path and os.path.exists(existing_path):
        # Already have a real local file - use it as is
        return existing_path

    # Try to get the key from Secret Manager and write it to a temp file
    key_json = _get_secret("SERVICE_ACCOUNT_KEY_JSON")
    if not key_json:
        return None

    import tempfile

    temp_path = os.path.join(tempfile.gettempdir(), "service-account-key.json")
    with open(temp_path, "w") as f:
        f.write(key_json)
    return temp_path

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
ANTHROPIC_API_KEY = _get_secret("ANTHROPIC_API_KEY")
GOOGLE_CLOUD_PROJECT = _get_secret("GOOGLE_CLOUD_PROJECT")
ADMIN_EMAIL = _get_secret("ADMIN_EMAIL")
SHEETS_ID = _get_secret("SHEETS_ID")
OVERSEER_RIDE_EMAIL = _get_secret("OVERSEER_RIDE_EMAIL")
OVERSEER_RIDE_EMAIL_2 = _get_secret("OVERSEER_RIDE_EMAIL_2")
OVERSEER_DRIVER_EMAIL = _get_secret("OVERSEER_DRIVER_EMAIL")
BCC_EMAIL = _get_secret("BCC_EMAIL")
RIDER_SHEET_ID = _get_secret("RIDER_SHEET_ID")
GMAIL_APP_PASSWORD = _get_secret("GMAIL_APP_PASSWORD")

# Path to the service account JSON key file used for Gmail API
# domain-wide delegation (see functions/send_email.py). This is the
# same standard env var Google's client libraries read automatically
# elsewhere (e.g. google.auth.default() in functions/read_sheets.py) -
# it's exposed here as a setting so send_email.py can load it explicitly.
GOOGLE_APPLICATION_CREDENTIALS = _setup_service_account_key()

GOOGLE_MAPS_API_KEY = _get_secret("GOOGLE_MAPS_API_KEY")

# OVERSEER_EMAILS is stored as a comma-separated string in .env
# (e.g. "alice@example.com,bob@example.com") and parsed into a list here.
_overseer_emails_raw = _get_secret("OVERSEER_EMAILS", "")
OVERSEER_EMAILS = [
    email.strip() for email in _overseer_emails_raw.split(",") if email.strip()
]

DISCORD_BOT_TOKEN = _get_secret("DISCORD_BOT_TOKEN")

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
    "OVERSEER_RIDE_EMAIL_2": OVERSEER_RIDE_EMAIL_2,
    "OVERSEER_DRIVER_EMAIL": OVERSEER_DRIVER_EMAIL,
    "BCC_EMAIL": BCC_EMAIL,
    "GMAIL_APP_PASSWORD": GMAIL_APP_PASSWORD,
    "RIDER_SHEET_ID": RIDER_SHEET_ID,
    "GOOGLE_APPLICATION_CREDENTIALS": GOOGLE_APPLICATION_CREDENTIALS,
    "GOOGLE_MAPS_API_KEY": GOOGLE_MAPS_API_KEY,
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
