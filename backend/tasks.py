"""Простое in-memory хранилище фоновых задач с прогрессом."""
import uuid
import threading
from datetime import datetime
from typing import Optional

_lock = threading.Lock()
_tasks: dict[str, dict] = {}


def create(title: str, server_id: int = None) -> str:
    tid = str(uuid.uuid4())
    with _lock:
        _tasks[tid] = {
            "id": tid,
            "title": title,
            "server_id": server_id,
            "status": "running",  # running | done | error
            "progress": 0,        # 0-100
            "message": "",
            "created_at": datetime.utcnow().isoformat(),
        }
    return tid


def update(tid: str, progress: int = None, message: str = None,
           status: str = None):
    with _lock:
        t = _tasks.get(tid)
        if not t:
            return
        if progress is not None:
            t["progress"] = min(100, max(0, progress))
        if message is not None:
            t["message"] = message
        if status is not None:
            t["status"] = status


def finish(tid: str, message: str = ""):
    update(tid, progress=100, status="done", message=message)


def fail(tid: str, message: str = ""):
    update(tid, status="error", message=message)


def get(tid: str) -> Optional[dict]:
    with _lock:
        return dict(_tasks[tid]) if tid in _tasks else None


def for_server(server_id: int) -> list[dict]:
    with _lock:
        return [dict(t) for t in _tasks.values() if t.get("server_id") == server_id]


def delete(tid: str):
    with _lock:
        _tasks.pop(tid, None)
