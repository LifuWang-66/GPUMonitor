from datetime import datetime
from pydantic import BaseModel, Field


class CredentialCheckRequest(BaseModel):
    username: str = Field(min_length=1)
    email: str | None = None
    password: str | None = None
    use_agent: bool = False


class HostAccessResult(BaseModel):
    name: str
    address: str
    accessible: bool
    reason: str | None = None


class SessionResponse(BaseModel):
    username: str
    email: str | None = None
    accessible_hosts: list[str]


class TestEmailRequest(BaseModel):
    to_email: str | None = None
    subject: str | None = None
    body: str | None = None
    cc_lifu: bool = True


class TestEmailResponse(BaseModel):
    success: bool
    to_email: str
    cc_email: str | None = None
    detail: str


class TestPolicyEmailRequest(BaseModel):
    username: str = Field(min_length=1)
    host_address: str = Field(min_length=1)
    simulated_max_utilization: float = 55.0
    cc_lifu: bool = True


class TestPolicyEmailResponse(BaseModel):
    success: bool
    username: str
    to_email: str
    cc_email: str | None = None
    host_address: str
    host_name: str
    simulated_max_utilization: float
    detail: str


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


class UserServerBreakdown(BaseModel):
    gpu_type: str
    gpu_hours: float
    non_idle_hours: float
    average_gpu_utilization: float
    daily_average_gpu_hours: float


class UserSummaryResponse(BaseModel):
    username: str
    host_names: list[str]
    host_addresses: list[str]
    gpu_hours: float
    non_idle_hours: float
    average_gpu_utilization: float
    daily_average_gpu_hours: float
    server_breakdown: list[UserServerBreakdown]


class KillCandidateResponse(BaseModel):
    id: int
    username: str
    host_name: str
    host_address: str
    utilization_gpu: float
    memory_used_mb: float
    first_detected_at: datetime
    last_seen_at: datetime
    kill_after_at: datetime
    extension_hours: int | None = None
    extension_reason: str | None = None
    extension_expires_at: datetime | None = None
    total_running_hours: float


class KillExtensionRequest(BaseModel):
    hours: int = Field(description='Allowed values: 4, 8, 12, 24')
    reason: str = Field(min_length=3, max_length=1000)
