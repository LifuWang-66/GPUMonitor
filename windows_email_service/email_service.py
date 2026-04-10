"""
Windows Email Service for GPU Monitor.

Polls the remote GPU Monitor server for pending emails in the outbox,
sends them via SMTP, and reports delivery status back to the server.

Run:
    python email_service.py
"""

from __future__ import annotations

import logging
import os
import smtplib
import time
from email.message import EmailMessage

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('email_service')

REMOTE_SERVER_URL = os.getenv('REMOTE_SERVER_URL', 'http://10.193.104.165:8000').rstrip('/')
SMTP_HOST = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USERNAME = os.getenv('SMTP_USERNAME', '')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', '')
SMTP_FROM_EMAIL = os.getenv('SMTP_FROM_EMAIL', '')
SMTP_USE_TLS = os.getenv('SMTP_USE_TLS', 'true').lower() in ('true', '1', 'yes')
POLL_INTERVAL_SECONDS = int(os.getenv('POLL_INTERVAL_SECONDS', '30'))


def send_email(to_email: str, subject: str, body: str, cc_email: str | None = None) -> None:
    """Send an email via SMTP. Raises on failure."""
    message = EmailMessage()
    message['From'] = SMTP_FROM_EMAIL
    message['To'] = to_email
    if cc_email:
        message['Cc'] = cc_email
    message['Subject'] = subject
    message.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
        if SMTP_USE_TLS:
            smtp.starttls()
        if SMTP_USERNAME:
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        smtp.send_message(message)


def fetch_pending_emails() -> list[dict]:
    """Fetch pending emails from the remote server's outbox API."""
    url = f'{REMOTE_SERVER_URL}/api/email-outbox/pending'
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return response.json()


def mark_sent(email_id: int) -> None:
    """Mark an email as sent on the remote server."""
    url = f'{REMOTE_SERVER_URL}/api/email-outbox/{email_id}/mark-sent'
    response = requests.post(url, timeout=15)
    response.raise_for_status()


def mark_failed(email_id: int, error_message: str) -> None:
    """Mark an email as failed on the remote server."""
    url = f'{REMOTE_SERVER_URL}/api/email-outbox/{email_id}/mark-failed'
    response = requests.post(url, json={'error_message': error_message}, timeout=15)
    response.raise_for_status()


def process_pending_emails() -> int:
    """Fetch and send all pending emails. Returns number of emails processed."""
    try:
        pending = fetch_pending_emails()
    except Exception:
        logger.exception('Failed to fetch pending emails from %s', REMOTE_SERVER_URL)
        return 0

    if not pending:
        return 0

    logger.info('Found %d pending email(s)', len(pending))
    sent_count = 0

    for item in pending:
        email_id = item['id']
        to_email = item['to_email']
        subject = item['subject']
        body = item['body']
        cc_email = item.get('cc_email')

        try:
            send_email(to_email, subject, body, cc_email=cc_email)
            logger.info('Sent email #%d to %s: %s', email_id, to_email, subject)
            mark_sent(email_id)
            sent_count += 1
        except Exception as exc:
            error_msg = f'{type(exc).__name__}: {exc}'
            logger.error('Failed to send email #%d to %s: %s', email_id, to_email, error_msg)
            try:
                mark_failed(email_id, error_msg)
            except Exception:
                logger.exception('Failed to mark email #%d as failed on remote server', email_id)

    return sent_count


def run_service() -> None:
    """Main loop: poll for pending emails and send them."""
    logger.info('=== GPU Monitor Windows Email Service ===')
    logger.info('Remote server: %s', REMOTE_SERVER_URL)
    logger.info('SMTP host: %s:%d (TLS=%s)', SMTP_HOST, SMTP_PORT, SMTP_USE_TLS)
    logger.info('From email: %s', SMTP_FROM_EMAIL)
    logger.info('Poll interval: %d seconds', POLL_INTERVAL_SECONDS)
    logger.info('Starting polling loop...')

    while True:
        try:
            sent = process_pending_emails()
            if sent > 0:
                logger.info('Sent %d email(s) this cycle', sent)
        except Exception:
            logger.exception('Unexpected error in polling loop')

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == '__main__':
    run_service()
