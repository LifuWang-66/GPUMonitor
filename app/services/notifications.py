from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


def send_email(to_email: str, subject: str, body: str, cc_email: str | None = None) -> bool:
    if not settings.smtp_host or not settings.smtp_from_email:
        logger.warning('SMTP not configured, skip sending email to %s with subject %s', to_email, subject)
        return False

    message = EmailMessage()
    message['From'] = settings.smtp_from_email
    message['To'] = to_email
    if cc_email:
        message['Cc'] = cc_email
    message['Subject'] = subject
    message.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password or '')
        smtp.send_message(message)
    return True
