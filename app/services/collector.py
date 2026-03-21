from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CurrentGpuStatus, DailyGpuAggregate, DailyUserAggregate, Host
from app.services.ssh_client import HostSnapshot, SshCredentials, collect_host_snapshot

settings = get_settings()


def _int_or_zero(value: int | None) -> int:
    return int(value or 0)


def _float_or_zero(value: float | None) -> float:
    return float(value or 0.0)


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


def run_collection(db: Session) -> list[str]:
    if not settings.collector_ssh_username:
        return ['Collector skipped: missing COLLECTOR_SSH_USERNAME configuration.']

    repair_null_aggregates(db)
    db.commit()

    credentials = SshCredentials(
        username=settings.collector_ssh_username,
        password=settings.collector_ssh_password,
        key_path=settings.collector_ssh_key_path,
        use_agent=bool(settings.collector_ssh_key_path and not settings.collector_ssh_password),
    )
    messages: list[str] = []
    hosts = ensure_hosts(db)
    for host in hosts:
        try:
            snapshot = collect_host_snapshot(host.name, host.address, credentials)
            upsert_snapshot(db, host, snapshot)
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

        daily_gpu = db.scalar(
            select(DailyGpuAggregate).where(
                DailyGpuAggregate.host_id == host.id,
                DailyGpuAggregate.gpu_index == record.gpu_index,
                DailyGpuAggregate.date == sample_date,
            )
        )
        if daily_gpu is None:
            daily_gpu = DailyGpuAggregate(host_id=host.id, gpu_index=record.gpu_index, gpu_name=record.gpu_name, date=sample_date)
            db.add(daily_gpu)
        daily_gpu.gpu_name = record.gpu_name
        daily_gpu.samples = _int_or_zero(daily_gpu.samples) + 1
        daily_gpu.total_utilization = _float_or_zero(daily_gpu.total_utilization) + record.utilization_gpu
        daily_gpu.total_memory_used_mb = _float_or_zero(daily_gpu.total_memory_used_mb) + record.memory_used_mb
        daily_gpu.busy_samples = _int_or_zero(daily_gpu.busy_samples)
        daily_gpu.non_idle_samples = _int_or_zero(daily_gpu.non_idle_samples)
        if record.process_count > 0:
            daily_gpu.busy_samples = _int_or_zero(daily_gpu.busy_samples) + 1
        if not is_idle:
            daily_gpu.non_idle_samples = _int_or_zero(daily_gpu.non_idle_samples) + 1

        for username in record.active_users:
            if username in settings.excluded_users:
                continue
            daily_user = db.scalar(
                select(DailyUserAggregate).where(
                    DailyUserAggregate.host_id == host.id,
                    DailyUserAggregate.username == username,
                    DailyUserAggregate.date == sample_date,
                )
            )
            if daily_user is None:
                daily_user = DailyUserAggregate(host_id=host.id, username=username, date=sample_date)
                db.add(daily_user)
            daily_user.gpu_samples = _int_or_zero(daily_user.gpu_samples) + 1
            daily_user.total_utilization = _float_or_zero(daily_user.total_utilization) + record.utilization_gpu
            daily_user.non_idle_samples = _int_or_zero(daily_user.non_idle_samples)
            if not is_idle:
                daily_user.non_idle_samples = _int_or_zero(daily_user.non_idle_samples) + 1


def cleanup_old_data(db: Session) -> None:
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=settings.retention_days)
    db.execute(delete(DailyGpuAggregate).where(DailyGpuAggregate.date < cutoff))
    db.execute(delete(DailyUserAggregate).where(DailyUserAggregate.date < cutoff))
