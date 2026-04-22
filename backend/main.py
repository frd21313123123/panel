"""Panel — браузерная панель управления кодовыми контейнерами (Pterodactyl-like)."""
import asyncio
import json
import os
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from database import init_db, get_db, User, Server, Egg, Subuser, Schedule, Backup, SessionLocal
import threading
import auth
import docker_manager as dm
import files as fs
import backups as bk
import scheduler
import tasks as tk

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(title="Panel", version="0.1.0")
templates = Jinja2Templates(directory=str(FRONTEND_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR / "static")), name="static")


@app.on_event("startup")
def startup():
    init_db()
    # создать админа по умолчанию, если нет пользователей
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            admin = User(
                username="admin",
                email="admin@panel.local",
                password_hash=auth.hash_password("admin"),
                is_admin=True,
            )
            db.add(admin)
            db.commit()
            print("[panel] default admin created: admin / admin")
    finally:
        db.close()
    scheduler.start_background()
    print("[panel] scheduler started")


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


class ServerUpdate(BaseModel):
    name: Optional[str] = None
    memory_mb: Optional[int] = None
    cpu_limit: Optional[int] = None
    disk_mb: Optional[int] = None
    startup_cmd: Optional[str] = None
    ports: Optional[List[PortDef]] = None
    env_vars: Optional[dict] = None


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


def _server_dto(s: Server) -> dict:
    return {
        "id": s.id, "name": s.name, "status": s.status, "owner_id": s.owner_id,
        "egg": {"id": s.egg.id, "name": s.egg.name, "language": s.egg.language,
                "docker_image": s.egg.docker_image} if s.egg else None,
        "memory_mb": s.memory_mb, "cpu_limit": s.cpu_limit, "disk_mb": s.disk_mb,
        "startup_cmd": s.startup_cmd or (s.egg.default_cmd if s.egg else ""),
        "ports": _parse_json(s.ports, []),
        "env_vars": _parse_json(s.env_vars, {}),
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
    s = Server(
        name=body.name, owner_id=user.id, egg_id=egg.id,
        memory_mb=body.memory_mb, cpu_limit=body.cpu_limit, disk_mb=body.disk_mb,
        startup_cmd=body.startup_cmd or egg.default_cmd,
        ports=json.dumps([p.model_dump() for p in body.ports]),
        env_vars=json.dumps(body.env_vars or {}),
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    s.data_dir = str(dm.server_dir(s.id))
    # стартовые файлы — содержат бесконечный цикл чтобы контейнер не завершался
    if egg.language == "python":
        fs.write_file(s.id, "main.py",
            'print("Hello from Panel!")\n\n'
            '# Держи контейнер живым — добавь сюда свой код\n'
            'import time\nwhile True:\n    time.sleep(60)\n')
    elif egg.language == "javascript":
        fs.write_file(s.id, "index.js",
            'console.log("Hello from Panel!");\n\n'
            '// Держи контейнер живым\n'
            'setInterval(() => {}, 60000);\n')
    elif egg.language == "go":
        fs.write_file(s.id, "main.go",
            'package main\nimport ("fmt";"time")\n'
            'func main(){\n  fmt.Println("Hello from Panel!")\n'
            '  for { time.Sleep(60 * time.Second) }\n}\n')
    elif egg.language == "bash":
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
    if not dm.docker_available():
        raise HTTPException(503, "Docker is not available on this host")
    ports = _parse_json(s.ports, [])
    env = _parse_json(s.env_vars, {})
    cmd = s.startup_cmd or s.egg.default_cmd
    if action == "start":
        if not dm.inspect(s.id):
            dm.create_container(s.id, s.egg.docker_image, cmd, s.memory_mb, s.cpu_limit, ports, env)
        dm.start(s.id)
    elif action == "stop":
        dm.stop(s.id)
    elif action == "restart":
        if not dm.inspect(s.id):
            dm.create_container(s.id, s.egg.docker_image, cmd, s.memory_mb, s.cpu_limit, ports, env)
            dm.start(s.id)
        else:
            dm.restart(s.id)
    elif action == "kill":
        dm.kill(s.id)
    elif action == "rebuild":
        dm.remove(s.id)
        dm.create_container(s.id, s.egg.docker_image, cmd, s.memory_mb, s.cpu_limit, ports, env)
    else:
        raise HTTPException(400, "Unknown action")
    s.status = dm.status(s.id)
    db.commit()
    return {"status": s.status}


@app.get("/api/servers/{sid}/stats")
def server_stats(sid: int, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)):
    s = _server_for(db, sid, user)
    return dm.stats(s.id)


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
        if not user or (s.owner_id != user.id and not user.is_admin):
            await websocket.send_json({"type": "error", "message": "forbidden"})
            await websocket.close()
            return
    finally:
        db.close()

    stream = dm.attach_stream(sid)
    if stream is None:
        await websocket.send_json({"type": "log", "data": "[container is not running]\n"})

    async def pump_logs():
        if stream is None:
            return
        loop = asyncio.get_event_loop()
        try:
            while True:
                chunk = await loop.run_in_executor(None, lambda: next(stream, None))
                if chunk is None:
                    break
                text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
                await websocket.send_json({"type": "log", "data": text})
        except Exception:
            pass

    log_task = asyncio.create_task(pump_logs())

    try:
        while True:
            msg = await websocket.receive_json()
            if msg.get("type") == "cmd":
                cmd = msg.get("data", "")
                out = dm.exec_command(sid, cmd)
                await websocket.send_json({"type": "log", "data": f"$ {cmd}\n{out}"})
            elif msg.get("type") == "stats":
                await websocket.send_json({"type": "stats", "data": dm.stats(sid)})
    except WebSocketDisconnect:
        pass
    finally:
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
