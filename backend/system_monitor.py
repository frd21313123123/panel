"""Host-level monitoring helpers for the panel."""
import os
import platform
import shutil
import socket
import time
from pathlib import Path
from typing import Optional

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    psutil = None


_last_cpu: Optional[tuple[float, float]] = None
_last_net: Optional[tuple[float, int, int]] = None


def _pct(used: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return round(max(0.0, min(100.0, used * 100.0 / total)), 1)


def _read_meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                key, value = line.split(":", 1)
                parts = value.strip().split()
                out[key] = int(parts[0]) * 1024 if parts else 0
    except OSError:
        pass
    return out


def _proc_cpu_times() -> Optional[tuple[float, float]]:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            line = f.readline()
    except OSError:
        return None
    if not line.startswith("cpu "):
        return None
    fields = [float(x) for x in line.split()[1:]]
    if len(fields) < 4:
        return None
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
    return sum(fields), idle


def _cpu_percent() -> Optional[float]:
    global _last_cpu
    if psutil:
        try:
            return round(float(psutil.cpu_percent(interval=None)), 1)
        except Exception:
            pass

    current = _proc_cpu_times()
    if current is None:
        return None
    total, idle = current
    if _last_cpu is None:
        _last_cpu = current
        return None
    prev_total, prev_idle = _last_cpu
    _last_cpu = current
    total_delta = total - prev_total
    idle_delta = idle - prev_idle
    if total_delta <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0)), 1)


def _memory() -> dict:
    if psutil:
        try:
            vm = psutil.virtual_memory()
            return {
                "total": int(vm.total),
                "used": int(vm.used),
                "available": int(vm.available),
                "percent": round(float(vm.percent), 1),
            }
        except Exception:
            pass

    mi = _read_meminfo()
    total = mi.get("MemTotal", 0)
    available = mi.get("MemAvailable", mi.get("MemFree", 0) + mi.get("Buffers", 0) + mi.get("Cached", 0))
    used = max(0, total - available)
    return {"total": total, "used": used, "available": available, "percent": _pct(used, total)}


def _swap() -> dict:
    if psutil:
        try:
            sw = psutil.swap_memory()
            return {
                "total": int(sw.total),
                "used": int(sw.used),
                "free": int(sw.free),
                "percent": round(float(sw.percent), 1),
            }
        except Exception:
            pass

    mi = _read_meminfo()
    total = mi.get("SwapTotal", 0)
    free = mi.get("SwapFree", 0)
    used = max(0, total - free)
    return {"total": total, "used": used, "free": free, "percent": _pct(used, total)}


def _disk(path: Path, label: str) -> dict:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(str(probe))
        return {
            "label": label,
            "path": str(path),
            "total": int(usage.total),
            "used": int(usage.used),
            "free": int(usage.free),
            "percent": _pct(usage.used, usage.total),
        }
    except OSError:
        return {"label": label, "path": str(path), "total": 0, "used": 0, "free": 0, "percent": 0.0}


def _net_totals() -> tuple[int, int]:
    if psutil:
        try:
            counters = psutil.net_io_counters(pernic=True)
            rx = tx = 0
            for name, item in counters.items():
                if name.lower().startswith(("lo", "loopback")):
                    continue
                rx += int(item.bytes_recv)
                tx += int(item.bytes_sent)
            return rx, tx
        except Exception:
            pass

    rx = tx = 0
    try:
        with open("/proc/net/dev", "r", encoding="utf-8") as f:
            for line in f.readlines()[2:]:
                if ":" not in line:
                    continue
                name, data = line.split(":", 1)
                if name.strip() == "lo":
                    continue
                fields = data.split()
                if len(fields) >= 16:
                    rx += int(fields[0])
                    tx += int(fields[8])
    except OSError:
        pass
    return rx, tx


def _network() -> dict:
    global _last_net
    now = time.time()
    rx, tx = _net_totals()
    rx_rate = tx_rate = 0.0
    if _last_net:
        prev_t, prev_rx, prev_tx = _last_net
        dt = max(0.001, now - prev_t)
        rx_rate = max(0.0, (rx - prev_rx) / dt)
        tx_rate = max(0.0, (tx - prev_tx) / dt)
    _last_net = (now, rx, tx)
    return {
        "rx_bytes": rx,
        "tx_bytes": tx,
        "rx_rate": round(rx_rate, 1),
        "tx_rate": round(tx_rate, 1),
    }


def _boot_time() -> Optional[int]:
    if psutil:
        try:
            return int(psutil.boot_time())
        except Exception:
            pass
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def _load_average() -> Optional[list[float]]:
    try:
        return [round(float(x), 2) for x in os.getloadavg()]
    except (AttributeError, OSError):
        return None


def _process_count() -> Optional[int]:
    if psutil:
        try:
            return len(psutil.pids())
        except Exception:
            pass
    proc = Path("/proc")
    if proc.exists():
        try:
            return sum(1 for p in proc.iterdir() if p.name.isdigit())
        except OSError:
            return None
    return None


def collect(base_dir: Path, data_root: Path) -> dict:
    boot = _boot_time()
    now = int(time.time())
    return {
        "timestamp": now,
        "host": {
            "hostname": socket.gethostname(),
            "system": platform.system(),
            "release": platform.release(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count() or 0,
            "load_average": _load_average(),
            "boot_time": boot,
            "uptime": max(0, now - boot) if boot else None,
            "processes": _process_count(),
        },
        "cpu": {"percent": _cpu_percent(), "count": os.cpu_count() or 0},
        "memory": _memory(),
        "swap": _swap(),
        "network": _network(),
        "disks": [
            _disk(base_dir.resolve(), "Панель"),
            _disk(data_root.resolve(), "Данные серверов"),
        ],
    }
