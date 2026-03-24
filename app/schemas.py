from datetime import datetime
from pydantic import BaseModel, Field


class CredentialCheckRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str | None = None
    use_agent: bool = False


class HostAccessResult(BaseModel):
    name: str
    address: str
    accessible: bool
    reason: str | None = None


class SessionResponse(BaseModel):
    username: str
    accessible_hosts: list[str]


class CurrentGpuResponse(BaseModel):
    host_name: str
    host_address: str
    gpu_index: int
    gpu_name: str
    utilization_gpu: float
    memory_used_mb: float
    memory_total_mb: float
    temperature_c: float | None
    active_users: list[str]
    process_count: int
    is_idle: bool
    last_seen_at: datetime


class TrendPoint(BaseModel):
    label: str
    occupancy_rate: float
    effective_utilization_rate: float
    average_gpu_utilization: float


class GpuSummaryResponse(BaseModel):
    host_name: str
    host_address: str
    gpu_index: int
    gpu_name: str
    occupancy_rate: float
    effective_utilization_rate: float
    average_gpu_utilization: float
    average_memory_used_mb: float
    trend: list[TrendPoint]


class UserSummaryResponse(BaseModel):
    username: str
    host_name: str
    host_address: str
    gpu_hours: float
    non_idle_hours: float
    average_gpu_utilization: float
