"""Бэкапы — tar.gz архивы директории сервера."""
import os
import tarfile
from pathlib import Path
from datetime import datetime
from docker_manager import server_dir, DATA_ROOT

BACKUP_ROOT = (DATA_ROOT.parent / "backups").resolve()
BACKUP_ROOT.mkdir(parents=True, exist_ok=True)


def _backup_dir(server_id: int) -> Path:
    p = BACKUP_ROOT / f"srv_{server_id}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def create_backup(server_id: int, name: str) -> tuple[str, int]:
    src = server_dir(server_id)
    safe = "".join(c for c in name if c.isalnum() or c in "-_") or "backup"
    fname = f"{safe}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.tar.gz"
    out = _backup_dir(server_id) / fname
    with tarfile.open(out, "w:gz") as tar:
        for entry in src.iterdir():
            tar.add(entry, arcname=entry.name)
    return fname, out.stat().st_size


def restore_backup(server_id: int, filename: str):
    bp = _backup_dir(server_id) / filename
    if not bp.exists():
        raise FileNotFoundError("Backup file missing")
    dst = server_dir(server_id)
    with tarfile.open(bp, "r:gz") as tar:
        for member in tar.getmembers():
            target = (dst / member.name).resolve()
            if not target.is_relative_to(dst.resolve()):
                raise ValueError(f"Unsafe path in backup: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as sf, open(target, "wb") as df:
                    import shutil
                    shutil.copyfileobj(sf, df)


def delete_backup(server_id: int, filename: str):
    bp = _backup_dir(server_id) / filename
    if bp.exists():
        bp.unlink()


def backup_path(server_id: int, filename: str) -> Path:
    bp = (_backup_dir(server_id) / filename).resolve()
    if not bp.is_relative_to(_backup_dir(server_id).resolve()):
        raise ValueError("Invalid path")
    return bp
