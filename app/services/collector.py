from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
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
EIGHT_HOURS = timedelta(hours=8)


def get_collector_credentials() -> SshCredentials | None:
    if not settings.collector_ssh_username:
        return None
    return SshCredentials(
        username=settings.collector_ssh_username,
        password=settings.collector_ssh_password,
        key_path=settings.collector_ssh_key_path,
        use_agent=bool(settings.collector_ssh_key_path and not settings.collector_ssh_password),
    )


def repair_null_aggregates(db: Session) -> None:
    db.execute(
        update(DailyGpuAggregate).values(
            samples=func.coalesce(DailyGpuAggregate.samples, 0),
            busy_samples=func.coalesce(DailyGpuAggregate.busy_samples, 0),
            non_idle_samples=func.coalesce(DailyGpuAggregate.non_idle_samples, 0),
            total_utilization=func.coalesce(DailyGpuAggregate.total_utilization, 0),
            total_memory_used_mb=func.coalesce(DailyGpuAggregate.total_memory_used_mb, 0),
        )
    )
    db.execute(
        update(DailyUserAggregate).values(
            gpu_samples=func.coalesce(DailyUserAggregate.gpu_samples, 0),
            non_idle_samples=func.coalesce(DailyUserAggregate.non_idle_samples, 0),
            total_utilization=func.coalesce(DailyUserAggregate.total_utilization, 0),
        )
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


def run_collection(db: Session) -> list[str]:
    credentials = get_collector_credentials()
    if credentials is None:
        return ['Collector skipped: missing COLLECTOR_SSH_USERNAME configuration.']

    repair_null_aggregates(db)
    db.commit()

    messages: list[str] = []
    hosts = ensure_hosts(db)
    for host in hosts:
        try:
            snapshot = collect_host_snapshot(host.name, host.address, credentials)
            upsert_snapshot(db, host, snapshot)
            _evaluate_and_handle_user_alerts(db, host, snapshot, credentials)
            messages.append(f'Collected {host.address}')
        except Exception as exc:  # noqa: BLE001
            messages.append(f'Failed {host.address}: {exc}')
    cleanup_old_data(db)
    db.commit()
    return messages


def upsert_snapshot(db: Session, host: Host, snapshot: HostSnapshot) -> None:
    sample_date = snapshot.collected_at.date()
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

    for username, used_bytes in (snapshot.home_user_used_bytes or {}).items():
        if used_bytes <= STORAGE_THRESHOLD_BYTES:
            continue
        profile = db.scalar(select(UserProfile).where(UserProfile.username == username))
        if not profile or not profile.email:
            continue
        event_time = snapshot.collected_at.replace(tzinfo=None)
        used_tb = used_bytes / 1024 / 1024 / 1024 / 1024
        _notify_once(
            db,
            host,
            username,
            profile.email,
            event_type='home_user_storage_over_1_5tb',
            event_key=event_time.strftime('%Y-%m-%d'),
            reason=f'/home/{username} usage is {used_tb:.2f} TB, which exceeds the 1.5 TB threshold.',
            cc_email=cc_email,
        )

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

        eight_hour_avg = _get_eight_hour_avg_util(db, host.id, username, snapshot.collected_at.replace(tzinfo=None))
        event_time = snapshot.collected_at.replace(tzinfo=None)

        if gpu_count > HIGH_GPU_COUNT_THRESHOLD:
            _notify_once(
                db,
                host,
                username,
                profile.email,
                event_type='gpu_count_over_8',
                event_key=event_time.strftime('%Y-%m-%d'),
                reason=(
                    f'You are using {gpu_count} GPUs on this host. More than 8 GPUs can heavily impact fair-share '
                    'capacity and block other users from scheduling jobs.'
                ),
                cc_email=cc_email,
            )

        if LOW_UTIL_THRESHOLD <= eight_hour_avg <= MID_UTIL_THRESHOLD:
            _notify_once(
                db,
                host,
                username,
                profile.email,
                event_type='avg_util_8h_40_70',
                event_key=event_time.strftime('%Y-%m-%d-%H'),
                reason=f'Your 8-hour average GPU utilization is {eight_hour_avg:.2f}% (between 40% and 70%).',
                cc_email=cc_email,
            )
        elif eight_hour_avg < LOW_UTIL_THRESHOLD and eight_hour_avg >= 0:
            kill_result = kill_user_gpu_processes(host.address, credentials, username)
            _notify_once(
                db,
                host,
                username,
                profile.email,
                event_type='avg_util_8h_below_40_killed',
                event_key=event_time.strftime('%Y-%m-%d-%H'),
                reason=(
                    f'Your 8-hour average GPU utilization is {eight_hour_avg:.2f}% (below 40%). '
                    f'GPU processes were terminated. Killed PIDs: {kill_result or "none"}'
                ),
                cc_email=cc_email,
            )


def _get_eight_hour_avg_util(db: Session, host_id: int, username: str, now_naive: datetime) -> float:
    since = now_naive - EIGHT_HOURS
    rows = db.scalars(
        select(UserUtilizationSample.average_gpu_utilization).where(
            UserUtilizationSample.host_id == host_id,
            UserUtilizationSample.username == username,
            UserUtilizationSample.sampled_at >= since,
        )
    ).all()
    if not rows:
        return -1.0
    return float(sum(rows) / len(rows))


def _notify_once(
    db: Session,
    host: Host,
    username: str,
    email: str,
    event_type: str,
    event_key: str,
    reason: str,
    cc_email: str | None = None,
) -> None:
    existing = db.scalar(
        select(NotificationEvent).where(
            NotificationEvent.host_id == host.id,
            NotificationEvent.username == username,
            NotificationEvent.event_type == event_type,
            NotificationEvent.event_key == event_key,
        )
    )
    if existing:
        return

    subject = f'[{host.name}/{host.address}] GPU usage action required - {event_type}'
    body = (
        f'Hello {username},\n\n'
        f'This alert was triggered on host {host.name} ({host.address}).\n'
        f'Reason:\n- {reason}\n\n'
        'Please complete this form and explain why this happened:\n'
        f'{settings.incident_form_url}\n\n'
        'Required fields in your response:\n'
        '1) Job name / experiment name\n'
        '2) Business justification\n'
        '3) Expected end time\n'
        '4) Mitigation plan\n'
    )
    if send_email(email, subject, body, cc_email=cc_email):
        db.add(
            NotificationEvent(
                host_id=host.id,
                username=username,
                event_type=event_type,
                event_key=event_key,
                sent_at=datetime.utcnow(),
            )
        )


def cleanup_old_data(db: Session) -> None:
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=settings.retention_days)
    db.execute(delete(DailyGpuAggregate).where(DailyGpuAggregate.date < cutoff))
    db.execute(delete(DailyUserAggregate).where(DailyUserAggregate.date < cutoff))
    sample_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    db.execute(delete(UserUtilizationSample).where(UserUtilizationSample.sampled_at < sample_cutoff))
    event_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=90)
    db.execute(delete(NotificationEvent).where(NotificationEvent.sent_at < event_cutoff))
