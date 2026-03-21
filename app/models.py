from datetime import datetime, date

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Host(Base):
    __tablename__ = 'hosts'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    address: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    current_statuses = relationship('CurrentGpuStatus', back_populates='host', cascade='all, delete-orphan')


class CurrentGpuStatus(Base):
    __tablename__ = 'current_gpu_statuses'
    __table_args__ = (UniqueConstraint('host_id', 'gpu_index', name='uq_current_status_host_gpu'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey('hosts.id'), nullable=False, index=True)
    gpu_index: Mapped[int] = mapped_column(Integer, nullable=False)
    gpu_name: Mapped[str] = mapped_column(String(200), nullable=False)
    gpu_uuid: Mapped[str] = mapped_column(String(200), nullable=False)
    utilization_gpu: Mapped[float] = mapped_column(Float, default=0)
    memory_used_mb: Mapped[float] = mapped_column(Float, default=0)
    memory_total_mb: Mapped[float] = mapped_column(Float, default=0)
    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    active_users: Mapped[str] = mapped_column(String(500), default='')
    process_count: Mapped[int] = mapped_column(Integer, default=0)
    is_idle: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    host = relationship('Host', back_populates='current_statuses')


class DailyGpuAggregate(Base):
    __tablename__ = 'daily_gpu_aggregates'
    __table_args__ = (UniqueConstraint('host_id', 'gpu_index', 'date', name='uq_daily_gpu_host_gpu_date'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey('hosts.id'), nullable=False, index=True)
    gpu_index: Mapped[int] = mapped_column(Integer, nullable=False)
    gpu_name: Mapped[str] = mapped_column(String(200), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    samples: Mapped[int] = mapped_column(Integer, default=0)
    busy_samples: Mapped[int] = mapped_column(Integer, default=0)
    non_idle_samples: Mapped[int] = mapped_column(Integer, default=0)
    total_utilization: Mapped[float] = mapped_column(Float, default=0)
    total_memory_used_mb: Mapped[float] = mapped_column(Float, default=0)


class DailyUserAggregate(Base):
    __tablename__ = 'daily_user_aggregates'
    __table_args__ = (UniqueConstraint('host_id', 'username', 'date', name='uq_daily_user_host_user_date'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey('hosts.id'), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    gpu_samples: Mapped[int] = mapped_column(Integer, default=0)
    non_idle_samples: Mapped[int] = mapped_column(Integer, default=0)
    total_utilization: Mapped[float] = mapped_column(Float, default=0)
