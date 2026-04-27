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

_procs: dict[int, any] = {}
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
    if not target.is_relative_to(base.resolve()):
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
        env.setdefault("HOST", "0.0.0.0")
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


class DockerProc:
    def __init__(self, container, log_fh):
        self.c = container
        self.pid = container.id[:12]
        self.log_fh = log_fh
        self._exit_code = None
        self._thread = threading.Thread(target=self._stream_logs, daemon=True)
        self._thread.start()

    def _stream_logs(self):
        try:
            for chunk in self.c.logs(stream=True, follow=True, stdout=True, stderr=True):
                self.log_fh.write(chunk)
                self.log_fh.flush()
            res = self.c.wait()
            self._exit_code = res.get("StatusCode", 0)
        except Exception:
            self._exit_code = -1
        finally:
            try: self.c.remove(force=True)
            except Exception: pass

    def poll(self):
        if self._exit_code is not None:
            return self._exit_code
        try:
            self.c.reload()
            if self.c.status == "running":
                return None
            if self.c.status == "exited":
                return self._exit_code or 0
        except Exception:
            return self._exit_code or -1
        return None

    def terminate(self):
        try: self.c.stop(timeout=10)
        except Exception: pass

    def kill(self):
        try: self.c.kill()
        except Exception: pass

    def wait(self, timeout=None):
        self._thread.join(timeout)


def status(site_id: int) -> dict:
    with _lock:
        p = _procs.get(site_id)
        if p is None:
            return {"running": False, "pid": 0, "exit_code": None}
        rc = p.poll()
        if rc is None:
            return {"running": True, "pid": getattr(p, "pid", 0), "exit_code": None}
        # finished
        _procs.pop(site_id, None)
        return {"running": False, "pid": 0, "exit_code": rc}


def stop(site_id: int, timeout: float = 10.0):
    with _lock:
        p = _procs.pop(site_id, None)
    if not p or p.poll() is not None:
        return False
    try:
        p.terminate()
        p.wait(timeout=timeout)
        if p.poll() is None:
            p.kill()
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

    import docker
    try:
        client = docker.from_env()
        env = _build_env(w)
        image = "node:20-bookworm"
        try: client.images.get(image)
        except docker.errors.NotFound: client.images.pull(image)

        c_name = f"panel_site_{w.id}"
        try:
            client.containers.get(c_name).remove(force=True)
        except docker.errors.NotFound:
            pass

        port_bindings = {}
        if getattr(w, "runtime_port", 0):
            port_bindings[f"{w.runtime_port}/tcp"] = ("127.0.0.1", int(w.runtime_port))

        c = client.containers.create(
            image=image,
            command=["sh", "-c", cmd],
            name=c_name,
            working_dir="/site",
            volumes={str(cwd): {"bind": "/site", "mode": "rw"}},
            environment=env,
            ports=port_bindings if port_bindings else None,
            detach=True
        )
        c.start()
        p = DockerProc(c, fh)
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
        import docker
        try:
            client = docker.from_env()
            env = _build_env(w)
            image = "node:20-bookworm"
            try: client.images.get(image)
            except docker.errors.NotFound: client.images.pull(image)

            c_name = f"panel_site_{w.id}_install"
            try:
                client.containers.get(c_name).remove(force=True)
            except docker.errors.NotFound:
                pass

            c = client.containers.run(
                image=image,
                command=["sh", "-c", cmd],
                name=c_name,
                working_dir="/site",
                volumes={str(cwd): {"bind": "/site", "mode": "rw"}},
                environment=env,
                detach=True
            )
            for chunk in c.logs(stream=True, follow=True, stdout=True, stderr=True):
                fh.write(chunk)
                fh.flush()
            res = c.wait()
            c.remove(force=True)
            if res.get("StatusCode", 0) != 0:
                return False, f"install завершился с кодом {res.get('StatusCode', 0)}"
        except Exception as e:
            return False, f"Ошибка: {e}"
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