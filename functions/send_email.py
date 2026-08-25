# Sends email notifications to riders and drivers about ride assignments.
#
# Uses the Gmail API with domain-wide delegation via a service account,
# rather than SMTP with an app password - the service account (loaded
# from settings.GOOGLE_APPLICATION_CREDENTIALS) impersonates
# settings.ADMIN_EMAIL to send mail as that mailbox.

from __future__ import annotations

import base64
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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

    Loads service account credentials from the JSON key file at
    settings.GOOGLE_APPLICATION_CREDENTIALS, then impersonates
    settings.ADMIN_EMAIL via credentials.with_subject() so the message
    is sent (and appears) as that mailbox - no per-user OAuth consent
    screen or Gmail app password is needed, just the domain-wide
    delegation grant configured in Google Workspace admin.

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
        credentials = service_account.Credentials.from_service_account_file(
            settings.GOOGLE_APPLICATION_CREDENTIALS,
            scopes=[_GMAIL_SEND_SCOPE],
        )
        delegated_credentials = credentials.with_subject(settings.ADMIN_EMAIL)
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
