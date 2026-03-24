from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CurrentGpuStatus, DailyGpuAggregate, DailyUserAggregate, Host
from app.schemas import CurrentGpuResponse, GpuSummaryResponse, TrendPoint, UserSummaryResponse

settings = get_settings()


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
                    occupancy_rate=round(item.busy_samples / sample_count * 100, 2),
                    effective_utilization_rate=round(item.non_idle_samples / sample_count * 100, 2),
                    average_gpu_utilization=round(item.total_utilization / sample_count, 2),
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


def get_user_history(db: Session, allowed_hosts: list[str], days: int) -> list[UserSummaryResponse]:
    if not allowed_hosts:
        return []

    since = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    rows = db.execute(
        select(DailyUserAggregate, Host)
        .join(Host, DailyUserAggregate.host_id == Host.id)
        .where(
            Host.address.in_(allowed_hosts),
            DailyUserAggregate.date >= since,
        )
        .order_by(DailyUserAggregate.username, Host.address)
    ).all()

    sample_hours = settings.collector_interval_minutes / 60
    grouped: dict[tuple[str, str], dict] = {}

    for daily, host in rows:
        key = (daily.username, host.address)
        if key not in grouped:
            grouped[key] = {
                'username': daily.username,
                'host_name': host.name,
                'host_address': host.address,
                'gpu_samples': 0,
                'non_idle_samples': 0,
                'total_utilization': 0.0,
            }

        grouped[key]['gpu_samples'] += daily.gpu_samples or 0
        grouped[key]['non_idle_samples'] += daily.non_idle_samples or 0
        grouped[key]['total_utilization'] += daily.total_utilization or 0.0

    return [
        UserSummaryResponse(
            username=item['username'],
            host_name=item['host_name'],
            host_address=item['host_address'],
            gpu_hours=round(item['gpu_samples'] * sample_hours, 2),
            non_idle_hours=round(item['non_idle_samples'] * sample_hours, 2),
            average_gpu_utilization=round(
                item['total_utilization'] / (item['gpu_samples'] or 1), 2
            ),
        )
        for item in grouped.values()
    ]