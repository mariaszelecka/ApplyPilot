"""Email notifications via SMTP (Gmail app password).

Requires GMAIL_ADDRESS and GMAIL_APP_PASSWORD in ~/.applypilot/.env.
Create an app password at https://myaccount.google.com/apppasswords
(requires 2-Step Verification enabled on the Google account).
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587


def send_email(subject: str, html_body: str, text_body: str | None = None, to_addr: str | None = None) -> bool:
    """Send an email via Gmail SMTP. Returns True on success, False otherwise (never raises)."""
    gmail_user = os.environ.get("GMAIL_ADDRESS", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    to_addr = to_addr or gmail_user

    if not gmail_user or not gmail_pass:
        log.warning(
            "GMAIL_ADDRESS/GMAIL_APP_PASSWORD not set in ~/.applypilot/.env -- skipping email send. "
            "See notify.py module docstring for setup instructions."
        )
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to_addr
    if text_body:
        msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, [to_addr], msg.as_string())
        log.info("Email sent to %s: %s", to_addr, subject)
        return True
    except Exception as e:
        log.error("Failed to send email: %s", e)
        return False
