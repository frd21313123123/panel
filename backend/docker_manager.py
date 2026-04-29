"""Обёртка над Docker SDK для управления контейнерами-"серверами"."""
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
import docker
from docker.errors import NotFound, APIError

_disk_cache: dict = {}  # server_id -> (timestamp, size_bytes)
_stats_history: dict[int, list[dict]] = {}
_stats_lock = threading.Lock()
_MAX_STATS_HISTORY = 30


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def disk_usage(server_id: int, ttl: float = 10.0) -> int:
    now = time.time()
    cached = _disk_cache.get(server_id)
    if cached and now - cached[0] < ttl:
        return cached[1]
    sz = _dir_size(server_dir(server_id))
    _disk_cache[server_id] = (now, sz)
    return sz

DATA_ROOT = Path(os.environ.get("PANEL_DATA_ROOT", "./data/servers")).resolve()
DATA_ROOT.mkdir(parents=True, exist_ok=True)

_client: Optional[docker.DockerClient] = None


def client() -> docker.DockerClient:
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def docker_available() -> bool:
    try:
        client().ping()
        return True
    except Exception:
        return False


def server_dir(server_id: int) -> Path:
    p = DATA_ROOT / f"srv_{server_id}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def event_log_path(server_id: int) -> Path:
    p = server_dir(server_id) / ".panel"
    p.mkdir(parents=True, exist_ok=True)
    return p / "console.log"


def append_event(server_id: int, message: str):
    try:
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = str(message or "").rstrip().splitlines() or [""]
        with event_log_path(server_id).open("a", encoding="utf-8", newline="\n") as f:
            for line in lines:
                f.write(f"[{ts}] {line}\n")
    except Exception:
        pass


def read_events(server_id: int, tail_bytes: int = 65536) -> str:
    path = event_log_path(server_id)
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def event_log_size(server_id: int) -> int:
    try:
        return event_log_path(server_id).stat().st_size
    except Exception:
        return 0


def read_event_chunk(server_id: int, offset: int) -> tuple[str, int]:
    path = event_log_path(server_id)
    try:
        size = path.stat().st_size
        if size < offset:
            offset = 0
        if size == offset:
            return "", offset
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read(size - offset)
        return data.decode("utf-8", errors="replace"), size
    except Exception:
        return "", offset


def container_name(server_id: int) -> str:
    return f"panel_srv_{server_id}"


def inspect(server_id: int):
    try:
        return client().containers.get(container_name(server_id))
    except NotFound:
        return None


def create_container(server_id: int, image: str, cmd: str, memory_mb: int, cpu_percent: int,
                     ports: list = None, env: dict = None) -> str:
    # удаляем старый
    old = inspect(server_id)
    if old:
        try:
            old.remove(force=True)
        except APIError:
            pass

    # pull образа при необходимости
    try:
        client().images.get(image)
    except NotFound:
        append_event(server_id, f"Docker: image '{image}' not found locally, pulling...")
        client().images.pull(image)
        append_event(server_id, f"Docker: image '{image}' pulled")

    host_dir = str(server_dir(server_id))
    cpu_quota = max(1000, int(cpu_percent * 1000))  # 100% = 100000

    port_bindings = {}
    for p in (ports or []):
        try:
            key = f"{int(p['container'])}/{p.get('proto', 'tcp')}"
            port_bindings[key] = int(p["host"])
        except (KeyError, ValueError, TypeError):
            continue

    env_dict = {"HOME": "/home/container"}
    if env:
        for k, v in env.items():
            if k:
                env_dict[str(k)] = str(v)

    c = client().containers.create(
        image=image,
        command=["sh", "-c", cmd] if cmd else None,
        name=container_name(server_id),
        working_dir="/home/container",
        volumes={host_dir: {"bind": "/home/container", "mode": "rw"}},
        mem_limit=f"{memory_mb}m",
        cpu_period=100000,
        cpu_quota=cpu_quota,
        stdin_open=True,
        tty=True,
        detach=True,
        network_mode="bridge",
        environment=env_dict,
        ports=port_bindings or None,
    )
    append_event(server_id, f"Docker: container created from '{image}'")
    return c.id


def exec_interactive(server_id: int, cmd: str = "/bin/sh"):
    """Создаёт интерактивный exec с TTY и socket-стримом для xterm."""
    c = inspect(server_id)
    if not c:
        return None, None
    api = client().api
    exec_id = api.exec_create(c.id, cmd, tty=True, stdin=True, stdout=True, stderr=True)["Id"]
    sock = api.exec_start(exec_id, tty=True, socket=True, demux=False)
    raw = sock._sock if hasattr(sock, "_sock") else sock
    return exec_id, raw


def exec_resize(exec_id: str, rows: int, cols: int):
    try:
        client().api.exec_resize(exec_id, height=rows, width=cols)
    except Exception:
        pass


def start(server_id: int):
    c = inspect(server_id)
    if c:
        append_event(server_id, "Docker: starting container")
        c.start()
        append_event(server_id, "Docker: start command sent")


def stop(server_id: int):
    c = inspect(server_id)
    if c:
        append_event(server_id, "Docker: stopping container")
        c.stop(timeout=10)
        append_event(server_id, "Docker: container stopped")


def restart(server_id: int):
    c = inspect(server_id)
    if c:
        append_event(server_id, "Docker: restarting container")
        c.restart(timeout=10)
        append_event(server_id, "Docker: restart command sent")


def kill(server_id: int):
    c = inspect(server_id)
    if c:
        try:
            append_event(server_id, "Docker: killing container")
            c.kill()
            append_event(server_id, "Docker: kill command sent")
        except APIError:
            pass


def remove_container(server_id: int):
    c = inspect(server_id)
    if c:
        try:
            append_event(server_id, "Docker: removing container")
            c.remove(force=True)
            append_event(server_id, "Docker: container removed")
        except APIError:
            pass


def remove(server_id: int):
    remove_container(server_id)
    p = DATA_ROOT / f"srv_{server_id}"
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)


def status(server_id: int) -> str:
    c = inspect(server_id)
    if not c:
        return "offline"
    c.reload()
    return c.status


def _uptime_seconds(c) -> int:
    try:
        started = (c.attrs.get("State") or {}).get("StartedAt")
        if not started or started.startswith("0001"):
            return 0
        # формат: 2024-01-02T03:04:05.123456789Z — обрезаем нс до мкс
        s = started.replace("Z", "+00:00")
        if "." in s:
            head, tail = s.split(".", 1)
            tz = ""
            if "+" in tail:
                tail, tz = tail.split("+", 1); tz = "+" + tz
            elif "-" in tail:
                tail, tz = tail.split("-", 1); tz = "-" + tz
            tail = tail[:6]
            s = f"{head}.{tail}{tz}"
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(s)
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except Exception:
        return 0


def _record_stats_sample(server_id: int, data: dict):
    sample = {
        "t": time.time(),
        "cpu": data.get("cpu", 0),
        "mem": data.get("mem", 0),
        "mem_limit": data.get("mem_limit", 0),
        "disk": data.get("disk", 0),
        "net_rx": data.get("net_rx", 0),
        "net_tx": data.get("net_tx", 0),
        "status": data.get("status", "offline"),
        "uptime": data.get("uptime", 0),
    }
    with _stats_lock:
        items = _stats_history.setdefault(server_id, [])
        items.append(sample)
        del items[:-_MAX_STATS_HISTORY]


def stats_history(server_id: int) -> list[dict]:
    with _stats_lock:
        return [dict(x) for x in _stats_history.get(server_id, [])]


def stats(server_id: int, record: bool = False) -> dict:
    c = inspect(server_id)
    disk = disk_usage(server_id)
    base = {"cpu": 0, "mem": 0, "mem_limit": 0, "disk": disk,
            "net_rx": 0, "net_tx": 0, "status": "offline", "uptime": 0}
    if not c:
        if record:
            _record_stats_sample(server_id, base)
        base["history"] = stats_history(server_id)
        return base
    try:
        c.reload()
        if c.status != "running":
            base["status"] = c.status
            if record:
                _record_stats_sample(server_id, base)
            base["history"] = stats_history(server_id)
            return base
        uptime = _uptime_seconds(c) if c.status == "running" else 0
        s = c.stats(stream=False)
        cpu_delta = s["cpu_stats"]["cpu_usage"]["total_usage"] - s["precpu_stats"]["cpu_usage"]["total_usage"]
        sys_delta = s["cpu_stats"].get("system_cpu_usage", 0) - s["precpu_stats"].get("system_cpu_usage", 0)
        cpu_pct = 0.0
        if sys_delta > 0 and cpu_delta > 0:
            online = s["cpu_stats"].get("online_cpus", 1) or 1
            cpu_pct = (cpu_delta / sys_delta) * online * 100.0
        mem = s["memory_stats"].get("usage", 0)
        mem_limit = s["memory_stats"].get("limit", 0)
        net_rx = 0
        net_tx = 0
        for iface in (s.get("networks") or {}).values():
            net_rx += iface.get("rx_bytes", 0)
            net_tx += iface.get("tx_bytes", 0)
        data = {"cpu": round(cpu_pct, 2), "mem": mem, "mem_limit": mem_limit,
                "disk": disk, "net_rx": net_rx, "net_tx": net_tx,
                "status": c.status, "uptime": uptime}
        if record:
            _record_stats_sample(server_id, data)
        data["history"] = stats_history(server_id)
        return data
    except Exception:
        base["status"] = c.status
        base["uptime"] = _uptime_seconds(c) if c.status == "running" else 0
        if record:
            _record_stats_sample(server_id, base)
        base["history"] = stats_history(server_id)
        return base


def logs(server_id: int, tail: int = 200) -> str:
    c = inspect(server_id)
    if not c:
        return ""
    try:
        return c.logs(tail=tail, stdout=True, stderr=True).decode("utf-8", errors="replace")
    except Exception:
        return ""


def exec_command(server_id: int, cmd: str) -> str:
    c = inspect(server_id)
    if not c:
        return "[container not found]"
    try:
        res = c.exec_run(["sh", "-c", cmd], tty=False, demux=False)
        return res.output.decode("utf-8", errors="replace")
    except Exception as e:
        return f"[exec error: {e}]"


def attach_stream(server_id: int, tail: int = 100, since: int | None = None):
    """Возвращает генератор вывода контейнера (stdout/stderr) для стрима логов."""
    c = inspect(server_id)
    if not c:
        return None
    try:
        c.reload()
        if c.status != "running":
            return None
    except Exception:
        return None
    kwargs = {"stream": True, "follow": True, "stdout": True, "stderr": True, "tail": tail}
    if since is not None:
        kwargs["since"] = since
    return c.logs(**kwargs)
