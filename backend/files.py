"""Файловый менеджер в директории сервера (sandbox через resolve + проверку префикса)."""
from pathlib import Path
from typing import List
from fastapi import HTTPException
import shutil
import tarfile
import zipfile

from docker_manager import server_dir

ARCHIVE_EXTS = {
    ".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
    ".tar.xz", ".txz", ".tar.zst", ".gz", ".bz2", ".7z",
}


def is_archive(name: str) -> bool:
    n = name.lower()
    return any(n.endswith(e) for e in ARCHIVE_EXTS)


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


def extract_archive(server_id: int, rel: str, dest_rel: str = "") -> int:
    """Распаковывает архив в dest_rel (по умолчанию — в ту же папку где архив).
    Возвращает количество извлечённых файлов.
    Все пути внутри архива проверяются на path-traversal."""
    src = _safe(server_id, rel)
    if not src.exists() or not src.is_file():
        raise HTTPException(404, "Archive not found")

    root = server_dir(server_id).resolve()

    # Папка назначения
    if dest_rel:
        dest = _safe(server_id, dest_rel)
    else:
        dest = src.parent

    dest.mkdir(parents=True, exist_ok=True)

    name = src.name.lower()
    count = 0

    def safe_dest(member_path: str) -> Path:
        """Защита от zip-slip."""
        target = (dest / member_path).resolve()
        if not str(target).startswith(str(root)):
            raise HTTPException(400, f"Unsafe path in archive: {member_path}")
        return target

    if name.endswith(".zip"):
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

    elif any(name.endswith(e) for e in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar.zst")):
        mode = "r:*"
        with tarfile.open(src, mode) as tf:
            for member in tf.getmembers():
                out = safe_dest(member.name)
                if member.isdir():
                    out.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with tf.extractfile(member) as sf, open(out, "wb") as df:
                        shutil.copyfileobj(sf, df)
                    count += 1

    elif name.endswith(".gz"):
        import gzip
        out_name = src.stem  # strip .gz
        out = safe_dest(out_name)
        with gzip.open(src, "rb") as sf, open(out, "wb") as df:
            shutil.copyfileobj(sf, df)
        count = 1

    elif name.endswith(".bz2"):
        import bz2
        out_name = src.stem
        out = safe_dest(out_name)
        with bz2.open(src, "rb") as sf, open(out, "wb") as df:
            shutil.copyfileobj(sf, df)
        count = 1

    elif name.endswith(".7z"):
        import py7zr
        with py7zr.SevenZipFile(src, mode="r") as zf:
            for fname, bio in zf.read().items():
                out = safe_dest(fname)
                if bio is None:
                    out.mkdir(parents=True, exist_ok=True)
                else:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with open(out, "wb") as df:
                        shutil.copyfileobj(bio, df)
                    count += 1

    else:
        raise HTTPException(400, f"Unsupported archive format: {src.name}")

    return count
