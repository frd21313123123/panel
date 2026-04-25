"""Per-site process manager: launches and supervises long-running commands
(node server.js, python app.py, etc.) defined in the Website row.

Logs go to data/sites/site_{id}/.runtime.log (rotated when exceeds 5 MB).
Supervision is in-process — when the panel restarts, sites configured with
runtime_enabled=True are auto-started.
"""
import os
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path

import sites_files as sfs

_procs: dict[int, subprocess.Popen] = {}
_lock = threading.Lock()
LOG_MAX = 5 * 1024 * 1024


def _log_path(site_id: int) -> Path:
    return sfs.site_dir(site_id) / ".runtime.log"


def _cwd(w) -> Path:
    base = sfs.site_dir(w.id)
    sub = (getattr(w, "runtime_cwd", "") or "").strip().strip("/\\")
    if not sub:
        return base
    target = (base / sub).resolve()
    if not str(target).startswith(str(base.resolve())):
        return base
    target.mkdir(parents=True, exist_ok=True)
    return target


def _build_env(w) -> dict:
    env = os.environ.copy()
    # parse KEY=value lines from runtime_env
    raw = (getattr(w, "runtime_env", "") or "").splitlines()
    for line in raw:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    if getattr(w, "runtime_port", 0):
        env.setdefault("PORT", str(w.runtime_port))
        env.setdefault("HOST", "127.0.0.1")
    return env


def write_dotenv(w):
    """Sync runtime_env into a .env file in the site cwd."""
    cwd = _cwd(w)
    raw = (getattr(w, "runtime_env", "") or "").strip()
    if not raw:
        return
    target = cwd / ".env"
    target.write_text(raw + ("\n" if not raw.endswith("\n") else ""))
    try: target.chmod(0o600)
    except OSError: pass


def _rotate(path: Path):
    try:
        if path.exists() and path.stat().st_size > LOG_MAX:
            path.rename(path.with_suffix(".log.old"))
    except OSError:
        pass


def status(site_id: int) -> dict:
    with _lock:
        p = _procs.get(site_id)
        if p is None:
            return {"running": False, "pid": 0, "exit_code": None}
        rc = p.poll()
        if rc is None:
            return {"running": True, "pid": p.pid, "exit_code": None}
        # finished
        _procs.pop(site_id, None)
        return {"running": False, "pid": 0, "exit_code": rc}


def stop(site_id: int, timeout: float = 10.0):
    with _lock:
        p = _procs.pop(site_id, None)
    if not p or p.poll() is not None:
        return False
    try:
        # try terminate process group
        try: os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except (OSError, AttributeError): p.terminate()
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except (OSError, AttributeError): p.kill()
            p.wait(timeout=5)
    except Exception:
        pass
    return True


def start(w) -> tuple[bool, str]:
    """Launch the configured start command. Returns (ok, message)."""
    cmd = (getattr(w, "runtime_start_cmd", "") or "").strip()
    if not cmd:
        return False, "Команда запуска не задана"
    st = status(w.id)
    if st["running"]:
        return True, f"Уже работает (PID {st['pid']})"

    write_dotenv(w)
    cwd = _cwd(w)
    log = _log_path(w.id)
    _rotate(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "ab", buffering=0)
    fh.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} | cwd={cwd} | cmd={cmd}\n".encode())
    fh.flush()

    try:
        p = subprocess.Popen(
            shlex.split(cmd) if not any(c in cmd for c in "|&;<>$`") else ["sh", "-c", cmd],
            cwd=str(cwd),
            env=_build_env(w),
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            close_fds=True,
        )
    except Exception as e:
        try: fh.close()
        except Exception: pass
        return False, f"Не удалось запустить: {e}"

    with _lock:
        _procs[w.id] = p
    # tiny grace period to detect immediate failure
    time.sleep(0.4)
    rc = p.poll()
    if rc is not None:
        with _lock: _procs.pop(w.id, None)
        return False, f"Процесс упал сразу (exit code {rc}); смотрите логи"
    return True, f"Запущено (PID {p.pid})"


def restart(w) -> tuple[bool, str]:
    stop(w.id)
    return start(w)


def run_install(w) -> tuple[bool, str]:
    cmd = (getattr(w, "runtime_install_cmd", "") or "").strip()
    if not cmd:
        return False, "Команда установки не задана"
    write_dotenv(w)
    cwd = _cwd(w)
    log = _log_path(w.id)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab", buffering=0) as fh:
        fh.write(f"\n--- install {time.strftime('%Y-%m-%d %H:%M:%S')} | cmd={cmd}\n".encode())
        try:
            r = subprocess.run(
                shlex.split(cmd) if not any(c in cmd for c in "|&;<>$`") else ["sh", "-c", cmd],
                cwd=str(cwd), env=_build_env(w),
                stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                timeout=900,
            )
        except subprocess.TimeoutExpired:
            return False, "Установка прервана по таймауту (15 мин)"
        except Exception as e:
            return False, f"Ошибка: {e}"
    if r.returncode != 0:
        return False, f"install завершился с кодом {r.returncode}"
    return True, "install выполнен успешно"


def auto_start_all(get_session_factory):
    """Called on panel boot. Starts every site with runtime_enabled=True."""
    from database import Website
    db = get_session_factory()
    try:
        for w in db.query(Website).filter(Website.runtime_enabled == True).all():
            try:
                start(w)
            except Exception:
                pass
    finally:
        db.close()


def stop_all():
    with _lock:
        ids = list(_procs.keys())
    for sid in ids:
        stop(sid)


def tail(site_id: int, last_bytes: int = 64 * 1024) -> str:
    p = _log_path(site_id)
    if not p.exists():
        return ""
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > last_bytes:
                f.seek(size - last_bytes)
                f.readline()  # discard partial line
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
