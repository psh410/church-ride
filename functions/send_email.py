# Sends email notifications to riders and drivers about ride assignments.
#
# Uses the Gmail API with domain-wide delegation via a service account,
# rather than SMTP with an app password - the service account (loaded
# from settings.GOOGLE_APPLICATION_CREDENTIALS) impersonates
# settings.ADMIN_EMAIL to send mail as that mailbox.

from __future__ import annotations

import base64
import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import google.auth
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import settings

logger = logging.getLogger(__name__)

# Minimal scope needed to send mail as the delegated user - no read
# access to the mailbox is requested.
_GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def send_email(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
) -> bool:
    """Send a plain-text email via the Gmail API with domain-wide delegation.

    Loads credentials one of two ways, depending on the environment:
    - Local development: if settings.GOOGLE_APPLICATION_CREDENTIALS
      points to a JSON key file that exists on disk, credentials are
      loaded from it via
      service_account.Credentials.from_service_account_file(), which
      always supports with_subject() impersonation.
    - Cloud Run (or anywhere else without that key file): falls back
      to google.auth.default(). Whether *those* credentials support
      with_subject() depends on what's actually backing Application
      Default Credentials at runtime - a real service account key
      does, but some other ADC types (e.g. Compute Engine metadata
      credentials) don't support impersonation this way at all. This
      is checked explicitly with hasattr(), and a clear RuntimeError
      is raised (caught below, returns False) rather than silently
      sending as the wrong identity if it's unsupported.

    Either way, once we have credentials that support it, they
    impersonate settings.ADMIN_EMAIL via credentials.with_subject() so
    the message is sent (and appears) as that mailbox - no per-user
    OAuth consent screen or Gmail app password is needed, just the
    domain-wide delegation grant configured in Google Workspace admin.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.
        cc: Optional CC address (or comma-separated addresses). Added as
            a visible "Cc" header.
        bcc: Optional BCC address (or comma-separated addresses). Added
            to the raw message's "Bcc" header before it's sent - Gmail
            strips this header from the delivered mail seen by To/Cc
            recipients, but still delivers a copy to every address
            listed here, so it stays invisible to everyone else.

    Returns:
        bool: True if the email was sent successfully, False otherwise.
    """
    try:
        if settings.GOOGLE_APPLICATION_CREDENTIALS and os.path.exists(
            settings.GOOGLE_APPLICATION_CREDENTIALS
        ):
            # Local development - a key file is present on disk.
            credentials = service_account.Credentials.from_service_account_file(
                settings.GOOGLE_APPLICATION_CREDENTIALS,
                scopes=[_GMAIL_SEND_SCOPE],
            )
            delegated_credentials = credentials.with_subject(settings.ADMIN_EMAIL)
        else:
            # Cloud Run (or any environment without a key file) - use
            # the runtime's Application Default Credentials instead.
            credentials, _ = google.auth.default(scopes=[_GMAIL_SEND_SCOPE])

            if not hasattr(credentials, "with_subject"):
                # These ADC aren't backed by a service account identity
                # that supports impersonation - there's no way to send
                # as settings.ADMIN_EMAIL this way.
                raise RuntimeError(
                    "Cloud Run's default service account credentials don't "
                    "support domain-wide delegation via with_subject(). "
                    "Either grant the runtime service account domain-wide "
                    "delegation for the gmail.send scope in Google "
                    "Workspace admin (so google.auth.default() returns "
                    "impersonation-capable credentials), or set "
                    "GOOGLE_APPLICATION_CREDENTIALS to a service account "
                    "key file instead."
                )

            delegated_credentials = credentials.with_subject(settings.ADMIN_EMAIL)
            # Force an immediate token fetch under the impersonated
            # identity, so any delegation misconfiguration surfaces
            # here (and gets logged clearly) instead of failing deep
            # inside the Gmail API call below.
            delegated_credentials.refresh(Request())

        service = build("gmail", "v1", credentials=delegated_credentials)

        message = MIMEMultipart()
        message["To"] = to
        message["From"] = settings.ADMIN_EMAIL
        message["Subject"] = subject
        if cc:
            message["Cc"] = cc
        if bcc:
            message["Bcc"] = bcc
        message.attach(MIMEText(body, "plain"))

        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

        service.users().messages().send(
            userId=settings.ADMIN_EMAIL, body={"raw": raw_message}
        ).execute()

        logger.info("Sent email to %s (subject=%r).", to, subject)
        return True

    except HttpError as exc:
        # Gmail API-specific failures: invalid recipient, delegation not
        # granted, quota exceeded, etc. exc carries the HTTP status/body.
        logger.error("Gmail API error sending email to %s: %s", to, exc)
        return False
    except Exception as exc:
        # Anything else: missing/invalid credentials file, network
        # issues, malformed message, etc.
        logger.error("Unexpected error sending email to %s: %s", to, exc)
        return False
