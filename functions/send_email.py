# Sends email notifications to riders and drivers about ride assignments.
#
# Uses SMTP (Gmail's smtp.gmail.com relay) with an app password, rather
# than the Gmail API - no Google API client or service account required.

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import settings

logger = logging.getLogger(__name__)

_SMTP_SERVER = "smtp.gmail.com"
_SMTP_PORT = 587


def send_email(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
) -> bool:
    """Send a plain-text email via Gmail's SMTP relay.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.
        cc: Optional CC address (or comma-separated addresses). Added as
            a visible "Cc" header.
        bcc: Optional BCC address (or comma-separated addresses). Never
            added as a header - included only in the SMTP envelope
            recipients so it stays invisible to everyone else.

    Returns:
        bool: True if the email was sent successfully, False otherwise.
    """
    try:
        message = MIMEMultipart()
        message["To"] = to
        message["From"] = settings.ADMIN_EMAIL
        message["Subject"] = subject
        if cc:
            message["Cc"] = cc
        message.attach(MIMEText(body, "plain"))

        # The SMTP envelope recipients determine who actually receives the
        # mail - Bcc is deliberately left off the headers above so it
        # never shows up in anyone's copy of the message.
        recipients = [to]
        if cc:
            recipients += [address.strip() for address in cc.split(",") if address.strip()]
        if bcc:
            recipients += [address.strip() for address in bcc.split(",") if address.strip()]

        with smtplib.SMTP(_SMTP_SERVER, _SMTP_PORT) as server:
            server.starttls()
            server.login(settings.ADMIN_EMAIL, settings.GMAIL_APP_PASSWORD)
            server.sendmail(settings.ADMIN_EMAIL, recipients, message.as_string())

        logger.info("Sent email to %s (subject=%r).", to, subject)
        return True

    except smtplib.SMTPException as exc:
        # SMTP-specific failures: auth rejected, bad recipient, etc.
        logger.error("SMTP error sending email to %s: %s", to, exc)
        return False
    except Exception as exc:
        # Anything else: network issues, missing credentials, etc.
        logger.error("Unexpected error sending email to %s: %s", to, exc)
        return False
