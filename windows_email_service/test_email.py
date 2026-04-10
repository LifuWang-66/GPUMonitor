"""
Test script for the Windows Email Service.

Verifies that:
1. SMTP credentials are valid and email can be sent
2. SSH + SQLite connectivity to the remote server works
3. End-to-end: insert a test email, send it, mark it sent

Usage:
    python test_email.py                     # run all tests
    python test_email.py smtp                # test SMTP only
    python test_email.py ssh                 # test SSH + DB connectivity only
    python test_email.py e2e                 # test end-to-end flow
"""

from __future__ import annotations

import sys

from email_service import (
    REMOTE_DB_PATH,
    SMTP_FROM_EMAIL,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USE_TLS,
    SSH_HOST,
    _connect_ssh,
    _ssh_exec,
    fetch_pending_emails,
    mark_sent,
    send_email,
)


def test_smtp() -> bool:
    """Send a test email via SMTP to verify credentials work."""
    print(f'[SMTP TEST] Sending test email via {SMTP_HOST}:{SMTP_PORT} (TLS={SMTP_USE_TLS})')
    print(f'[SMTP TEST] From: {SMTP_FROM_EMAIL}')
    target = SMTP_FROM_EMAIL  # send to self
    print(f'[SMTP TEST] To: {target}')

    try:
        send_email(
            to_email=target,
            subject='[TEST] GPU Monitor Windows Email Service - SMTP Test',
            body=(
                'This is a test email from the GPU Monitor Windows Email Service.\n\n'
                'If you received this, the SMTP configuration is working correctly.\n'
            ),
        )
        print('[SMTP TEST] SUCCESS - Email sent.')
        return True
    except Exception as exc:
        print(f'[SMTP TEST] FAILED - {type(exc).__name__}: {exc}')
        return False


def test_ssh() -> bool:
    """Test SSH connectivity and SQLite database access on the remote server."""
    print(f'[SSH TEST] Connecting to {SSH_HOST} via SSH...')

    try:
        client = _connect_ssh()
    except Exception as exc:
        print(f'[SSH TEST] FAILED to connect - {type(exc).__name__}: {exc}')
        return False

    try:
        # Check sqlite3 is available
        version = _ssh_exec(client, 'sqlite3 --version')
        print(f'[SSH TEST] sqlite3 version: {version}')

        # Check database file exists
        check = _ssh_exec(client, f'test -f "{REMOTE_DB_PATH}" && echo "exists" || echo "missing"')
        if check != 'exists':
            print(f'[SSH TEST] FAILED - Database not found at {REMOTE_DB_PATH}')
            return False
        print(f'[SSH TEST] Database found at {REMOTE_DB_PATH}')

        # Check email_outbox table exists
        tables = _ssh_exec(client, f'sqlite3 "{REMOTE_DB_PATH}" ".tables"')
        if 'email_outbox' not in tables:
            print(f'[SSH TEST] WARNING - email_outbox table not found. Tables: {tables}')
            print('[SSH TEST] The main app needs to be restarted to create the table.')
            return False

        # Query pending emails
        pending = fetch_pending_emails(client)
        print(f'[SSH TEST] SUCCESS - {len(pending)} pending email(s) in outbox.')
        for item in pending:
            print(f'  - #{item["id"]} to={item["to_email"]} subject={item["subject"][:60]}')
        return True
    except Exception as exc:
        print(f'[SSH TEST] FAILED - {type(exc).__name__}: {exc}')
        return False
    finally:
        client.close()


def test_e2e() -> bool:
    """End-to-end: insert a test email into outbox, send it, mark it sent."""
    print(f'[E2E TEST] Connecting to {SSH_HOST} via SSH...')

    try:
        client = _connect_ssh()
    except Exception as exc:
        print(f'[E2E TEST] FAILED to connect - {type(exc).__name__}: {exc}')
        return False

    try:
        # Insert a test email into the outbox
        insert_sql = (
            "INSERT INTO email_outbox (to_email, subject, body, status, created_at) "
            f"VALUES ('{SMTP_FROM_EMAIL}', "
            "'[TEST] GPU Monitor E2E Test', "
            "'This is an end-to-end test from the Windows Email Service.', "
            "'pending', datetime('now'));"
        )
        _ssh_exec(client, f'sqlite3 "{REMOTE_DB_PATH}" "{insert_sql}"')
        print('[E2E TEST] Inserted test email into outbox.')

        # Fetch it back
        pending = fetch_pending_emails(client)
        if not pending:
            print('[E2E TEST] FAILED - No pending emails found after insert.')
            return False

        item = pending[-1]  # last one should be the one we just inserted
        email_id = item['id']
        print(f'[E2E TEST] Sending email #{email_id} to {item["to_email"]}: {item["subject"][:60]}')

        try:
            send_email(
                to_email=item['to_email'],
                subject=item['subject'],
                body=item['body'],
                cc_email=item.get('cc_email') or None,
            )
            print(f'[E2E TEST] Email #{email_id} sent successfully.')
        except Exception as exc:
            print(f'[E2E TEST] FAILED to send - {type(exc).__name__}: {exc}')
            return False

        try:
            mark_sent(client, email_id)
            print(f'[E2E TEST] Email #{email_id} marked as sent in remote DB.')
        except Exception as exc:
            print(f'[E2E TEST] WARNING - sent but failed to mark in DB: {type(exc).__name__}: {exc}')

        print('[E2E TEST] SUCCESS')
        return True
    except Exception as exc:
        print(f'[E2E TEST] FAILED - {type(exc).__name__}: {exc}')
        return False
    finally:
        client.close()


def main() -> None:
    tests = {
        'smtp': test_smtp,
        'ssh': test_ssh,
        'e2e': test_e2e,
    }

    requested = sys.argv[1:] if len(sys.argv) > 1 else list(tests.keys())
    results: dict[str, bool] = {}

    for name in requested:
        if name not in tests:
            print(f'Unknown test: {name}. Available: {", ".join(tests.keys())}')
            sys.exit(1)
        print()
        results[name] = tests[name]()

    print()
    print('=== Results ===')
    all_passed = True
    for name, passed in results.items():
        status = 'PASS' if passed else 'FAIL'
        print(f'  {name}: {status}')
        if not passed:
            all_passed = False

    sys.exit(0 if all_passed else 1)


if __name__ == '__main__':
    main()
