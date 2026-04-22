"""Файловый менеджер в директории сервера (sandbox через resolve + проверку префикса)."""
from pathlib import Path
from typing import List
from fastapi import HTTPException
import shutil

from docker_manager import server_dir


def _safe(server_id: int, rel: str) -> Path:
    root = server_dir(server_id).resolve()
    target = (root / rel.lstrip("/\\")).resolve()
    if not str(target).startswith(str(root)):
        raise HTTPException(400, "Path escapes server directory")
    return target


def list_dir(server_id: int, rel: str = "") -> List[dict]:
    p = _safe(server_id, rel)
    if not p.exists():
        return []
    if not p.is_dir():
        raise HTTPException(400, "Not a directory")
    items = []
    for child in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        try:
            stat = child.stat()
            items.append({
                "name": child.name,
                "is_dir": child.is_dir(),
                "size": stat.st_size if child.is_file() else 0,
                "modified": stat.st_mtime,
            })
        except OSError:
            continue
    return items


def read_file(server_id: int, rel: str) -> str:
    p = _safe(server_id, rel)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "File not found")
    if p.stat().st_size > 2 * 1024 * 1024:
        raise HTTPException(413, "File too large (>2MB)")
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "Binary file")


def write_file(server_id: int, rel: str, content: str):
    p = _safe(server_id, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def delete_path(server_id: int, rel: str):
    p = _safe(server_id, rel)
    if not p.exists():
        raise HTTPException(404, "Not found")
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()


def create_dir(server_id: int, rel: str):
    p = _safe(server_id, rel)
    p.mkdir(parents=True, exist_ok=True)


def rename_path(server_id: int, rel: str, new_rel: str):
    src = _safe(server_id, rel)
    dst = _safe(server_id, new_rel)
    if not src.exists():
        raise HTTPException(404, "Not found")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
