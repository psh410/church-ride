# Sends email notifications to riders and drivers about ride assignments.
#
# Uses the Gmail API to send mail as the church's admin account. This
# assumes the runtime's Application Default Credentials resolve to a
# Google service account with Gmail domain-wide delegation configured to
# impersonate ADMIN_EMAIL - no interactive OAuth flow happens here.

from __future__ import annotations

import base64
import logging
from email.mime.text import MIMEText

import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import settings

logger = logging.getLogger(__name__)

# Gmail API scope needed to send mail on the admin's behalf.
_GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def send_email(to: str, subject: str, body: str) -> bool:
    """Send a plain-text email via the Gmail API.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.

    Returns:
        bool: True if the email was sent successfully, False otherwise.
    """
    try:
        # Resolve credentials for the Gmail send scope. In production this
        # comes from a service account with domain-wide delegation.
        credentials, _ = google.auth.default(scopes=[_GMAIL_SEND_SCOPE])
        service = build("gmail", "v1", credentials=credentials)

        # Build a standard RFC 2822 message, then base64url-encode it -
        # that's the raw format the Gmail API's send endpoint expects.
        message = MIMEText(body)
        message["to"] = to
        message["from"] = settings.ADMIN_EMAIL
        message["subject"] = subject
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

        service.users().messages().send(
            userId=settings.ADMIN_EMAIL, body={"raw": raw_message}
        ).execute()

        logger.info("Sent email to %s (subject=%r).", to, subject)
        return True

    except HttpError as exc:
        # Gmail API returned an error response (e.g. bad address, quota).
        logger.error("Gmail API error sending email to %s: %s", to, exc)
        return False
    except Exception as exc:
        # Credential resolution failures, network issues, etc.
        logger.error("Unexpected error sending email to %s: %s", to, exc)
        return False
