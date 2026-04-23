"""Site webroot file manager — mirror of files.py but rooted at data/sites/site_{id}."""
import os
import shutil
from pathlib import Path
from typing import List
from fastapi import HTTPException

import files as fs

SITES_ROOT = Path(os.environ.get("PANEL_SITES_ROOT", "./data/sites")).resolve()
SITES_ROOT.mkdir(parents=True, exist_ok=True)


def site_dir(site_id: int) -> Path:
    p = SITES_ROOT / f"site_{site_id}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe(site_id: int, rel: str) -> Path:
    root = site_dir(site_id).resolve()
    target = (root / (rel or "").lstrip("/\\")).resolve()
    if not str(target).startswith(str(root)):
        raise HTTPException(400, "Path escapes site directory")
    return target


def list_dir(site_id: int, rel: str = "") -> List[dict]:
    p = _safe(site_id, rel)
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


def delete_path(site_id: int, rel: str):
    p = _safe(site_id, rel)
    if not p.exists():
        raise HTTPException(404, "Not found")
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()


def create_dir(site_id: int, rel: str):
    p = _safe(site_id, rel)
    p.mkdir(parents=True, exist_ok=True)


def extract_archive(site_id: int, rel: str) -> int:
    """Extract archive at rel into its parent directory."""
    src = _safe(site_id, rel)
    if not src.exists() or not src.is_file():
        raise HTTPException(404, "Archive not found")
    root = site_dir(site_id).resolve()
    dest = src.parent

    name = src.name.lower()
    count = 0

    def safe_dest(member_path: str) -> Path:
        target = (dest / member_path).resolve()
        if not str(target).startswith(str(root)):
            raise HTTPException(400, f"Unsafe path in archive: {member_path}")
        return target

    if name.endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(src, "r") as zf:
            for member in zf.infolist():
                out = safe_dest(member.filename)
                if member.filename.endswith("/"):
                    out.mkdir(parents=True, exist_ok=True)
                else:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as sf, open(out, "wb") as df:
                        shutil.copyfileobj(sf, df)
                    count += 1
    elif any(name.endswith(e) for e in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        import tarfile
        with tarfile.open(src, "r:*") as tf:
            for member in tf.getmembers():
                out = safe_dest(member.name)
                if member.isdir():
                    out.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with tf.extractfile(member) as sf, open(out, "wb") as df:
                        shutil.copyfileobj(sf, df)
                    count += 1
    else:
        raise HTTPException(400, f"Unsupported archive format: {src.name}")
    return count


def is_archive(name: str) -> bool:
    return fs.is_archive(name)
