from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Host(Base):
    __tablename__ = 'hosts'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    address: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    current_statuses = relationship('CurrentGpuStatus', back_populates='host', cascade='all, delete-orphan')


class UserProfile(Base):
    __tablename__ = 'user_profiles'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class CurrentGpuStatus(Base):
    __tablename__ = 'current_gpu_statuses'
    __table_args__ = (UniqueConstraint('host_id', 'gpu_index', name='uq_current_status_host_gpu'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey('hosts.id'), nullable=False, index=True)
    gpu_index: Mapped[int] = mapped_column(Integer, nullable=False)
    gpu_name: Mapped[str] = mapped_column(String(200), nullable=False)
    gpu_uuid: Mapped[str] = mapped_column(String(200), nullable=False)
    utilization_gpu: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    memory_used_mb: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    memory_total_mb: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    active_users: Mapped[str] = mapped_column(String(500), default='', nullable=False)
    process_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_idle: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
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
    samples: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    busy_samples: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    non_idle_samples: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_utilization: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    total_memory_used_mb: Mapped[float] = mapped_column(Float, default=0, nullable=False)


class DailyUserAggregate(Base):
    __tablename__ = 'daily_user_aggregates'
    __table_args__ = (UniqueConstraint('host_id', 'username', 'date', name='uq_daily_user_host_user_date'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey('hosts.id'), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    gpu_samples: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    non_idle_samples: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_utilization: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    total_memory_used_mb: Mapped[float] = mapped_column(Float, default=0, nullable=False)


class UserStorageUsage(Base):
    __tablename__ = 'user_storage_usage'
    __table_args__ = (UniqueConstraint('host_id', 'username', name='uq_user_storage_host_user'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey('hosts.id'), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class UserUtilizationSample(Base):
    __tablename__ = 'user_utilization_samples'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey('hosts.id'), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    sampled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    average_gpu_utilization: Mapped[float] = mapped_column(Float, default=0, nullable=False)


class ProcessUtilizationSample(Base):
    __tablename__ = 'process_utilization_samples'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey('hosts.id'), nullable=False, index=True)
    pid: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    gpu_index: Mapped[int] = mapped_column(Integer, nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    gpu_utilization: Mapped[float] = mapped_column(Float, default=0, nullable=False)


class NotificationEvent(Base):
    __tablename__ = 'notification_events'
    __table_args__ = (UniqueConstraint('host_id', 'username', 'event_type', 'event_key', name='uq_notification_dedupe'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey('hosts.id'), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class EmailOutbox(Base):
    __tablename__ = 'email_outbox'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    to_email: Mapped[str] = mapped_column(String(320), nullable=False)
    cc_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(String(5000), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default='pending', nullable=False, index=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
