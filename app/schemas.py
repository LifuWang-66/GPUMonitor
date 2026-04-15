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
    average_memory_used_mb: float
    daily_average_gpu_hours: float


class UserSummaryResponse(BaseModel):
    username: str
    host_names: list[str]
    host_addresses: list[str]
    gpu_hours: float
    non_idle_hours: float
    average_gpu_utilization: float
    average_memory_used_mb: float
    daily_average_gpu_hours: float
    server_breakdown: list[UserServerBreakdown]


class UserStorageHostItem(BaseModel):
    host_name: str
    host_address: str
    used_bytes: int
    updated_at: datetime | None = None


class UserStorageSummary(BaseModel):
    username: str
    total_used_bytes: int
    server_breakdown: list[UserStorageHostItem]


class EmailOutboxItem(BaseModel):
    id: int
    to_email: str
    cc_email: str | None = None
    subject: str
    body: str
    status: str
    created_at: datetime


class EmailOutboxMarkRequest(BaseModel):
    error_message: str | None = None


class EmailOutboxMarkResponse(BaseModel):
    id: int
    status: str
    detail: str


class JobKillCandidateItem(BaseModel):
    id: int
    host_name: str
    host_address: str
    pid: int
    username: str
    gpu_index: int
    utilization_gpu: float
    memory_used_mb: float
    status: str
    kill_after: datetime
    extended_until: datetime | None = None
    extension_hours: int | None = None
    extension_reason: str | None = None
    first_seen_at: datetime
    total_running_hours: float


class JobExtensionRequest(BaseModel):
    hours: int
    reason: str = Field(min_length=1, max_length=2000)


class JobKillResponse(BaseModel):
    id: int
    status: str
    detail: str
