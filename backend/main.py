"""Panel — браузерная панель управления кодовыми контейнерами (Pterodactyl-like)."""
import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from database import init_db, get_db, User, Server, Egg, Subuser, Schedule, Backup, Website, Setting, SessionLocal
import threading
import auth
import docker_manager as dm
import nginx_manager as nm
import files as fs
import sites_files as sfs
import site_runtime as srt
import backups as bk
import scheduler
import tasks as tk

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
_stats_sampler_started = False

app = FastAPI(title="Panel", version="0.1.0")
templates = Jinja2Templates(directory=str(FRONTEND_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR / "static")), name="static")


def _start_stats_sampler():
    global _stats_sampler_started
    if _stats_sampler_started:
        return
    _stats_sampler_started = True

    def _loop():
        while True:
            db = SessionLocal()
            try:
                ids = [row[0] for row in db.query(Server.id).all()]
            except Exception:
                ids = []
            finally:
                db.close()

            for sid in ids:
                try:
                    dm.stats(sid, record=True)
                except Exception:
                    pass
            time.sleep(2)

    threading.Thread(target=_loop, daemon=True, name="panel-stats-sampler").start()


@app.on_event("startup")
def startup():
    init_db()
    # создать админа по умолчанию, если нет пользователей
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            import secrets
            rand_pass = secrets.token_urlsafe(8)
            admin = User(
                username="admin",
                email="admin@panel.local",
                password_hash=auth.hash_password(rand_pass),
                is_admin=True,
            )
            db.add(admin)
            db.commit()
            print(f"[panel] default admin created: admin / {rand_pass}")
    finally:
        db.close()
    scheduler.start_background()
    print("[panel] scheduler started")
    _start_stats_sampler()
    print("[panel] stats sampler started")
    try:
        srt.auto_start_all(SessionLocal)
        print("[panel] site runtime auto-start complete")
    except Exception as e:
        print(f"[panel] site runtime auto-start error: {e}")


@app.on_event("shutdown")
def _shutdown_site_runtimes():
    try: srt.stop_all()
    except Exception: pass


# ---------- Schemas ----------
class RegisterIn(BaseModel):
    username: str
    email: EmailStr
    password: str
    is_admin: bool = False


class LoginIn(BaseModel):
    username: str
    password: str


class PortDef(BaseModel):
    host: int
    container: int
    proto: str = "tcp"


class ServerCreate(BaseModel):
    name: str
    egg_id: int
    memory_mb: int = 512
    cpu_limit: int = 100
    disk_mb: int = 1024
    startup_cmd: str = ""
    ports: List[PortDef] = []
    env_vars: dict = {}
    git_repo: str = ""
    git_branch: str = ""
    git_subdir: str = ""
    git_auto_update: bool = False


class ServerUpdate(BaseModel):
    name: Optional[str] = None
    memory_mb: Optional[int] = None
    cpu_limit: Optional[int] = None
    disk_mb: Optional[int] = None
    startup_cmd: Optional[str] = None
    ports: Optional[List[PortDef]] = None
    env_vars: Optional[dict] = None
    git_repo: Optional[str] = None
    git_branch: Optional[str] = None
    git_subdir: Optional[str] = None
    git_auto_update: Optional[bool] = None


class SubuserIn(BaseModel):
    username: str
    permissions: str = "console,files"


class ScheduleIn(BaseModel):
    name: str
    cron: str
    action: str = "command"
    payload: str = ""
    enabled: bool = True


class BackupIn(BaseModel):
    name: str = "backup"


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class AdminPasswordReset(BaseModel):
    new_password: str


class FileWrite(BaseModel):
    path: str
    content: str


class PathIn(BaseModel):
    path: str


class WebsiteIn(BaseModel):
    name: str
    domain: str
    mode: str = "proxy"  # "proxy" | "static"
    proxy_pass: str = ""
    domains: List[str] = []
    listen_port: int = 80
    nginx_extra: str = ""
    ssl_enabled: bool = False
    is_active: bool = True
    git_repo: str = ""
    git_branch: str = ""
    web_subdir: str = ""
    runtime_enabled: bool = False
    runtime_cwd: str = ""
    runtime_install_cmd: str = ""
    runtime_start_cmd: str = ""
    runtime_port: int = 0
    runtime_env: str = ""


class WebsiteUpdate(BaseModel):
    name: Optional[str] = None
    domain: Optional[str] = None
    mode: Optional[str] = None
    proxy_pass: Optional[str] = None
    domains: Optional[List[str]] = None
    listen_port: Optional[int] = None
    nginx_extra: Optional[str] = None
    ssl_enabled: Optional[bool] = None
    is_active: Optional[bool] = None
    git_repo: Optional[str] = None
    git_branch: Optional[str] = None
    web_subdir: Optional[str] = None
    runtime_enabled: Optional[bool] = None
    runtime_cwd: Optional[str] = None
    runtime_install_cmd: Optional[str] = None
    runtime_start_cmd: Optional[str] = None
    runtime_port: Optional[int] = None
    runtime_env: Optional[str] = None


class RenameIn(BaseModel):
    path: str
    new_path: str


# ---------- Pages ----------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    token = request.cookies.get("panel_token")
    if not token or not auth.decode_token(token):
        return RedirectResponse("/login")
    return RedirectResponse("/dashboard")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/servers/{sid}", response_class=HTMLResponse)
def server_page(request: Request, sid: int):
    return templates.TemplateResponse("server.html", {"request": request, "server_id": sid})


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    return templates.TemplateResponse("admin.html", {"request": request})


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request):
    return templates.TemplateResponse("profile.html", {"request": request})


EXPERIMENTAL_FLAGS = {"experimental_websites"}


def _get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.query(Setting).get(key)
    return row.value if row else default


def _flag_enabled(db: Session, key: str) -> bool:
    return _get_setting(db, key, "false").lower() == "true"


def _require_flag(key: str):
    def dep(db: Session = Depends(get_db)):
        if not _flag_enabled(db, key):
            raise HTTPException(404, "Not found")
    return dep


@app.get("/websites", response_class=HTMLResponse)
def websites_page(request: Request, db: Session = Depends(get_db)):
    if not _flag_enabled(db, "experimental_websites"):
        raise HTTPException(404, "Not found")
    return templates.TemplateResponse("websites.html", {"request": request})


@app.get("/sites/{wid}", response_class=HTMLResponse)
def site_page(request: Request, wid: int, db: Session = Depends(get_db)):
    if not _flag_enabled(db, "experimental_websites"):
        raise HTTPException(404, "Not found")
    return templates.TemplateResponse("site.html", {"request": request, "site_id": wid})


# ---------- Settings API ----------
@app.get("/api/settings/public")
def public_settings(db: Session = Depends(get_db), _: User = Depends(auth.get_current_user)):
    """Flags safe to expose to all logged-in users (used by frontend to toggle UI)."""
    return {k: _flag_enabled(db, k) for k in EXPERIMENTAL_FLAGS}


@app.get("/api/settings")
def list_settings(db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    return {k: _flag_enabled(db, k) for k in EXPERIMENTAL_FLAGS}


class SettingIn(BaseModel):
    key: str
    value: bool


@app.post("/api/settings")
def set_setting(body: SettingIn, db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    if body.key not in EXPERIMENTAL_FLAGS:
        raise HTTPException(400, "Unknown setting key")
    row = db.query(Setting).get(body.key)
    val = "true" if body.value else "false"
    if row:
        row.value = val
    else:
        db.add(Setting(key=body.key, value=val))
    db.commit()
    return {"ok": True, "key": body.key, "value": body.value}


# ---------- Auth API ----------
@app.post("/api/auth/register")
def register(body: RegisterIn, db: Session = Depends(get_db),
             admin: User = Depends(auth.require_admin)):
    """Создание новых пользователей — только для администратора."""
    if db.query(User).filter((User.username == body.username) | (User.email == body.email)).first():
        raise HTTPException(400, "Username or email already exists")
    u = User(
        username=body.username,
        email=body.email,
        password_hash=auth.hash_password(body.password),
        is_admin=body.is_admin,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return {"id": u.id, "username": u.username}


@app.post("/api/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    u = db.query(User).filter(User.username == body.username).first()
    if not u or not auth.verify_password(body.password, u.password_hash):
        raise HTTPException(401, "Invalid credentials")
    token = auth.create_token({"sub": u.id, "username": u.username})
    resp = JSONResponse({"token": token, "username": u.username, "is_admin": u.is_admin})
    resp.set_cookie("panel_token", token, httponly=True, max_age=60 * 60 * 24 * 7,
                    samesite="lax", secure=True)
    return resp


@app.post("/api/auth/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("panel_token")
    return resp


@app.post("/api/auth/change-password")
def change_password(body: PasswordChange, user: User = Depends(auth.get_current_user),
                    db: Session = Depends(get_db)):
    if not auth.verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    if len(body.new_password) < 4:
        raise HTTPException(400, "Password must be at least 4 characters")
    user.password_hash = auth.hash_password(body.new_password)
    db.commit()
    return {"ok": True}


@app.post("/api/users/{uid}/reset-password")
def admin_reset_password(uid: int, body: AdminPasswordReset,
                         db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    u = db.query(User).get(uid)
    if not u:
        raise HTTPException(404, "Not found")
    if len(body.new_password) < 4:
        raise HTTPException(400, "Password too short")
    u.password_hash = auth.hash_password(body.new_password)
    db.commit()
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: User = Depends(auth.get_current_user)):
    return {"id": user.id, "username": user.username, "email": user.email, "is_admin": user.is_admin}


# ---------- Eggs API ----------
@app.get("/api/eggs")
def list_eggs(db: Session = Depends(get_db), _: User = Depends(auth.get_current_user)):
    return [{"id": e.id, "name": e.name, "language": e.language, "docker_image": e.docker_image,
             "default_cmd": e.default_cmd, "description": e.description} for e in db.query(Egg).all()]


class EggIn(BaseModel):
    name: str
    language: str
    docker_image: str
    default_cmd: str
    description: str = ""


@app.post("/api/eggs")
def create_egg(body: EggIn, db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    e = Egg(**body.model_dump())
    db.add(e)
    db.commit()
    db.refresh(e)
    return {"id": e.id}


@app.delete("/api/eggs/{eid}")
def delete_egg(eid: int, db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    e = db.query(Egg).get(eid)
    if not e:
        raise HTTPException(404, "Not found")
    db.delete(e)
    db.commit()
    return {"ok": True}


# ---------- Servers API ----------
def _server_for(db: Session, sid: int, user: User, perm: str = None) -> Server:
    s = db.query(Server).get(sid)
    if not s:
        raise HTTPException(404, "Server not found")
    if s.owner_id == user.id or user.is_admin:
        return s
    su = db.query(Subuser).filter(Subuser.server_id == sid, Subuser.user_id == user.id).first()
    if not su:
        raise HTTPException(403, "Forbidden")
    if perm and perm not in (su.permissions or "").split(","):
        raise HTTPException(403, f"Missing permission: {perm}")
    return s


def _parse_json(text: str, default):
    try:
        return json.loads(text) if text else default
    except (ValueError, TypeError):
        return default


def _clean_git_subdir(value: Optional[str]) -> str:
    text = (value or "").strip().replace("\\", "/")
    return text.strip("/")


def _git_output(stdout: str, stderr: str) -> str:
    parts = [part.strip() for part in (stdout or "", stderr or "") if part and part.strip()]
    return "\n".join(parts).strip()


def _safe_git_label(repo: str) -> str:
    text = (repo or "").strip()
    if "://" in text and "@" in text.split("://", 1)[1].split("/", 1)[0]:
        proto, rest = text.split("://", 1)
        host_and_path = rest.split("@", 1)[1]
        return f"{proto}://***@{host_and_path}"
    return text


def _record_git_output(sid: int, title: str, output: str):
    output = (output or "").strip()
    if output:
        dm.append_event(sid, f"{title}\n{output}")
    else:
        dm.append_event(sid, title)


def _run_git(args: list[str], cwd: Path, check: bool = True):
    if not shutil.which("git"):
        raise HTTPException(500, "Git is not installed on the panel host")

    # Security: prevent option injection by ensuring no argument starts with '-' 
    # unless it's a known safe command/flag we explicitly provided.
    # However, since we control 'args' in our calling code, we'll focus on 
    # using '--' where user input is involved.

    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Git operation timed out")

    output = _git_output(proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        # Don't leak full command in error if it might contain secrets (though unlikely here)
        raise HTTPException(400, output or "Git command failed")
    return proc.returncode, output


def _sync_server_repo(s: Server) -> str:
    repo = (s.git_repo or "").strip()
    if not s.git_auto_update:
        msg = "Git: auto-update is disabled for this server"
        dm.append_event(s.id, msg)
        return msg
    if not repo:
        msg = "Git: auto-update is enabled, but repository URL is empty"
        dm.append_event(s.id, msg)
        return msg
    
    if repo.startswith("-"):
        dm.append_event(s.id, "Git: invalid repository URL")
        raise HTTPException(400, "Invalid git repository URL")

    worktree = fs._safe(s.id, _clean_git_subdir(s.git_subdir))
    worktree.mkdir(parents=True, exist_ok=True)
    branch = (s.git_branch or "").strip()
    if branch.startswith("-"):
        dm.append_event(s.id, "Git: invalid branch name")
        raise HTTPException(400, "Invalid git branch name")
    
    messages = [f"Git: auto-update enabled for {_safe_git_label(repo)}"]
    if branch:
        messages.append(f"Git: target branch is '{branch}'")
    if _clean_git_subdir(s.git_subdir):
        messages.append(f"Git: target folder is '{_clean_git_subdir(s.git_subdir)}'")
    dm.append_event(s.id, "\n".join(messages))

    if (worktree / ".git").exists():
        dm.append_event(s.id, "Git: existing repository found, checking status")
        _, origin = _run_git(["config", "--get", "remote.origin.url"], worktree, check=False)
        if origin and origin.strip() != repo:
            dm.append_event(s.id, "Git: configured URL does not match this folder's origin remote")
            raise HTTPException(400, "Configured Git URL does not match this folder's origin remote")

        _, dirty = _run_git(["status", "--porcelain"], worktree, check=False)
        if dirty.strip():
            _record_git_output(s.id, "Git: local changes detected; auto-update stopped", dirty)
            raise HTTPException(400, "Git auto-update stopped: repository has local changes")

        if branch:
            _record_git_output(s.id, f"Git: fetching origin/{branch}", "")
            messages.append(_run_git(["fetch", "origin", branch], worktree)[1])
            code, _ = _run_git(["checkout", branch], worktree, check=False)
            if code != 0:
                # Use origin/{branch} safely with --
                dm.append_event(s.id, f"Git: creating local branch '{branch}' from origin/{branch}")
                messages.append(_run_git(["checkout", "-b", branch, f"origin/{branch}"], worktree)[1])
            _record_git_output(s.id, f"Git: pulling origin/{branch}", "")
            pull_output = _run_git(["pull", "--ff-only", "origin", branch], worktree)[1]
            messages.append(pull_output)
            _record_git_output(s.id, "Git: pull result", pull_output or "Already up to date")
        else:
            _record_git_output(s.id, "Git: pulling current branch", "")
            pull_output = _run_git(["pull", "--ff-only"], worktree)[1]
            messages.append(pull_output)
            _record_git_output(s.id, "Git: pull result", pull_output or "Already up to date")
    else:
        if any(worktree.iterdir()):
            dm.append_event(s.id, "Git: target folder is not empty; clone stopped")
            raise HTTPException(400, "Git auto-update target folder is not empty")

        clone_args = ["clone"]
        if branch:
            clone_args += ["--branch", branch, "--single-branch"]
        # Security: use -- to separate options from URL and path
        clone_args += ["--", repo, "."]
        dm.append_event(s.id, "Git: repository not found locally, cloning")
        clone_output = _run_git(clone_args, worktree)[1]
        messages.append(clone_output)
        _record_git_output(s.id, "Git: clone result", clone_output or "Clone complete")

    _, status = _run_git(["status", "-sb"], worktree, check=False)
    _, head = _run_git(["rev-parse", "--short", "HEAD"], worktree, check=False)
    if head:
        messages.append(f"Git HEAD: {head.strip()}")
        dm.append_event(s.id, f"Git: current commit {head.strip()}")
    if status:
        messages.append(f"Git status:\n{status}")
        _record_git_output(s.id, "Git: status after update", status)
    dm.append_event(s.id, "Git: auto-update complete")

    return "\n".join(msg for msg in messages if msg).strip()


def _server_dto(s: Server) -> dict:
    return {
        "id": s.id, "name": s.name, "status": s.status, "owner_id": s.owner_id,
        "egg": {"id": s.egg.id, "name": s.egg.name, "language": s.egg.language,
                "docker_image": s.egg.docker_image} if s.egg else None,
        "memory_mb": s.memory_mb, "cpu_limit": s.cpu_limit, "disk_mb": s.disk_mb,
        "startup_cmd": s.startup_cmd or (s.egg.default_cmd if s.egg else ""),
        "ports": _parse_json(s.ports, []),
        "env_vars": _parse_json(s.env_vars, {}),
        "git_repo": s.git_repo or "",
        "git_branch": s.git_branch or "",
        "git_subdir": _clean_git_subdir(s.git_subdir),
        "git_auto_update": bool(s.git_auto_update),
    }


@app.get("/api/servers")
def list_servers(db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    if user.is_admin:
        servers = db.query(Server).all()
    else:
        owned = db.query(Server).filter(Server.owner_id == user.id).all()
        shared_ids = [su.server_id for su in db.query(Subuser).filter(Subuser.user_id == user.id).all()]
        shared = db.query(Server).filter(Server.id.in_(shared_ids)).all() if shared_ids else []
        servers = owned + shared
    out = []
    for s in servers:
        s.status = dm.status(s.id)
        out.append(_server_dto(s))
    db.commit()
    return out


@app.post("/api/servers")
def create_server(body: ServerCreate, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    egg = db.query(Egg).get(body.egg_id)
    if not egg:
        raise HTTPException(404, "Egg not found")
    git_repo = (body.git_repo or "").strip()
    git_subdir = _clean_git_subdir(body.git_subdir)
    if body.git_auto_update and not git_repo:
        raise HTTPException(400, "Git repository URL is required for auto-update")
    s = Server(
        name=body.name, owner_id=user.id, egg_id=egg.id,
        memory_mb=body.memory_mb, cpu_limit=body.cpu_limit, disk_mb=body.disk_mb,
        startup_cmd=body.startup_cmd or egg.default_cmd,
        ports=json.dumps([p.model_dump() for p in body.ports]),
        env_vars=json.dumps(body.env_vars or {}),
        git_repo=git_repo,
        git_branch=(body.git_branch or "").strip(),
        git_subdir=git_subdir,
        git_auto_update=body.git_auto_update,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    s.data_dir = str(dm.server_dir(s.id))
    # стартовые файлы — содержат бесконечный цикл чтобы контейнер не завершался
    if not git_repo and egg.language == "python":
        fs.write_file(s.id, "main.py",
            'print("Hello from Panel!")\n\n'
            '# Держи контейнер живым — добавь сюда свой код\n'
            'import time\nwhile True:\n    time.sleep(60)\n')
    elif not git_repo and egg.language == "javascript":
        fs.write_file(s.id, "index.js",
            'console.log("Hello from Panel!");\n\n'
            '// Держи контейнер живым\n'
            'setInterval(() => {}, 60000);\n')
    elif not git_repo and egg.language == "go":
        fs.write_file(s.id, "main.go",
            'package main\nimport ("fmt";"time")\n'
            'func main(){\n  fmt.Println("Hello from Panel!")\n'
            '  for { time.Sleep(60 * time.Second) }\n}\n')
    elif not git_repo and egg.language == "bash":
        fs.write_file(s.id, "start.sh",
            '#!/bin/bash\necho "Hello from Panel!"\n\n'
            '# Держи контейнер живым\nwhile true; do sleep 60; done\n')
    db.commit()
    return _server_dto(s)


@app.get("/api/servers/{sid}")
def get_server(sid: int, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user)
    s.status = dm.status(s.id)
    db.commit()
    return _server_dto(s)


@app.delete("/api/servers/{sid}")
def delete_server(sid: int, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user)
    dm.remove(s.id)
    db.delete(s)
    db.commit()
    return {"ok": True}


@app.post("/api/servers/{sid}/power/{action}")
def power(sid: int, action: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user)
    labels = {
        "start": "start",
        "stop": "stop",
        "restart": "restart",
        "kill": "kill",
        "rebuild": "rebuild",
    }
    if action not in labels:
        raise HTTPException(400, "Unknown action")
    dm.append_event(s.id, f"Power: {labels[action]} requested by {user.username}")
    if not dm.docker_available():
        dm.append_event(s.id, "Docker: not available on this host")
        raise HTTPException(503, "Docker is not available on this host")
    ports = _parse_json(s.ports, [])
    env = _parse_json(s.env_vars, {})
    cmd = s.startup_cmd or s.egg.default_cmd
    git_message = ""
    try:
        if action == "start":
            git_message = _sync_server_repo(s)
            if not dm.inspect(s.id):
                dm.append_event(s.id, "Docker: no existing container, creating one")
                dm.create_container(s.id, s.egg.docker_image, cmd, s.memory_mb, s.cpu_limit, ports, env)
            dm.start(s.id)
        elif action == "stop":
            dm.stop(s.id)
        elif action == "restart":
            git_message = _sync_server_repo(s)
            if not dm.inspect(s.id):
                dm.append_event(s.id, "Docker: no existing container, creating one")
                dm.create_container(s.id, s.egg.docker_image, cmd, s.memory_mb, s.cpu_limit, ports, env)
                dm.start(s.id)
            else:
                dm.restart(s.id)
        elif action == "kill":
            dm.kill(s.id)
        elif action == "rebuild":
            git_message = _sync_server_repo(s)
            dm.remove_container(s.id)
            dm.create_container(s.id, s.egg.docker_image, cmd, s.memory_mb, s.cpu_limit, ports, env)
        s.status = dm.status(s.id)
        dm.append_event(s.id, f"Power: {labels[action]} finished, status={s.status}")
        db.commit()
        return {"status": s.status, "message": git_message}
    except HTTPException as e:
        dm.append_event(s.id, f"Power: {labels[action]} failed: {e.detail}")
        raise
    except Exception as e:
        dm.append_event(s.id, f"Power: {labels[action]} failed: {e}")
        raise


@app.get("/api/servers/{sid}/stats")
def server_stats(sid: int, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user)
    return dm.stats(s.id, record=False)


@app.get("/api/servers/{sid}/logs")
def server_logs(sid: int, tail: int = 200, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user)
    return {"logs": dm.logs(s.id, tail=tail)}


# ---------- Files API ----------
@app.get("/api/servers/{sid}/files")
def list_files(sid: int, path: str = "", db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user)
    return {"path": path, "items": fs.list_dir(s.id, path)}


@app.get("/api/servers/{sid}/files/read")
def read_file(sid: int, path: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user)
    return {"path": path, "content": fs.read_file(s.id, path)}


@app.post("/api/servers/{sid}/files/write")
def write_file(sid: int, body: FileWrite, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user)
    fs.write_file(s.id, body.path, body.content)
    return {"ok": True}


@app.post("/api/servers/{sid}/files/delete")
def delete_file(sid: int, body: PathIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user)
    fs.delete_path(s.id, body.path)
    return {"ok": True}


@app.post("/api/servers/{sid}/files/mkdir")
def mkdir(sid: int, body: PathIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user)
    fs.create_dir(s.id, body.path)
    return {"ok": True}


@app.post("/api/servers/{sid}/files/rename")
def rename(sid: int, body: RenameIn, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user)
    fs.rename_path(s.id, body.path, body.new_path)
    return {"ok": True}


class ExtractIn(BaseModel):
    path: str
    dest: str = ""


@app.post("/api/servers/{sid}/files/extract")
def extract(sid: int, body: ExtractIn, db: Session = Depends(get_db),
            user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user, "files")
    fname = body.path.split("/")[-1]
    tid = tk.create(f"Распаковка: {fname}", server_id=s.id)

    def _run():
        try:
            tk.update(tid, progress=5, message="Открываю архив…")
            count = fs.extract_archive(s.id, body.path, body.dest)
            tk.finish(tid, message=f"Готово — {count} файлов")
        except Exception as e:
            tk.fail(tid, message=str(e))

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "task_id": tid}


# ---------- Users Admin ----------
@app.get("/api/users")
def list_users(db: Session = Depends(get_db), _: User = Depends(auth.require_admin)):
    return [{"id": u.id, "username": u.username, "email": u.email, "is_admin": u.is_admin,
             "created_at": u.created_at.isoformat()} for u in db.query(User).all()]


@app.delete("/api/users/{uid}")
def delete_user(uid: int, db: Session = Depends(get_db), admin: User = Depends(auth.require_admin)):
    if uid == admin.id:
        raise HTTPException(400, "Cannot delete yourself")
    u = db.query(User).get(uid)
    if not u:
        raise HTTPException(404, "Not found")
    for s in list(u.servers):
        dm.remove(s.id)
    db.delete(u)
    db.commit()
    return {"ok": True}


# ---------- System ----------
@app.get("/api/system")
def system_info(_: User = Depends(auth.get_current_user)):
    info = {"docker": dm.docker_available()}
    if info["docker"]:
        try:
            d = dm.client().info()
            info.update({
                "containers": d.get("Containers", 0),
                "containers_running": d.get("ContainersRunning", 0),
                "images": d.get("Images", 0),
                "kernel": d.get("KernelVersion", ""),
                "os": d.get("OperatingSystem", ""),
                "cpus": d.get("NCPU", 0),
                "memory": d.get("MemTotal", 0),
            })
        except Exception as e:
            info["error"] = str(e)
    return info


# ---------- WebSocket консоль ----------
@app.websocket("/ws/servers/{sid}/console")
async def console_ws(websocket: WebSocket, sid: int, token: Optional[str] = None):
    await websocket.accept()
    # авторизация: токен из query или cookie
    tok = token or websocket.cookies.get("panel_token")
    payload = auth.decode_token(tok) if tok else None
    if not payload:
        await websocket.send_json({"type": "error", "message": "unauthorized"})
        await websocket.close()
        return

    db = SessionLocal()
    try:
        s = db.query(Server).get(sid)
        if not s:
            await websocket.send_json({"type": "error", "message": "server not found"})
            await websocket.close()
            return
        user = db.query(User).get(int(payload["sub"]))
        if not user:
            await websocket.send_json({"type": "error", "message": "forbidden"})
            await websocket.close()
            return
        if s.owner_id != user.id and not user.is_admin:
            su = db.query(Subuser).filter(Subuser.server_id == sid, Subuser.user_id == user.id).first()
            if not su or "console" not in (su.permissions or "").split(","):
                await websocket.send_json({"type": "error", "message": "forbidden"})
                await websocket.close()
                return
    finally:
        db.close()

    event_tail = dm.read_events(sid)
    if event_tail:
        await websocket.send_json({"type": "log", "data": event_tail})
    event_offset = dm.event_log_size(sid)

    if not dm.inspect(sid):
        await websocket.send_json({"type": "log", "data": "[container is not running]\n"})

    async def pump_events():
        nonlocal event_offset
        try:
            while True:
                chunk, event_offset = dm.read_event_chunk(sid, event_offset)
                if chunk:
                    await websocket.send_json({"type": "log", "data": chunk})
                await asyncio.sleep(0.5)
        except Exception:
            pass

    async def pump_container_logs():
        loop = asyncio.get_event_loop()
        try:
            while True:
                stream = dm.attach_stream(sid, tail=0, since=int(time.time()))
                if stream is None:
                    await asyncio.sleep(1.0)
                    continue
                while True:
                    chunk = await loop.run_in_executor(None, lambda: next(stream, None))
                    if chunk is None:
                        break
                    text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
                    await websocket.send_json({"type": "log", "data": text})
                await asyncio.sleep(0.5)
        except Exception:
            pass

    event_task = asyncio.create_task(pump_events())
    log_task = asyncio.create_task(pump_container_logs())

    try:
        while True:
            msg = await websocket.receive_json()
            if msg.get("type") == "cmd":
                cmd = msg.get("data", "")
                out = dm.exec_command(sid, cmd)
                await websocket.send_json({"type": "log", "data": f"$ {cmd}\n{out}"})
            elif msg.get("type") == "stats":
                await websocket.send_json({"type": "stats", "data": dm.stats(sid, record=False)})
    except WebSocketDisconnect:
        pass
    finally:
        event_task.cancel()
        log_task.cancel()


@app.patch("/api/servers/{sid}")
def update_server(sid: int, body: ServerUpdate, db: Session = Depends(get_db),
                  user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user)
    if body.name is not None: s.name = body.name
    if body.memory_mb is not None: s.memory_mb = body.memory_mb
    if body.cpu_limit is not None: s.cpu_limit = body.cpu_limit
    if body.disk_mb is not None: s.disk_mb = body.disk_mb
    if body.startup_cmd is not None: s.startup_cmd = body.startup_cmd
    if body.ports is not None: s.ports = json.dumps([p.model_dump() for p in body.ports])
    if body.env_vars is not None: s.env_vars = json.dumps(body.env_vars)
    if body.git_repo is not None: s.git_repo = (body.git_repo or "").strip()
    if body.git_branch is not None: s.git_branch = (body.git_branch or "").strip()
    if body.git_subdir is not None: s.git_subdir = _clean_git_subdir(body.git_subdir)
    if body.git_auto_update is not None: s.git_auto_update = body.git_auto_update
    if s.git_auto_update and not (s.git_repo or "").strip():
        raise HTTPException(400, "Git repository URL is required for auto-update")
    db.commit()
    return _server_dto(s)


# ---------- Files: upload / download ----------
@app.post("/api/servers/{sid}/files/upload")
async def upload_file(sid: int, path: str = "", file: UploadFile = File(...),
                      db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user, "files")
    target_rel = (path.rstrip("/\\") + "/" + file.filename) if path else file.filename
    tid = tk.create(f"Загрузка: {file.filename}", server_id=s.id)
    tk.update(tid, progress=10, message="Принимаю файл…")
    content = await file.read()
    if len(content) > 100 * 1024 * 1024:
        tk.fail(tid, "Файл >100MB")
        raise HTTPException(413, "File too large (>100MB)")
    tk.update(tid, progress=70, message="Сохраняю…")
    p = fs._safe(s.id, target_rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    tk.finish(tid, message=f"Загружено: {target_rel}")
    return {"ok": True, "path": target_rel, "size": len(content), "task_id": tid}


@app.get("/api/servers/{sid}/files/download")
def download_file(sid: int, path: str, db: Session = Depends(get_db),
                  user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user, "files")
    p = fs._safe(s.id, path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(p, filename=p.name)


# ---------- Backups ----------
@app.get("/api/servers/{sid}/backups")
def list_backups(sid: int, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user, "backups")
    return [{"id": b.id, "name": b.name, "filename": b.filename, "size": b.size,
             "created_at": b.created_at.isoformat()} for b in s.backups]


@app.post("/api/servers/{sid}/backups")
def create_backup(sid: int, body: BackupIn, db: Session = Depends(get_db),
                  user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user, "backups")
    fname, size = bk.create_backup(s.id, body.name)
    b = Backup(server_id=s.id, name=body.name, filename=fname, size=size)
    db.add(b)
    db.commit()
    db.refresh(b)
    return {"id": b.id, "filename": b.filename, "size": b.size}


@app.post("/api/servers/{sid}/backups/{bid}/restore")
def restore_backup(sid: int, bid: int, db: Session = Depends(get_db),
                   user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user, "backups")
    b = db.query(Backup).get(bid)
    if not b or b.server_id != s.id:
        raise HTTPException(404, "Not found")
    bk.restore_backup(s.id, b.filename)
    return {"ok": True}


@app.delete("/api/servers/{sid}/backups/{bid}")
def del_backup(sid: int, bid: int, db: Session = Depends(get_db),
               user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user, "backups")
    b = db.query(Backup).get(bid)
    if not b or b.server_id != s.id:
        raise HTTPException(404, "Not found")
    bk.delete_backup(s.id, b.filename)
    db.delete(b)
    db.commit()
    return {"ok": True}


@app.get("/api/servers/{sid}/backups/{bid}/download")
def download_backup(sid: int, bid: int, db: Session = Depends(get_db),
                    user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user, "backups")
    b = db.query(Backup).get(bid)
    if not b or b.server_id != s.id:
        raise HTTPException(404, "Not found")
    p = bk.backup_path(s.id, b.filename)
    if not p.exists():
        raise HTTPException(404, "File missing")
    return FileResponse(p, filename=b.filename, media_type="application/gzip")


# ---------- Subusers ----------
@app.get("/api/servers/{sid}/subusers")
def list_subusers(sid: int, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user)
    if s.owner_id != user.id and not user.is_admin:
        raise HTTPException(403, "Owner only")
    return [{"id": su.id, "user_id": su.user_id, "username": su.user.username,
             "permissions": su.permissions} for su in s.subusers]


@app.post("/api/servers/{sid}/subusers")
def add_subuser(sid: int, body: SubuserIn, db: Session = Depends(get_db),
                user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user)
    if s.owner_id != user.id and not user.is_admin:
        raise HTTPException(403, "Owner only")
    target = db.query(User).filter(User.username == body.username).first()
    if not target:
        raise HTTPException(404, "User not found")
    if target.id == s.owner_id:
        raise HTTPException(400, "Owner cannot be a subuser")
    if db.query(Subuser).filter(Subuser.server_id == s.id, Subuser.user_id == target.id).first():
        raise HTTPException(400, "Already a subuser")
    su = Subuser(server_id=s.id, user_id=target.id, permissions=body.permissions)
    db.add(su)
    db.commit()
    return {"id": su.id}


@app.delete("/api/servers/{sid}/subusers/{suid}")
def del_subuser(sid: int, suid: int, db: Session = Depends(get_db),
                user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user)
    if s.owner_id != user.id and not user.is_admin:
        raise HTTPException(403, "Owner only")
    su = db.query(Subuser).get(suid)
    if not su or su.server_id != s.id:
        raise HTTPException(404, "Not found")
    db.delete(su)
    db.commit()
    return {"ok": True}


# ---------- Schedules ----------
@app.get("/api/servers/{sid}/schedules")
def list_schedules(sid: int, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user, "schedules")
    return [{"id": sc.id, "name": sc.name, "cron": sc.cron, "action": sc.action,
             "payload": sc.payload, "enabled": sc.enabled,
             "last_run": sc.last_run.isoformat() if sc.last_run else None} for sc in s.schedules]


@app.post("/api/servers/{sid}/schedules")
def add_schedule(sid: int, body: ScheduleIn, db: Session = Depends(get_db),
                 user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user, "schedules")
    if len(body.cron.split()) != 5:
        raise HTTPException(400, "Cron must have 5 fields: m h dom mon dow")
    sc = Schedule(server_id=s.id, name=body.name, cron=body.cron, action=body.action,
                  payload=body.payload, enabled=body.enabled)
    db.add(sc)
    db.commit()
    db.refresh(sc)
    return {"id": sc.id}


@app.delete("/api/servers/{sid}/schedules/{scid}")
def del_schedule(sid: int, scid: int, db: Session = Depends(get_db),
                 user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user, "schedules")
    sc = db.query(Schedule).get(scid)
    if not sc or sc.server_id != s.id:
        raise HTTPException(404, "Not found")
    db.delete(sc)
    db.commit()
    return {"ok": True}


# ---------- xterm.js PTY console ----------
@app.websocket("/ws/servers/{sid}/term")
async def term_ws(websocket: WebSocket, sid: int, token: Optional[str] = None,
                  shell: str = "/bin/sh"):
    await websocket.accept()
    tok = token or websocket.cookies.get("panel_token")
    payload = auth.decode_token(tok) if tok else None
    if not payload:
        await websocket.close(code=4401)
        return

    db = SessionLocal()
    try:
        s = db.query(Server).get(sid)
        u = db.query(User).get(int(payload["sub"]))
        if not s or not u:
            await websocket.close(code=4404); return
        if s.owner_id != u.id and not u.is_admin:
            su = db.query(Subuser).filter(Subuser.server_id == sid, Subuser.user_id == u.id).first()
            if not su or "console" not in (su.permissions or "").split(","):
                await websocket.close(code=4403); return
    finally:
        db.close()

    exec_id, sock = dm.exec_interactive(sid, shell)
    if sock is None:
        await websocket.send_text("[container is not running — start it first]\r\n")
        await websocket.close()
        return

    sock.setblocking(False)
    loop = asyncio.get_event_loop()

    async def reader():
        try:
            while True:
                try:
                    data = await loop.run_in_executor(None, lambda: sock.recv(4096))
                    if not data:
                        break
                    await websocket.send_bytes(data)
                except (BlockingIOError, OSError):
                    await asyncio.sleep(0.05)
        except Exception:
            pass

    rtask = asyncio.create_task(reader())
    try:
        while True:
            msg = await websocket.receive()
            if "text" in msg and msg["text"]:
                try:
                    j = json.loads(msg["text"])
                    if j.get("type") == "resize":
                        dm.exec_resize(exec_id, int(j.get("rows", 24)), int(j.get("cols", 80)))
                        continue
                    if j.get("type") == "input":
                        sock.send(j.get("data", "").encode("utf-8"))
                        continue
                except (ValueError, TypeError):
                    sock.send(msg["text"].encode("utf-8"))
            elif "bytes" in msg and msg["bytes"]:
                sock.send(msg["bytes"])
    except WebSocketDisconnect:
        pass
    finally:
        rtask.cancel()
        try: sock.close()
        except Exception: pass


# ---------- Tasks ----------
@app.get("/api/servers/{sid}/tasks")
def list_tasks(sid: int, db: Session = Depends(get_db),
               user: User = Depends(auth.get_current_user)):
    _server_for(db, sid, user)
    return tk.for_server(sid)


@app.delete("/api/tasks/{tid}")
def delete_task(tid: str, _: User = Depends(auth.get_current_user)):
    tk.delete(tid)
    return {"ok": True}


# ---------- Websites / Nginx ----------
def _split_domains(text: str) -> list[str]:
    if not text:
        return []
    return [d.strip() for d in text.replace(",", " ").split() if d.strip()]


def _website_dto(w: Website) -> dict:
    return {
        "id": w.id, "name": w.name, "domain": w.domain,
        "domains": _split_domains(w.domains or ""),
        "mode": w.mode or "proxy",
        "listen_port": w.listen_port or 80,
        "proxy_pass": w.proxy_pass or "", "nginx_extra": w.nginx_extra or "",
        "ssl_enabled": bool(w.ssl_enabled), "is_active": bool(w.is_active),
        "git_repo": w.git_repo or "",
        "git_branch": w.git_branch or "",
        "web_subdir": w.web_subdir or "",
        "runtime_enabled": bool(getattr(w, "runtime_enabled", False)),
        "runtime_cwd": getattr(w, "runtime_cwd", "") or "",
        "runtime_install_cmd": getattr(w, "runtime_install_cmd", "") or "",
        "runtime_start_cmd": getattr(w, "runtime_start_cmd", "") or "",
        "runtime_port": getattr(w, "runtime_port", 0) or 0,
        "runtime_env": getattr(w, "runtime_env", "") or "",
        "runtime_status": srt.status(w.id),
        "webroot": str(sfs.site_dir(w.id)) if (w.mode or "proxy") == "static" else "",
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }


def _apply_website(w: Website):
    """Write config + (en|dis)able + reload. Raises HTTPException on failure."""
    try:
        webroot = ""
        if (w.mode or "proxy") == "static":
            base = sfs.site_dir(w.id)
            sub = (w.web_subdir or "").strip().strip("/\\")
            if sub:
                resolved = (base / sub).resolve()
                if not str(resolved).startswith(str(base.resolve())):
                    raise HTTPException(400, "web_subdir escapes webroot")
                webroot = str(resolved)
            else:
                webroot = str(base)
        cfg = nm.generate_config(
            domain=w.domain,
            proxy_pass=w.proxy_pass or "",
            extra_config=w.nginx_extra or "",
            ssl=bool(w.ssl_enabled),
            extra_domains=_split_domains(w.domains or ""),
            mode=w.mode or "proxy",
            listen_port=w.listen_port or 80,
            webroot=webroot,
        )
        nm.write_config(w.id, cfg)
        if w.is_active:
            nm.enable_site(w.id)
        else:
            nm.disable_site(w.id)
        ok, msg = nm.reload_nginx()
        if not ok:
            raise HTTPException(400, f"nginx reload failed: {msg}")
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.get("/api/websites")
def list_websites(db: Session = Depends(get_db), _: User = Depends(auth.require_admin),
                  __=Depends(_require_flag("experimental_websites"))):
    return [_website_dto(w) for w in db.query(Website).order_by(Website.id.desc()).all()]


@app.post("/api/websites")
def create_website(body: WebsiteIn, db: Session = Depends(get_db), _: User = Depends(auth.require_admin),
                   __=Depends(_require_flag("experimental_websites"))):
    if body.mode not in ("proxy", "static"):
        raise HTTPException(400, "mode must be proxy|static")
    if body.mode == "proxy" and not body.proxy_pass:
        raise HTTPException(400, "proxy_pass required for proxy mode")
    if db.query(Website).filter(Website.domain == body.domain).first():
        raise HTTPException(400, "Domain already exists")
    data = body.model_dump()
    data["domains"] = ",".join(body.domains or [])
    w = Website(**data)
    db.add(w)
    db.commit()
    db.refresh(w)
    if w.mode == "static":
        sfs.site_dir(w.id)  # ensure webroot
        if (w.git_repo or "").strip():
            try:
                _website_git_sync(w)
            except HTTPException:
                shutil.rmtree(sfs.site_dir(w.id), ignore_errors=True)
                db.delete(w)
                db.commit()
                raise
    try:
        _apply_website(w)
    except HTTPException:
        nm.delete_config(w.id)
        db.delete(w)
        db.commit()
        raise
    return _website_dto(w)


@app.get("/api/websites/{wid}")
def get_website(wid: int, db: Session = Depends(get_db), _: User = Depends(auth.require_admin),
                   __=Depends(_require_flag("experimental_websites"))):
    w = db.query(Website).get(wid)
    if not w:
        raise HTTPException(404, "Not found")
    return _website_dto(w)


@app.patch("/api/websites/{wid}")
def update_website(wid: int, body: WebsiteUpdate, db: Session = Depends(get_db),
                   _: User = Depends(auth.require_admin),
                   __=Depends(_require_flag("experimental_websites"))):
    w = db.query(Website).get(wid)
    if not w:
        raise HTTPException(404, "Not found")
    data = body.model_dump(exclude_unset=True)
    if "domain" in data and data["domain"] != w.domain:
        if db.query(Website).filter(Website.domain == data["domain"]).first():
            raise HTTPException(400, "Domain already exists")
    if "domains" in data:
        data["domains"] = ",".join(data["domains"] or [])
    for k, v in data.items():
        setattr(w, k, v)
    db.commit()
    _apply_website(w)
    return _website_dto(w)


@app.delete("/api/websites/{wid}")
def delete_website(wid: int, db: Session = Depends(get_db), _: User = Depends(auth.require_admin),
                   __=Depends(_require_flag("experimental_websites"))):
    w = db.query(Website).get(wid)
    if not w:
        raise HTTPException(404, "Not found")
    nm.delete_config(w.id)
    # remove webroot if any
    try:
        wr = sfs.site_dir(w.id)
        if wr.exists():
            shutil.rmtree(wr, ignore_errors=True)
    except Exception:
        pass
    db.delete(w)
    db.commit()
    nm.reload_nginx()
    return {"ok": True}


# ---------- Website files (static mode) ----------
@app.get("/api/websites/{wid}/files")
def site_list_files(wid: int, path: str = "", db: Session = Depends(get_db),
                    _: User = Depends(auth.require_admin),
                   __=Depends(_require_flag("experimental_websites"))):
    w = db.query(Website).get(wid)
    if not w:
        raise HTTPException(404, "Not found")
    return {"path": path, "items": sfs.list_dir(w.id, path)}


@app.post("/api/websites/{wid}/files/delete")
def site_delete_file(wid: int, body: PathIn, db: Session = Depends(get_db),
                     _: User = Depends(auth.require_admin),
                   __=Depends(_require_flag("experimental_websites"))):
    w = db.query(Website).get(wid)
    if not w:
        raise HTTPException(404, "Not found")
    sfs.delete_path(w.id, body.path)
    return {"ok": True}


@app.post("/api/websites/{wid}/files/mkdir")
def site_mkdir(wid: int, body: PathIn, db: Session = Depends(get_db),
               _: User = Depends(auth.require_admin),
                   __=Depends(_require_flag("experimental_websites"))):
    w = db.query(Website).get(wid)
    if not w:
        raise HTTPException(404, "Not found")
    sfs.create_dir(w.id, body.path)
    return {"ok": True}


@app.post("/api/websites/{wid}/files/upload")
async def site_upload(wid: int, path: str = "", file: UploadFile = File(...),
                      auto_extract: bool = True,
                      db: Session = Depends(get_db), _: User = Depends(auth.require_admin),
                   __=Depends(_require_flag("experimental_websites"))):
    w = db.query(Website).get(wid)
    if not w:
        raise HTTPException(404, "Not found")
    target_rel = (path.rstrip("/\\") + "/" + file.filename) if path else file.filename
    content = await file.read()
    if len(content) > 200 * 1024 * 1024:
        raise HTTPException(413, "File too large (>200MB)")
    p = sfs._safe(w.id, target_rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    extracted = 0
    if auto_extract and sfs.is_archive(file.filename):
        try:
            extracted = sfs.extract_archive(w.id, target_rel)
            p.unlink()
        except HTTPException:
            raise
    return {"ok": True, "path": target_rel, "size": len(content), "extracted": extracted}


@app.post("/api/websites/{wid}/files/extract")
def site_extract(wid: int, body: PathIn, db: Session = Depends(get_db),
                 _: User = Depends(auth.require_admin),
                   __=Depends(_require_flag("experimental_websites"))):
    w = db.query(Website).get(wid)
    if not w:
        raise HTTPException(404, "Not found")
    count = sfs.extract_archive(w.id, body.path)
    return {"ok": True, "extracted": count}


@app.post("/api/websites/{wid}/toggle")
def toggle_website(wid: int, db: Session = Depends(get_db), _: User = Depends(auth.require_admin),
                   __=Depends(_require_flag("experimental_websites"))):
    w = db.query(Website).get(wid)
    if not w:
        raise HTTPException(404, "Not found")
    w.is_active = not bool(w.is_active)
    db.commit()
    _apply_website(w)
    return _website_dto(w)


@app.post("/api/websites/{wid}/ssl")
def website_issue_ssl(wid: int, db: Session = Depends(get_db), _: User = Depends(auth.require_admin),
                   __=Depends(_require_flag("experimental_websites"))):
    w = db.query(Website).get(wid)
    if not w:
        raise HTTPException(404, "Not found")
    ok, msg = nm.issue_ssl(w.domain)
    if not ok:
        raise HTTPException(400, msg)
    w.ssl_enabled = True
    db.commit()
    _apply_website(w)
    return {"ok": True, "message": msg}


def _website_git_sync(w: Website) -> str:
    """Clone (if empty) or pull (if already a git repo) into the site webroot.
    Returns combined git output. Static-mode only."""
    if (w.mode or "proxy") != "static":
        raise HTTPException(400, "Git sync is only available for static sites")
    repo = (w.git_repo or "").strip()
    if not repo:
        raise HTTPException(400, "Git repository is not configured for this site")
    
    if repo.startswith("-"):
        raise HTTPException(400, "Invalid git repository URL")

    worktree = sfs.site_dir(w.id)
    branch = (w.git_branch or "").strip()
    if branch.startswith("-"):
        raise HTTPException(400, "Invalid git branch name")
    
    messages = []

    if (worktree / ".git").exists():
        _, origin = _run_git(["config", "--get", "remote.origin.url"], worktree, check=False)
        if origin and origin.strip() != repo:
            _run_git(["remote", "set-url", "origin", "--", repo], worktree)
        if branch:
            messages.append(_run_git(["fetch", "origin", branch], worktree)[1])
            code, _ = _run_git(["checkout", branch], worktree, check=False)
            if code != 0:
                _run_git(["checkout", "-b", branch, f"origin/{branch}"], worktree)
            messages.append(_run_git(["reset", "--hard", f"origin/{branch}"], worktree)[1])
        else:
            messages.append(_run_git(["pull", "--ff-only"], worktree)[1])
    else:
        # initial clone — webroot may have leftover files; require empty or wipe
        existing = [p for p in worktree.iterdir()] if worktree.exists() else []
        if existing:
            for p in existing:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    try: p.unlink()
                    except OSError: pass
        clone_args = ["clone"]
        if branch:
            clone_args += ["--branch", branch, "--single-branch"]
        clone_args += ["--", repo, "."]
        messages.append(_run_git(clone_args, worktree)[1])

    return "\n".join(m for m in messages if m).strip()


@app.get("/api/websites/{wid}/runtime/status")
def site_runtime_status(wid: int, db: Session = Depends(get_db),
                        _: User = Depends(auth.require_admin),
                        __=Depends(_require_flag("experimental_websites"))):
    w = db.query(Website).get(wid)
    if not w: raise HTTPException(404, "Not found")
    return srt.status(w.id)


@app.post("/api/websites/{wid}/runtime/start")
def site_runtime_start(wid: int, db: Session = Depends(get_db),
                       _: User = Depends(auth.require_admin),
                       __=Depends(_require_flag("experimental_websites"))):
    w = db.query(Website).get(wid)
    if not w: raise HTTPException(404, "Not found")
    ok, msg = srt.start(w)
    if not ok: raise HTTPException(400, msg)
    return {"ok": True, "message": msg, "status": srt.status(w.id)}


@app.post("/api/websites/{wid}/runtime/stop")
def site_runtime_stop(wid: int, db: Session = Depends(get_db),
                      _: User = Depends(auth.require_admin),
                      __=Depends(_require_flag("experimental_websites"))):
    w = db.query(Website).get(wid)
    if not w: raise HTTPException(404, "Not found")
    srt.stop(w.id)
    return {"ok": True, "status": srt.status(w.id)}


@app.post("/api/websites/{wid}/runtime/restart")
def site_runtime_restart(wid: int, db: Session = Depends(get_db),
                         _: User = Depends(auth.require_admin),
                         __=Depends(_require_flag("experimental_websites"))):
    w = db.query(Website).get(wid)
    if not w: raise HTTPException(404, "Not found")
    ok, msg = srt.restart(w)
    if not ok: raise HTTPException(400, msg)
    return {"ok": True, "message": msg, "status": srt.status(w.id)}


@app.post("/api/websites/{wid}/runtime/install")
def site_runtime_install(wid: int, db: Session = Depends(get_db),
                         _: User = Depends(auth.require_admin),
                         __=Depends(_require_flag("experimental_websites"))):
    w = db.query(Website).get(wid)
    if not w: raise HTTPException(404, "Not found")
    ok, msg = srt.run_install(w)
    if not ok: raise HTTPException(400, msg)
    return {"ok": True, "message": msg}


@app.get("/api/websites/{wid}/runtime/logs")
def site_runtime_logs(wid: int, db: Session = Depends(get_db),
                      _: User = Depends(auth.require_admin),
                      __=Depends(_require_flag("experimental_websites"))):
    w = db.query(Website).get(wid)
    if not w: raise HTTPException(404, "Not found")
    return {"text": srt.tail(w.id)}


@app.websocket("/ws/websites/{wid}/logs")
async def site_runtime_logs_ws(websocket: WebSocket, wid: int):
    # auth via cookie or token
    token = websocket.query_params.get("token") or websocket.cookies.get("panel_token")
    payload = auth.decode_token(token) if token else None
    if not payload:
        await websocket.close(code=4401); return
    db = SessionLocal()
    try:
        uid = payload.get("sub") or payload.get("uid")
        u = db.query(User).get(int(uid)) if uid else None
        if not u or not u.is_admin:
            await websocket.close(code=4403); return
        w = db.query(Website).get(wid)
        if not w:
            await websocket.close(code=4404); return
    finally:
        db.close()
    await websocket.accept()
    log_path = sfs.site_dir(wid) / ".runtime.log"
    last_size = 0
    # send initial tail
    try:
        await websocket.send_text(srt.tail(wid))
        if log_path.exists():
            last_size = log_path.stat().st_size
    except Exception:
        pass
    try:
        while True:
            await asyncio.sleep(1.0)
            if not log_path.exists():
                last_size = 0
                continue
            sz = log_path.stat().st_size
            if sz < last_size:
                last_size = 0
            if sz > last_size:
                with open(log_path, "rb") as f:
                    f.seek(last_size)
                    chunk = f.read(sz - last_size)
                last_size = sz
                try:
                    await websocket.send_text(chunk.decode("utf-8", errors="replace"))
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


@app.post("/api/websites/{wid}/git/sync")
def website_git_sync(wid: int, db: Session = Depends(get_db),
                     _: User = Depends(auth.require_admin),
                     __=Depends(_require_flag("experimental_websites"))):
    w = db.query(Website).get(wid)
    if not w:
        raise HTTPException(404, "Not found")
    output = _website_git_sync(w)
    return {"ok": True, "output": output or "Updated"}


@app.get("/api/nginx/status")
def nginx_status(_: User = Depends(auth.require_admin),
                 __=Depends(_require_flag("experimental_websites"))):
    return nm.nginx_status()


@app.post("/api/nginx/reload")
def nginx_reload(_: User = Depends(auth.require_admin),
                 __=Depends(_require_flag("experimental_websites"))):
    ok, msg = nm.reload_nginx()
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
