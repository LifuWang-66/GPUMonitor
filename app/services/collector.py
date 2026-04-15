from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CurrentGpuStatus, DailyGpuAggregate, DailyUserAggregate, Host, NotificationEvent, UserProfile, UserUtilizationSample
from app.schemas import CurrentGpuResponse
from app.services.analytics import snapshot_to_current_status
from app.services.notifications import send_email
from app.services.ssh_client import HostSnapshot, SshCredentials, collect_host_snapshot, kill_user_gpu_processes

settings = get_settings()
HIGH_GPU_COUNT_THRESHOLD = 8
STORAGE_THRESHOLD_BYTES = int(1.5 * 1024 * 1024 * 1024 * 1024)
LOW_UTIL_THRESHOLD = 40.0
MID_UTIL_THRESHOLD = 70.0
UTILIZATION_WINDOW = timedelta(hours=4)
ESCALATION_AFTER = timedelta(days=1)
RUN_COLLECTION_LOCK = threading.Lock()
MIN_UTILIZATION_WINDOW_SAMPLE_RATIO = 0.9
_LAST_HOME_USAGE_SCAN_DATE_BY_HOST: dict[int, date] = {}


def get_collector_credentials() -> SshCredentials | None:
    if not settings.collector_ssh_username:
        return None
    return SshCredentials(
        username=settings.collector_ssh_username,
        password=settings.collector_ssh_password,
        key_path=settings.collector_ssh_key_path,
        use_agent=bool(settings.collector_ssh_key_path and not settings.collector_ssh_password),
    )


def ensure_hosts(db: Session) -> list[Host]:
    existing = {host.address: host for host in db.scalars(select(Host)).all()}
    hosts: list[Host] = []
    for item in settings.hosts:
        host = existing.get(item['address'])
        if host is None:
            host = Host(name=item['name'], address=item['address'], enabled=True)
            db.add(host)
            db.flush()
        else:
            host.name = item['name']
            host.enabled = True
        hosts.append(host)
    db.commit()
    return hosts


def collect_live_current_status(allowed_hosts: list[str]) -> tuple[list[CurrentGpuResponse], list[str]]:
    credentials = get_collector_credentials()
    if credentials is None:
        return [], ['Collector skipped: missing COLLECTOR_SSH_USERNAME configuration.']

    snapshots: list[CurrentGpuResponse] = []
    errors: list[str] = []
    allowed = {host['address']: host['name'] for host in settings.hosts if host['address'] in allowed_hosts}
    for address, name in allowed.items():
        try:
            snapshot = collect_host_snapshot(name, address, credentials)
            snapshots.extend(snapshot_to_current_status(snapshot))
        except Exception as exc:  # noqa: BLE001
            errors.append(f'Failed {address}: {exc}')
    return snapshots, errors


def refresh_current_status_only(db: Session, allowed_hosts: list[str]) -> tuple[list[CurrentGpuResponse], list[str]]:
    credentials = get_collector_credentials()
    if credentials is None:
        return [], ['Collector skipped: missing COLLECTOR_SSH_USERNAME configuration.']

    host_rows = db.scalars(select(Host).where(Host.address.in_(allowed_hosts))).all()
    host_by_address = {host.address: host for host in host_rows}
    errors: list[str] = []
    for address in allowed_hosts:
        host = host_by_address.get(address)
        if not host:
            continue
        try:
            snapshot = collect_host_snapshot(host.name, host.address, credentials, include_home_user_usage=False)
            print(f'[refresh] host={host.name} address={host.address} collected_at={snapshot.collected_at.isoformat()} gpus={len(snapshot.gpu_records)}')
            for record in snapshot.gpu_records:
                print(
                    '[refresh] '
                    f'host={host.address} '
                    f'gpu_index={record.gpu_index} '
                    f'gpu_name={record.gpu_name} '
                    f'util={record.utilization_gpu:.1f}% '
                    f'mem={record.memory_used_mb:.0f}/{record.memory_total_mb:.0f}MB '
                    f'users={",".join(record.active_users) if record.active_users else "none"} '
                    f'proc={record.process_count}'
                )
            _upsert_current_status_snapshot(db, host, snapshot)
        except Exception as exc:  # noqa: BLE001
            errors.append(f'Failed {address}: {exc}')
    db.commit()
    refreshed = db.execute(
        select(CurrentGpuStatus, Host)
        .join(Host, CurrentGpuStatus.host_id == Host.id)
        .where(Host.address.in_(allowed_hosts))
        .order_by(Host.address, CurrentGpuStatus.gpu_index)
    ).all()
    current_status = [
        CurrentGpuResponse(
            host_name=host.name,
            host_address=host.address,
            gpu_index=status.gpu_index,
            gpu_name=status.gpu_name,
            utilization_gpu=status.utilization_gpu,
            memory_used_mb=status.memory_used_mb,
            memory_total_mb=status.memory_total_mb,
            temperature_c=status.temperature_c,
            active_users=[user for user in status.active_users.split(',') if user],
            process_count=status.process_count,
            is_idle=status.is_idle,
            last_seen_at=status.last_seen_at,
        )
        for status, host in refreshed
    ]
    return current_status, errors


def run_collection(db: Session) -> list[str]:
    if not RUN_COLLECTION_LOCK.acquire(blocking=False):
        return ['Collector skipped: another collection run is already in progress.']
    credentials = get_collector_credentials()
    try:
        if credentials is None:
            return ['Collector skipped: missing COLLECTOR_SSH_USERNAME configuration.']

        messages: list[str] = []
        hosts = ensure_hosts(db)
        for host in hosts:
            try:
                snapshot = collect_host_snapshot(
                    host.name,
                    host.address,
                    credentials,
                    include_home_user_usage=_should_collect_home_user_usage_today(host.id),
                )
                upsert_snapshot(db, host, snapshot)
                _evaluate_and_handle_user_alerts(db, host, snapshot, credentials)
                messages.append(f'Collected {host.address}')
            except Exception as exc:  # noqa: BLE001
                messages.append(f'Failed {host.address}: {exc}')
        cleanup_old_data(db)
        db.commit()
        return messages
    finally:
        RUN_COLLECTION_LOCK.release()


def _should_collect_home_user_usage_today(host_id: int) -> bool:
    today = datetime.now(timezone.utc).date()
    last_scanned = _LAST_HOME_USAGE_SCAN_DATE_BY_HOST.get(host_id)
    if last_scanned == today:
        return False
    _LAST_HOME_USAGE_SCAN_DATE_BY_HOST[host_id] = today
    return True


def upsert_snapshot(db: Session, host: Host, snapshot: HostSnapshot) -> None:
    sample_date = snapshot.collected_at.date()
    _upsert_current_status_snapshot(db, host, snapshot)
    for record in snapshot.gpu_records:
        is_idle = record.utilization_gpu < 10.0

        daily_gpu_insert = sqlite_insert(DailyGpuAggregate).values(
            host_id=host.id,
            gpu_index=record.gpu_index,
            gpu_name=record.gpu_name,
            date=sample_date,
            samples=1,
            busy_samples=1 if record.process_count > 0 else 0,
            non_idle_samples=1 if not is_idle else 0,
            total_utilization=record.utilization_gpu,
            total_memory_used_mb=record.memory_used_mb,
        )
        daily_gpu_upsert = daily_gpu_insert.on_conflict_do_update(
            index_elements=['host_id', 'gpu_index', 'date'],
            set_={
                'gpu_name': record.gpu_name,
                'samples': func.coalesce(DailyGpuAggregate.samples, 0) + 1,
                'busy_samples': func.coalesce(DailyGpuAggregate.busy_samples, 0) + (1 if record.process_count > 0 else 0),
                'non_idle_samples': func.coalesce(DailyGpuAggregate.non_idle_samples, 0) + (1 if not is_idle else 0),
                'total_utilization': func.coalesce(DailyGpuAggregate.total_utilization, 0.0) + record.utilization_gpu,
                'total_memory_used_mb': func.coalesce(DailyGpuAggregate.total_memory_used_mb, 0.0) + record.memory_used_mb,
            },
        )
        db.execute(daily_gpu_upsert)

        for username in record.active_users:
            if username in settings.excluded_users:
                continue
            daily_user_insert = sqlite_insert(DailyUserAggregate).values(
                host_id=host.id,
                username=username,
                date=sample_date,
                gpu_samples=1,
                non_idle_samples=1 if not is_idle else 0,
                total_utilization=record.utilization_gpu,
            )
            daily_user_upsert = daily_user_insert.on_conflict_do_update(
                index_elements=['host_id', 'username', 'date'],
                set_={
                    'gpu_samples': func.coalesce(DailyUserAggregate.gpu_samples, 0) + 1,
                    'non_idle_samples': func.coalesce(DailyUserAggregate.non_idle_samples, 0) + (1 if not is_idle else 0),
                    'total_utilization': func.coalesce(DailyUserAggregate.total_utilization, 0.0) + record.utilization_gpu,
                },
            )
            db.execute(daily_user_upsert)

    _persist_user_utilization_samples(db, host, snapshot)


def _upsert_current_status_snapshot(db: Session, host: Host, snapshot: HostSnapshot) -> None:
    for record in snapshot.gpu_records:
        is_idle = record.utilization_gpu < 10.0
        current = db.scalar(
            select(CurrentGpuStatus).where(
                CurrentGpuStatus.host_id == host.id,
                CurrentGpuStatus.gpu_index == record.gpu_index,
            )
        )
        if current is None:
            current = CurrentGpuStatus(host_id=host.id, gpu_index=record.gpu_index, gpu_name=record.gpu_name, gpu_uuid=record.gpu_uuid)
            db.add(current)
        current.gpu_name = record.gpu_name
        current.gpu_uuid = record.gpu_uuid
        current.utilization_gpu = record.utilization_gpu
        current.memory_used_mb = record.memory_used_mb
        current.memory_total_mb = record.memory_total_mb
        current.temperature_c = record.temperature_c
        current.active_users = ','.join(record.active_users)
        current.process_count = record.process_count
        current.is_idle = is_idle
        current.last_seen_at = snapshot.collected_at.replace(tzinfo=None)


def _persist_user_utilization_samples(db: Session, host: Host, snapshot: HostSnapshot) -> None:
    per_user_utils: dict[str, list[float]] = {}
    for record in snapshot.gpu_records:
        for username in record.active_users:
            if username in settings.excluded_users:
                continue
            per_user_utils.setdefault(username, []).append(record.utilization_gpu)

    sampled_at = snapshot.collected_at.replace(tzinfo=None)
    for username, utils in per_user_utils.items():
        db.add(
            UserUtilizationSample(
                host_id=host.id,
                username=username,
                sampled_at=sampled_at,
                average_gpu_utilization=round(sum(utils) / max(len(utils), 1), 2),
            )
        )


def _evaluate_and_handle_user_alerts(db: Session, host: Host, snapshot: HostSnapshot, credentials: SshCredentials) -> None:
    lifu_profile = db.scalar(select(UserProfile).where(UserProfile.username == 'lifu'))
    cc_email = lifu_profile.email if lifu_profile and lifu_profile.email else None
    active_issues: set[tuple[str, str]] = set()

    if snapshot.home_user_used_bytes is not None:
        for username, used_bytes in snapshot.home_user_used_bytes.items():
            if used_bytes <= STORAGE_THRESHOLD_BYTES:
                continue
            profile = db.scalar(select(UserProfile).where(UserProfile.username == username))
            if not profile or not profile.email:
                continue
            used_tb = used_bytes / 1024 / 1024 / 1024 / 1024
            _notify_issue_with_escalation(
                db,
                host,
                username,
                profile.email,
                event_type='home_user_storage_over_1_5tb',
                reason=f'/home/{username} usage is {used_tb:.2f} TB, which exceeds the 1.5 TB threshold.',
                cc_email=cc_email,
                lifu_email=cc_email,
            )
            active_issues.add((username, 'home_user_storage_over_1_5tb'))

    per_user_gpu_count: dict[str, int] = {}
    for record in snapshot.gpu_records:
        for username in record.active_users:
            if username in settings.excluded_users:
                continue
            per_user_gpu_count[username] = per_user_gpu_count.get(username, 0) + 1

    for username, gpu_count in per_user_gpu_count.items():
        profile = db.scalar(select(UserProfile).where(UserProfile.username == username))
        if not profile or not profile.email:
            continue

        four_hour_max, sample_count = _get_window_max_util(db, host.id, username, snapshot.collected_at.replace(tzinfo=None))
        required_samples = _required_samples_for_window()

        if gpu_count > HIGH_GPU_COUNT_THRESHOLD:
            _notify_issue_with_escalation(
                db,
                host,
                username,
                profile.email,
                event_type='gpu_count_over_8',
                reason=(
                    f'You are using {gpu_count} GPUs on this host. More than 8 GPUs can heavily impact fair-share '
                    'capacity and block other users from scheduling jobs.'
                ),
                cc_email=cc_email,
                lifu_email=cc_email,
            )
            active_issues.add((username, 'gpu_count_over_8'))

        if sample_count < required_samples:
            continue

        if LOW_UTIL_THRESHOLD <= four_hour_max <= MID_UTIL_THRESHOLD:
            _notify_issue_with_escalation(
                db,
                host,
                username,
                profile.email,
                event_type='avg_util_8h_40_70',
                reason=f'Your 4-hour max GPU utilization is {four_hour_max:.2f}% (between 40% and 70%).',
                cc_email=cc_email,
                lifu_email=cc_email,
            )
            active_issues.add((username, 'avg_util_8h_40_70'))
        elif four_hour_max < LOW_UTIL_THRESHOLD and four_hour_max >= 0:
            kill_result = kill_user_gpu_processes(host.address, credentials, username)
            _notify_issue_with_escalation(
                db,
                host,
                username,
                profile.email,
                event_type='avg_util_8h_below_40_killed',
                reason=(
                    f'Your 4-hour max GPU utilization is {four_hour_max:.2f}% (below 40%). '
                    f'GPU processes were terminated. Killed PIDs: {kill_result or "none"}'
                ),
                cc_email=cc_email,
                lifu_email=cc_email,
            )
            active_issues.add((username, 'avg_util_8h_below_40_killed'))

    _clear_resolved_issue_events(db, host.id, active_issues)


def _get_window_max_util(db: Session, host_id: int, username: str, now_naive: datetime) -> tuple[float, int]:
    since = now_naive - UTILIZATION_WINDOW
    rows = db.scalars(
        select(UserUtilizationSample.average_gpu_utilization).where(
            UserUtilizationSample.host_id == host_id,
            UserUtilizationSample.username == username,
            UserUtilizationSample.sampled_at >= since,
        )
    ).all()
    if not rows:
        return -1.0, 0
    return float(max(rows)), len(rows)


def _required_samples_for_window() -> int:
    expected = int((UTILIZATION_WINDOW.total_seconds() / 60) / max(settings.collector_interval_minutes, 1))
    return max(int(expected * MIN_UTILIZATION_WINDOW_SAMPLE_RATIO), 1)


def _notify_issue_with_escalation(
    db: Session,
    host: Host,
    username: str,
    email: str,
    event_type: str,
    reason: str,
    cc_email: str | None = None,
    lifu_email: str | None = None,
) -> None:
    now = datetime.utcnow()
    active_key = 'active'
    escalated_key = 'escalated'
    existing = db.scalar(
        select(NotificationEvent).where(
            NotificationEvent.host_id == host.id,
            NotificationEvent.username == username,
            NotificationEvent.event_type == event_type,
            NotificationEvent.event_key == active_key,
        )
    )
    if existing is None:
        subject, body = build_notification_email(host.name, host.address, username, event_type, reason)
        if send_email(email, subject, body, cc_email=cc_email):
            db.add(
                NotificationEvent(
                    host_id=host.id,
                    username=username,
                    event_type=event_type,
                    event_key=active_key,
                    sent_at=now,
                )
            )
        return

    if lifu_email is None:
        return

    if now - existing.sent_at < ESCALATION_AFTER:
        return

    escalated = db.scalar(
        select(NotificationEvent).where(
            NotificationEvent.host_id == host.id,
            NotificationEvent.username == username,
            NotificationEvent.event_type == event_type,
            NotificationEvent.event_key == escalated_key,
        )
    )
    if escalated is not None:
        return

    escalation_subject = f'[{host.name}/{host.address}] Unresolved for 1 day - {event_type}'
    escalation_body = (
        f'User {username} still has an unresolved issue on {host.name} ({host.address}) after 1 day.\n\n'
        f'Current reason:\n- {reason}\n'
    )
    if send_email(lifu_email, escalation_subject, escalation_body):
        db.add(
            NotificationEvent(
                host_id=host.id,
                username=username,
                event_type=event_type,
                event_key=escalated_key,
                sent_at=now,
            )
        )


def _clear_resolved_issue_events(db: Session, host_id: int, active_issues: set[tuple[str, str]]) -> None:
    tracked_event_types = (
        'home_user_storage_over_1_5tb',
        'gpu_count_over_8',
        'avg_util_8h_40_70',
        'avg_util_8h_below_40_killed',
    )
    existing_events = db.scalars(
        select(NotificationEvent).where(
            NotificationEvent.host_id == host_id,
            NotificationEvent.event_type.in_(tracked_event_types),
            NotificationEvent.event_key.in_(('active', 'escalated')),
        )
    ).all()
    for event in existing_events:
        if (event.username, event.event_type) not in active_issues:
            db.delete(event)


def build_notification_email(
    host_name: str,
    host_address: str,
    username: str,
    event_type: str,
    reason: str,
) -> tuple[str, str]:
    subject = f'[{host_name}/{host_address}] GPU usage action required - {event_type}'
    body = (
        f'Hello {username},\n\n'
        f'This alert was triggered on host {host_name} ({host_address}).\n'
        f'Reason:\n- {reason}\n\n'
        'Please complete this form and explain why this happened:\n'
        f'{settings.incident_form_url}\n\n'
        'Required fields in your response:\n'
        '1) Job name / experiment name\n'
        '2) Business justification\n'
        '3) Expected end time\n'
    )
    return subject, body


def cleanup_old_data(db: Session) -> None:
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=settings.retention_days)
    db.execute(delete(DailyGpuAggregate).where(DailyGpuAggregate.date < cutoff))
    db.execute(delete(DailyUserAggregate).where(DailyUserAggregate.date < cutoff))
    sample_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    db.execute(delete(UserUtilizationSample).where(UserUtilizationSample.sampled_at < sample_cutoff))
    event_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=90)
    db.execute(delete(NotificationEvent).where(NotificationEvent.sent_at < event_cutoff))
