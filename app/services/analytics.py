from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CurrentGpuStatus, DailyGpuAggregate, DailyUserAggregate, Host
from app.schemas import CurrentGpuResponse, GpuSummaryResponse, TrendPoint, UserServerBreakdown, UserSummaryResponse
from app.services.ssh_client import HostSnapshot

settings = get_settings()
ADMIN_USERNAMES = {'lifu', 'panzhou'}


def snapshot_to_current_status(snapshot: HostSnapshot) -> list[CurrentGpuResponse]:
    return [
        CurrentGpuResponse(
            host_name=snapshot.host_name,
            host_address=snapshot.host_address,
            gpu_index=record.gpu_index,
            gpu_name=record.gpu_name,
            utilization_gpu=record.utilization_gpu,
            memory_used_mb=record.memory_used_mb,
            memory_total_mb=record.memory_total_mb,
            temperature_c=record.temperature_c,
            active_users=record.active_users,
            process_count=record.process_count,
            is_idle=record.utilization_gpu < 10.0,
            last_seen_at=snapshot.collected_at.replace(tzinfo=None),
        )
        for record in snapshot.gpu_records
    ]


def get_current_status(db: Session, allowed_hosts: list[str]) -> list[CurrentGpuResponse]:
    if not allowed_hosts:
        return []
    rows = db.execute(
        select(CurrentGpuStatus, Host)
        .join(Host, CurrentGpuStatus.host_id == Host.id)
        .where(Host.address.in_(allowed_hosts))
        .order_by(Host.address, CurrentGpuStatus.gpu_index)
    ).all()
    return [
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
        for status, host in rows
    ]


def get_gpu_history(db: Session, allowed_hosts: list[str], days: int) -> list[GpuSummaryResponse]:
    if not allowed_hosts:
        return []
    since = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    rows = db.execute(
        select(DailyGpuAggregate, Host)
        .join(Host, DailyGpuAggregate.host_id == Host.id)
        .where(Host.address.in_(allowed_hosts), DailyGpuAggregate.date >= since)
        .order_by(Host.address, DailyGpuAggregate.gpu_index, DailyGpuAggregate.date)
    ).all()

    grouped: dict[tuple[str, int], list[tuple[DailyGpuAggregate, Host]]] = defaultdict(list)
    for daily, host in rows:
        grouped[(host.address, daily.gpu_index)].append((daily, host))

    results: list[GpuSummaryResponse] = []
    for entries in grouped.values():
        first_daily, host = entries[0]
        samples = sum((item.samples or 0) for item, _ in entries)
        busy_samples = sum((item.busy_samples or 0) for item, _ in entries)
        non_idle_samples = sum((item.non_idle_samples or 0) for item, _ in entries)
        total_util = sum((item.total_utilization or 0) for item, _ in entries)
        total_memory = sum((item.total_memory_used_mb or 0) for item, _ in entries)
        trend = []
        for item, _ in entries:
            sample_count = item.samples or 1
            trend.append(
                TrendPoint(
                    label=item.date.isoformat(),
                    occupancy_rate=round((item.busy_samples or 0) / sample_count * 100, 2),
                    effective_utilization_rate=round((item.non_idle_samples or 0) / sample_count * 100, 2),
                    average_gpu_utilization=round((item.total_utilization or 0) / sample_count, 2),
                )
            )
        sample_count = samples or 1
        results.append(
            GpuSummaryResponse(
                host_name=host.name,
                host_address=host.address,
                gpu_index=first_daily.gpu_index,
                gpu_name=first_daily.gpu_name,
                occupancy_rate=round(busy_samples / sample_count * 100, 2),
                effective_utilization_rate=round(non_idle_samples / sample_count * 100, 2),
                average_gpu_utilization=round(total_util / sample_count, 2),
                average_memory_used_mb=round(total_memory / sample_count, 2),
                trend=trend,
            )
        )
    return results


def get_user_history(db: Session, allowed_hosts: list[str], days: int, viewer_username: str) -> list[UserSummaryResponse]:
    if not allowed_hosts or not viewer_username:
        return []

    since = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    rows = db.execute(
        select(DailyUserAggregate, Host)
        .join(Host, DailyUserAggregate.host_id == Host.id)
        .where(Host.address.in_(allowed_hosts), DailyUserAggregate.date >= since)
        .order_by(DailyUserAggregate.username, Host.address)
    ).all()

    sample_hours = settings.collector_interval_minutes / 60
    is_admin = viewer_username in ADMIN_USERNAMES
    host_gpu_type_map = _get_host_gpu_type_map(db, allowed_hosts)

    grouped: dict[str, dict] = defaultdict(
        lambda: {
            'host_names': [],
            'host_addresses': [],
            'active_days': set(),
            'gpu_samples': 0,
            'non_idle_samples': 0,
            'total_utilization': 0.0,
            'server_breakdown': defaultdict(
                lambda: {
                    'gpu_type': 'Unknown model',
                    'active_days': set(),
                    'gpu_samples': 0,
                    'non_idle_samples': 0,
                    'total_utilization': 0.0,
                }
            ),
        }
    )

    for daily, host in rows:
        if not is_admin and daily.username != viewer_username:
            continue

        gpu_type = host_gpu_type_map.get(host.address, 'Unknown model')
        bucket = grouped[daily.username]
        if host.name not in bucket['host_names']:
            bucket['host_names'].append(host.name)
        if host.address not in bucket['host_addresses']:
            bucket['host_addresses'].append(host.address)

        gpu_samples = daily.gpu_samples or 0
        non_idle_samples = daily.non_idle_samples or 0
        total_utilization = daily.total_utilization or 0.0

        bucket['gpu_samples'] += gpu_samples
        bucket['non_idle_samples'] += non_idle_samples
        bucket['total_utilization'] += total_utilization
        bucket['active_days'].add(daily.date)

        server_item = bucket['server_breakdown'][gpu_type]
        server_item['gpu_type'] = gpu_type
        server_item['gpu_samples'] += gpu_samples
        server_item['non_idle_samples'] += non_idle_samples
        server_item['total_utilization'] += total_utilization
        server_item['active_days'].add(daily.date)

    results: list[UserSummaryResponse] = []
    for username, item in sorted(grouped.items(), key=lambda entry: (-entry[1]['gpu_samples'], entry[0])):
        server_breakdown = [
            UserServerBreakdown(
                gpu_type=server['gpu_type'],
                gpu_hours=round(server['gpu_samples'] * sample_hours, 2),
                non_idle_hours=round(server['non_idle_samples'] * sample_hours, 2),
                average_gpu_utilization=round(server['total_utilization'] / (server['gpu_samples'] or 1), 2),
                daily_average_gpu_hours=round((server['gpu_samples'] * sample_hours) / max(len(server['active_days']), 1), 2),
            )
            for _, server in sorted(item['server_breakdown'].items())
        ]
        total_gpu_hours = round(item['gpu_samples'] * sample_hours, 2)
        active_day_count = max(len(item['active_days']), 1)
        results.append(
            UserSummaryResponse(
                username=username,
                host_names=sorted(item['host_names']),
                host_addresses=sorted(item['host_addresses']),
                gpu_hours=total_gpu_hours,
                non_idle_hours=round(item['non_idle_samples'] * sample_hours, 2),
                average_gpu_utilization=round(item['total_utilization'] / (item['gpu_samples'] or 1), 2),
                daily_average_gpu_hours=round(total_gpu_hours / active_day_count, 2),
                server_breakdown=server_breakdown,
            )
        )
    return results


def _get_host_gpu_type_map(db: Session, allowed_hosts: list[str]) -> dict[str, str]:
    host_gpu_type_map: dict[str, str] = {}
    rows = db.execute(
        select(CurrentGpuStatus.gpu_name, Host.address)
        .join(Host, CurrentGpuStatus.host_id == Host.id)
        .where(Host.address.in_(allowed_hosts))
        .order_by(Host.address, CurrentGpuStatus.gpu_index)
    ).all()
    for gpu_name, address in rows:
        if address in host_gpu_type_map:
            continue
        host_gpu_type_map[address] = _normalize_gpu_type(gpu_name)

    daily_rows = db.execute(
        select(DailyGpuAggregate.gpu_name, Host.address)
        .join(Host, DailyGpuAggregate.host_id == Host.id)
        .where(Host.address.in_(allowed_hosts))
        .order_by(DailyGpuAggregate.date.desc(), Host.address, DailyGpuAggregate.gpu_index)
    ).all()
    for gpu_name, address in daily_rows:
        if address in host_gpu_type_map:
            continue
        host_gpu_type_map[address] = _normalize_gpu_type(gpu_name)

    for address in allowed_hosts:
        host_gpu_type_map.setdefault(address, 'Unknown model')
    return host_gpu_type_map


def _normalize_gpu_type(gpu_name: str) -> str:
    upper = (gpu_name or '').upper()
    if 'L40S' in upper:
        return 'NVIDIA L40S'
    if 'RTX PRO 6000' in upper:
        return 'NVIDIA RTX Pro 6000'
    return gpu_name or 'Unknown model'
