"""
Test script for the Windows Email Service.

Verifies that:
1. SMTP credentials are valid and email can be sent
2. The remote server outbox API is reachable
3. End-to-end: queue an email on the remote server and send it

Usage:
    python test_email.py                     # run all tests
    python test_email.py smtp                # test SMTP only
    python test_email.py api                 # test API connectivity only
    python test_email.py e2e                 # test end-to-end flow
"""

from __future__ import annotations

import sys

from email_service import (
    REMOTE_SERVER_URL,
    SMTP_FROM_EMAIL,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USE_TLS,
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


def test_api() -> bool:
    """Test connectivity to the remote server outbox API."""
    print(f'[API TEST] Fetching pending emails from {REMOTE_SERVER_URL}')

    try:
        pending = fetch_pending_emails()
        print(f'[API TEST] SUCCESS - {len(pending)} pending email(s) in outbox.')
        for item in pending:
            print(f'  - #{item["id"]} to={item["to_email"]} subject={item["subject"][:60]}')
        return True
    except Exception as exc:
        print(f'[API TEST] FAILED - {type(exc).__name__}: {exc}')
        return False


def test_e2e() -> bool:
    """End-to-end test: fetch pending emails, send the first one, mark it sent."""
    print(f'[E2E TEST] Fetching pending emails from {REMOTE_SERVER_URL}')

    try:
        pending = fetch_pending_emails()
    except Exception as exc:
        print(f'[E2E TEST] FAILED to fetch - {type(exc).__name__}: {exc}')
        return False

    if not pending:
        print('[E2E TEST] No pending emails in outbox. Queue one from the main app first.')
        print('[E2E TEST] SKIPPED')
        return True

    item = pending[0]
    email_id = item['id']
    print(f'[E2E TEST] Sending email #{email_id} to {item["to_email"]}: {item["subject"][:60]}')

    try:
        send_email(
            to_email=item['to_email'],
            subject=item['subject'],
            body=item['body'],
            cc_email=item.get('cc_email'),
        )
        print(f'[E2E TEST] Email #{email_id} sent successfully.')
    except Exception as exc:
        print(f'[E2E TEST] FAILED to send - {type(exc).__name__}: {exc}')
        return False

    try:
        mark_sent(email_id)
        print(f'[E2E TEST] Email #{email_id} marked as sent on remote server.')
    except Exception as exc:
        print(f'[E2E TEST] WARNING - sent but failed to mark on server: {type(exc).__name__}: {exc}')

    print('[E2E TEST] SUCCESS')
    return True


def main() -> None:
    tests = {
        'smtp': test_smtp,
        'api': test_api,
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
