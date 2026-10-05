from __future__ import annotations

import json
import threading
import time
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


HOME_USAGE_LOCK_PATH = '/tmp/gpumonitor-home-usage.lock'


def _build_home_user_usage_query(excluded_users: set[str], sudo_password: str, du_timeout_seconds: int) -> str:
    """Remote script that runs one `du` at a time over /home/<user>.

    Each `du` is wrapped in `timeout -s KILL` *inside* sudo so the kill comes from
    root and cannot orphan the `du` (killing `sudo` itself with SIGKILL would).
    A normal timeout only skips that directory. If `du` still has not exited well
    after the KILL, it is stuck in D state on disk I/O, so the scan stops instead
    of piling more `du` processes onto the same disk. A file lock plus a check
    for leftover `du` processes keeps scans from overlapping across runs.

    Output lines: OK<TAB>bytes<TAB>user, TIMEOUT<TAB>user, ERROR<TAB>user<TAB>rc,
    STUCK<TAB>user, BUSY<TAB>reason.
    """
    excluded_list = sorted(excluded_users)
    return (
        "python3 - <<'PY'\n"
        "import fcntl\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "\n"
        f"excluded = set({excluded_list!r})\n"
        f"sudo_password = {sudo_password!r}\n"
        f"du_timeout = {int(du_timeout_seconds)}\n"
        "\n"
        f"lock = open({HOME_USAGE_LOCK_PATH!r}, 'a')\n"
        "try:\n"
        "    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except OSError:\n"
        "    print('BUSY\\tprevious scan is still running')\n"
        "    sys.exit(0)\n"
        "\n"
        "leftover = subprocess.run(['pgrep', '-f', '^du -sb /home/'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)\n"
        "leftover_pids = leftover.stdout.decode().split()\n"
        "if leftover_pids:\n"
        "    print('BUSY\\tleftover du processes still running: ' + ','.join(leftover_pids))\n"
        "    sys.exit(0)\n"
        "\n"
        "use_sudo = False\n"
        "if sudo_password:\n"
        "    try:\n"
        "        use_sudo = subprocess.run(\n"
        "            ['sudo', '-S', '-p', '', 'true'],\n"
        "            input=(sudo_password + '\\n').encode(),\n"
        "            stdout=subprocess.DEVNULL,\n"
        "            stderr=subprocess.DEVNULL,\n"
        "            timeout=30,\n"
        "        ).returncode == 0\n"
        "    except Exception:\n"
        "        use_sudo = False\n"
        "\n"
        "entries = []\n"
        "for name in os.listdir('/home'):\n"
        "    if name in excluded:\n"
        "        continue\n"
        "    path = os.path.join('/home', name)\n"
        "    if not os.path.isdir(path) or os.path.islink(path):\n"
        "        continue\n"
        "    entries.append((name, path))\n"
        "\n"
        "for name, path in sorted(entries):\n"
        "    cmd = ['timeout', '-s', 'KILL', str(du_timeout), 'nice', '-n', '19', 'ionice', '-c', '2', '-n', '7', 'du', '-sb', path]\n"
        "    if use_sudo:\n"
        "        cmd = ['sudo', '-S', '-p', ''] + cmd\n"
        "    try:\n"
        "        proc = subprocess.run(\n"
        "            cmd,\n"
        "            input=(sudo_password + '\\n').encode() if use_sudo else None,\n"
        "            stdout=subprocess.PIPE,\n"
        "            stderr=subprocess.DEVNULL,\n"
        "            check=False,\n"
        "            timeout=du_timeout + 120,\n"
        "        )\n"
        "    except subprocess.TimeoutExpired:\n"
        "        print(f'STUCK\\t{name}', flush=True)\n"
        "        break\n"
        "    parts = proc.stdout.decode().split(None, 1)\n"
        "    if proc.returncode == 0 and parts and parts[0].isdigit():\n"
        "        print(f'OK\\t{parts[0]}\\t{name}', flush=True)\n"
        "    elif proc.returncode in (124, 137, -9):\n"
        "        print(f'TIMEOUT\\t{name}', flush=True)\n"
        "    else:\n"
        "        print(f'ERROR\\t{name}\\t{proc.returncode}', flush=True)\n"
        "PY"
    )


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
    active_pids: dict[int, str]  # pid -> username
    active_pid_memory_mb: dict[int, float]  # pid -> process memory


@dataclass
class HostSnapshot:
    host_name: str
    host_address: str
    collected_at: datetime
    gpu_records: list[GpuRecord]
    storage_used_bytes: int = 0


@dataclass
class HomeUsageScanResult:
    used_bytes: dict[str, int]
    timed_out_users: list[str]
    failed_users: list[str]
    stuck_user: str | None = None
    busy_reason: str | None = None
    ssh_timed_out: bool = False


class RemoteCollectorError(RuntimeError):
    pass


_COLLECTOR_CLIENTS: dict[str, paramiko.SSHClient] = {}
_COLLECTOR_CLIENT_KEYS: dict[str, tuple[str, str | None, str | None, bool]] = {}
_COLLECTOR_CLIENTS_LOCK = threading.Lock()


def _credentials_key(credentials: SshCredentials) -> tuple[str, str | None, str | None, bool]:
    return (credentials.username, credentials.password, credentials.key_path, credentials.use_agent)


def _connect(host: str, credentials: SshCredentials, timeout: int | None = None) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connection_kwargs = {
        'hostname': host,
        'port': settings.collector_ssh_port,
        'username': credentials.username,
        'password': credentials.password,
        'allow_agent': credentials.use_agent,
        'look_for_keys': credentials.use_agent,
        'timeout': timeout if timeout is not None else settings.ssh_connect_timeout_seconds,
    }
    if credentials.key_path:
        connection_kwargs['key_filename'] = credentials.key_path
    client.connect(**connection_kwargs)
    return client


def _get_or_create_collector_client(host: str, credentials: SshCredentials) -> paramiko.SSHClient:
    key = _credentials_key(credentials)
    with _COLLECTOR_CLIENTS_LOCK:
        existing = _COLLECTOR_CLIENTS.get(host)
        existing_key = _COLLECTOR_CLIENT_KEYS.get(host)
        if existing is not None and existing_key == key and existing.get_transport() and existing.get_transport().is_active():
            return existing
        if existing is not None:
            try:
                existing.close()
            except Exception:  # noqa: BLE001
                pass
        client = _connect(host, credentials, timeout=5)
        _COLLECTOR_CLIENTS[host] = client
        _COLLECTOR_CLIENT_KEYS[host] = key
        return client


def close_collector_connections() -> None:
    with _COLLECTOR_CLIENTS_LOCK:
        for client in _COLLECTOR_CLIENTS.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        _COLLECTOR_CLIENTS.clear()
        _COLLECTOR_CLIENT_KEYS.clear()


def _run_with_deadline(client: paramiko.SSHClient, command: str, timeout_seconds: float) -> tuple[int | None, str, str]:
    """Run a remote command, giving up after timeout_seconds.

    Returns (exit_code, stdout, stderr); exit_code is None when the deadline hit,
    in which case stdout/stderr hold whatever arrived before it.
    """
    channel = client.get_transport().open_session()
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    deadline = time.monotonic() + timeout_seconds
    try:
        channel.exec_command(command)
        while True:
            while channel.recv_ready():
                stdout_chunks.append(channel.recv(65536))
            while channel.recv_stderr_ready():
                stderr_chunks.append(channel.recv_stderr(65536))
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                exit_code: int | None = channel.recv_exit_status()
                break
            if time.monotonic() >= deadline:
                exit_code = None
                break
            time.sleep(0.05)
    finally:
        channel.close()
    return exit_code, b''.join(stdout_chunks).decode(errors='replace').strip(), b''.join(stderr_chunks).decode(errors='replace').strip()


def _execute_command_with_client(client: paramiko.SSHClient, command: str) -> str:
    timeout_seconds = settings.ssh_command_timeout_seconds
    exit_code, output, error = _run_with_deadline(client, command, timeout_seconds)
    if exit_code is None:
        raise RemoteCollectorError(f'Remote command timed out after {timeout_seconds}s')
    if exit_code != 0 and error:
        raise RemoteCollectorError(error)
    return output


def execute_command(host: str, credentials: SshCredentials, command: str) -> str:
    client = _connect(host, credentials)
    try:
        return _execute_command_with_client(client, command)
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


def kill_specific_gpu_processes(host: str, credentials: SshCredentials, pids: list[int]) -> str:
    if not pids:
        return ''
    sudo_password = credentials.password or ''
    pid_list_repr = repr([str(p) for p in pids])
    command = (
        "python3 - <<'PY'\n"
        "import subprocess\n"
        "pids = " + pid_list_repr + "\n"
        "sudo_password = " + repr(sudo_password) + "\n"
        "killed = []\n"
        "for pid in pids:\n"
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


def collect_host_snapshot(
    host_name: str,
    host_address: str,
    credentials: SshCredentials,
) -> HostSnapshot:
    client = _get_or_create_collector_client(host_address, credentials)
    try:
        gpu_output = _execute_command_with_client(client, GPU_QUERY)
        process_output = _execute_command_with_client(client, PROCESS_QUERY)
        pid_users_raw = _execute_command_with_client(client, PID_USER_QUERY)
        storage_used_raw = _execute_command_with_client(client, STORAGE_QUERY)
    except Exception:  # noqa: BLE001
        with _COLLECTOR_CLIENTS_LOCK:
            stale = _COLLECTOR_CLIENTS.pop(host_address, None)
            _COLLECTOR_CLIENT_KEYS.pop(host_address, None)
        if stale is not None:
            try:
                stale.close()
            except Exception:  # noqa: BLE001
                pass
        raise
    pid_users = json.loads(pid_users_raw or '{}')
    storage_used_bytes = int(storage_used_raw or 0)

    uuid_to_users: dict[str, list[str]] = {}
    uuid_to_count: dict[str, int] = {}
    uuid_to_pids: dict[str, dict[int, str]] = {}
    uuid_to_pid_memory: dict[str, dict[int, float]] = {}
    for row in process_output.splitlines():
        parts = [part.strip() for part in row.split(',')]
        if len(parts) < 2:
            continue
        gpu_uuid = parts[0]
        pid = parts[1]
        username = pid_users.get(pid)
        memory_text = parts[2] if len(parts) > 2 else '0'
        memory_mb = float(memory_text) if memory_text not in {'', 'N/A'} else 0.0
        if username:
            uuid_to_users.setdefault(gpu_uuid, [])
            if username not in uuid_to_users[gpu_uuid]:
                uuid_to_users[gpu_uuid].append(username)
            if pid.isdigit():
                pid_int = int(pid)
                uuid_to_pids.setdefault(gpu_uuid, {})[pid_int] = username
                uuid_to_pid_memory.setdefault(gpu_uuid, {})[pid_int] = memory_mb
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
                active_pids=uuid_to_pids.get(gpu_uuid, {}),
                active_pid_memory_mb=uuid_to_pid_memory.get(gpu_uuid, {}),
            )
        )

    return HostSnapshot(
        host_name=host_name,
        host_address=host_address,
        collected_at=datetime.now(timezone.utc),
        gpu_records=gpu_records,
        storage_used_bytes=storage_used_bytes,
    )


def _parse_home_usage_output(output: str) -> HomeUsageScanResult:
    result = HomeUsageScanResult(used_bytes={}, timed_out_users=[], failed_users=[])
    for line in output.splitlines():
        parts = line.strip().split('\t')
        kind = parts[0]
        if kind == 'OK' and len(parts) == 3 and parts[1].isdigit():
            result.used_bytes[parts[2]] = int(parts[1])
        elif kind == 'TIMEOUT' and len(parts) >= 2:
            result.timed_out_users.append(parts[1])
        elif kind == 'ERROR' and len(parts) >= 2:
            result.failed_users.append(parts[1])
        elif kind == 'STUCK' and len(parts) >= 2:
            result.stuck_user = parts[1]
        elif kind == 'BUSY':
            result.busy_reason = parts[1] if len(parts) > 1 else 'busy'
    return result


def collect_home_user_usage(host_address: str, credentials: SshCredentials) -> HomeUsageScanResult:
    """Scan per-user /home usage on a dedicated SSH connection.

    Uses its own connection so a long scan never holds up (or tears down) the
    shared connection used by the regular GPU collector.
    """
    command = _build_home_user_usage_query(
        settings.excluded_users,
        credentials.password or '',
        settings.home_scan_du_timeout_seconds,
    )
    client = _connect(host_address, credentials)
    try:
        exit_code, output, error = _run_with_deadline(client, command, settings.home_scan_total_timeout_seconds)
    finally:
        client.close()
    if exit_code not in (None, 0) and not output:
        raise RemoteCollectorError(error or f'Home usage scan exited with code {exit_code}')
    result = _parse_home_usage_output(output)
    result.ssh_timed_out = exit_code is None
    return result
