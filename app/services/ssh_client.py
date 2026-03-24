from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import paramiko

from app.config import get_settings

settings = get_settings()

GPU_QUERY = (
    'nvidia-smi --query-gpu=index,name,uuid,utilization.gpu,memory.used,memory.total,temperature.gpu '
    '--format=csv,noheader,nounits'
)
PROCESS_QUERY = (
    'nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory '
    '--format=csv,noheader,nounits || true'
)
PID_USER_QUERY = (
    "python3 - <<'PY'\n"
    'import json, subprocess\n'
    'cmd = "ps -eo pid=,user="\n'
    'rows = subprocess.check_output(cmd, shell=True, text=True).splitlines()\n'
    'mapping = {}\n'
    'for row in rows:\n'
    '    parts = row.strip().split(None, 1)\n'
    '    if len(parts) == 2:\n'
    '        mapping[parts[0]] = parts[1]\n'
    'print(json.dumps(mapping))\n'
    'PY'
)
HOME_USERS_QUERY = 'ls /home'
STORAGE_QUERY = "df -B1 /home | awk 'NR==2 {print $3}'"
HOME_USER_USAGE_QUERY = "du -sb /home/* 2>/dev/null || true"


@dataclass
class SshCredentials:
    username: str
    password: str | None = None
    key_path: str | None = None
    use_agent: bool = False


@dataclass
class GpuRecord:
    gpu_index: int
    gpu_name: str
    gpu_uuid: str
    utilization_gpu: float
    memory_used_mb: float
    memory_total_mb: float
    temperature_c: float | None
    active_users: list[str]
    process_count: int


@dataclass
class HostSnapshot:
    host_name: str
    host_address: str
    collected_at: datetime
    gpu_records: list[GpuRecord]
    storage_used_bytes: int = 0
    home_user_used_bytes: dict[str, int] | None = None


class RemoteCollectorError(RuntimeError):
    pass


def _connect(host: str, credentials: SshCredentials) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connection_kwargs = {
        'hostname': host,
        'port': settings.collector_ssh_port,
        'username': credentials.username,
        'password': credentials.password,
        'allow_agent': credentials.use_agent,
        'look_for_keys': credentials.use_agent,
        'timeout': settings.ssh_connect_timeout_seconds,
    }
    if credentials.key_path:
        connection_kwargs['key_filename'] = credentials.key_path
    client.connect(**connection_kwargs)
    return client


def execute_command(host: str, credentials: SshCredentials, command: str) -> str:
    client = _connect(host, credentials)
    try:
        _, stdout, stderr = client.exec_command(command)
        exit_code = stdout.channel.recv_exit_status()
        output = stdout.read().decode().strip()
        error = stderr.read().decode().strip()
        if exit_code != 0 and error:
            raise RemoteCollectorError(error)
        return output
    finally:
        client.close()


def validate_host_access(host: str, credentials: SshCredentials) -> tuple[bool, str | None]:
    try:
        execute_command(host, credentials, 'echo ok')
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def fetch_home_users(host: str, credentials: SshCredentials) -> list[str]:
    output = execute_command(host, credentials, HOME_USERS_QUERY)
    return [line.strip() for line in output.splitlines() if line.strip()]


def kill_user_gpu_processes(host: str, credentials: SshCredentials, username: str) -> str:
    sudo_password = credentials.password or ''
    command = (
        "python3 - <<'PY'\n"
        "import subprocess\n"
        "target = " + repr(username) + "\n"
        "sudo_password = " + repr(sudo_password) + "\n"
        "out = subprocess.check_output('nvidia-smi --query-compute-apps=pid --format=csv,noheader,nounits || true', shell=True, text=True)\n"
        "pids = [p.strip() for p in out.splitlines() if p.strip().isdigit()]\n"
        "killed = []\n"
        "for pid in pids:\n"
        "    try:\n"
        "        owner = subprocess.check_output(f'ps -o user= -p {pid}', shell=True, text=True).strip()\n"
        "    except Exception:\n"
        "        continue\n"
        "    if owner != target:\n"
        "        continue\n"
        "    try:\n"
        "        subprocess.check_call(['kill', '-9', pid])\n"
        "        killed.append(pid)\n"
        "        continue\n"
        "    except Exception:\n"
        "        pass\n"
        "    try:\n"
        "        if not sudo_password:\n"
        "            continue\n"
        "        subprocess.run(['sudo', '-S', 'kill', '-9', pid], input=(sudo_password + '\\n').encode(), check=True)\n"
        "        killed.append(pid)\n"
        "    except Exception:\n"
        "        pass\n"
        "print(','.join(killed))\n"
        "PY"
    )
    return execute_command(host, credentials, command)


def collect_host_snapshot(host_name: str, host_address: str, credentials: SshCredentials) -> HostSnapshot:
    gpu_output = execute_command(host_address, credentials, GPU_QUERY)
    process_output = execute_command(host_address, credentials, PROCESS_QUERY)
    pid_users_raw = execute_command(host_address, credentials, PID_USER_QUERY)
    storage_used_raw = execute_command(host_address, credentials, STORAGE_QUERY)
    home_user_usage_raw = execute_command(host_address, credentials, HOME_USER_USAGE_QUERY)
    pid_users = json.loads(pid_users_raw or '{}')
    storage_used_bytes = int(storage_used_raw or 0)
    home_user_used_bytes: dict[str, int] = {}
    for row in home_user_usage_raw.splitlines():
        parts = row.strip().split(None, 1)
        if len(parts) != 2:
            continue
        size_text, path = parts
        if not size_text.isdigit():
            continue
        username = path.rstrip('/').split('/')[-1]
        home_user_used_bytes[username] = int(size_text)

    uuid_to_users: dict[str, list[str]] = {}
    uuid_to_count: dict[str, int] = {}
    for row in process_output.splitlines():
        parts = [part.strip() for part in row.split(',')]
        if len(parts) < 2:
            continue
        gpu_uuid = parts[0]
        pid = parts[1]
        username = pid_users.get(pid)
        if username:
            uuid_to_users.setdefault(gpu_uuid, [])
            if username not in uuid_to_users[gpu_uuid]:
                uuid_to_users[gpu_uuid].append(username)
        uuid_to_count[gpu_uuid] = uuid_to_count.get(gpu_uuid, 0) + 1

    gpu_records: list[GpuRecord] = []
    for row in gpu_output.splitlines():
        parts = [part.strip() for part in row.split(',')]
        if len(parts) != 7:
            continue
        gpu_uuid = parts[2]
        gpu_records.append(
            GpuRecord(
                gpu_index=int(parts[0]),
                gpu_name=parts[1],
                gpu_uuid=gpu_uuid,
                utilization_gpu=float(parts[3] or 0),
                memory_used_mb=float(parts[4] or 0),
                memory_total_mb=float(parts[5] or 0),
                temperature_c=float(parts[6]) if parts[6] not in {'', 'N/A'} else None,
                active_users=uuid_to_users.get(gpu_uuid, []),
                process_count=uuid_to_count.get(gpu_uuid, 0),
            )
        )

    return HostSnapshot(
        host_name=host_name,
        host_address=host_address,
        collected_at=datetime.now(timezone.utc),
        gpu_records=gpu_records,
        storage_used_bytes=storage_used_bytes,
        home_user_used_bytes=home_user_used_bytes,
    )
