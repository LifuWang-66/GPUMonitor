"""
Windows Email Service for GPU Monitor.

Connects to the remote server (165) via SSH, reads the email_outbox table
directly from the SQLite database, sends pending emails via SMTP, and
updates the database to mark them as sent/failed.

Run:
    python email_service.py
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import time
from email.message import EmailMessage

import paramiko
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('email_service')

SSH_HOST = os.getenv('SSH_HOST', '10.193.104.165')
SSH_PORT = int(os.getenv('SSH_PORT', '22'))
SSH_USERNAME = os.getenv('SSH_USERNAME', 'lifu')
SSH_PASSWORD = os.getenv('SSH_PASSWORD', '')
REMOTE_DB_PATH = os.getenv('REMOTE_DB_PATH', '/home/lifu/workspace/GPUMonitor/data/gpu_monitor.db')

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


def _ssh_exec(client: paramiko.SSHClient, command: str) -> str:
    """Execute a command over SSH and return stdout. Raises on non-zero exit."""
    _, stdout, stderr = client.exec_command(command, timeout=15)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode('utf-8', errors='replace').strip()
    if exit_code != 0:
        err = stderr.read().decode('utf-8', errors='replace').strip()
        raise RuntimeError(f'SSH command failed (exit {exit_code}): {err}')
    return out


def _connect_ssh() -> paramiko.SSHClient:
    """Create and return an SSH connection to the remote server."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=SSH_HOST,
        port=SSH_PORT,
        username=SSH_USERNAME,
        password=SSH_PASSWORD,
        timeout=10,
    )
    return client


def fetch_pending_emails(client: paramiko.SSHClient) -> list[dict]:
    """Query the remote SQLite database for pending emails via SSH."""
    query = "SELECT id, to_email, cc_email, subject, body FROM email_outbox WHERE status = 'pending' ORDER BY created_at LIMIT 50;"
    command = f'sqlite3 -json "{REMOTE_DB_PATH}" "{query}"'
    output = _ssh_exec(client, command)
    if not output:
        return []
    return json.loads(output)


def mark_sent(client: paramiko.SSHClient, email_id: int) -> None:
    """Mark an email as sent in the remote database."""
    query = f"UPDATE email_outbox SET status = 'sent', sent_at = datetime('now') WHERE id = {email_id};"
    command = f'sqlite3 "{REMOTE_DB_PATH}" "{query}"'
    _ssh_exec(client, command)


def mark_failed(client: paramiko.SSHClient, email_id: int, error_message: str) -> None:
    """Mark an email as failed in the remote database."""
    safe_msg = error_message.replace("'", "''")[:500]
    query = f"UPDATE email_outbox SET status = 'failed', error_message = '{safe_msg}' WHERE id = {email_id};"
    command = f'sqlite3 "{REMOTE_DB_PATH}" "{query}"'
    _ssh_exec(client, command)


def process_pending_emails() -> int:
    """Connect via SSH, fetch pending emails, send them, update status. Returns count sent."""
    try:
        client = _connect_ssh()
    except Exception:
        logger.exception('Failed to SSH into %s', SSH_HOST)
        return 0

    try:
        try:
            pending = fetch_pending_emails(client)
        except Exception:
            logger.exception('Failed to query pending emails from remote DB')
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
            cc_email = item.get('cc_email') or None

            try:
                send_email(to_email, subject, body, cc_email=cc_email)
                logger.info('Sent email #%d to %s: %s', email_id, to_email, subject)
                mark_sent(client, email_id)
                sent_count += 1
            except Exception as exc:
                error_msg = f'{type(exc).__name__}: {exc}'
                logger.error('Failed to send email #%d to %s: %s', email_id, to_email, error_msg)
                try:
                    mark_failed(client, email_id, error_msg)
                except Exception:
                    logger.exception('Failed to mark email #%d as failed in remote DB', email_id)

        return sent_count
    finally:
        client.close()


def run_service() -> None:
    """Main loop: poll for pending emails and send them."""
    logger.info('=== GPU Monitor Windows Email Service ===')
    logger.info('Remote host: %s (SSH)', SSH_HOST)
    logger.info('Remote DB: %s', REMOTE_DB_PATH)
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
