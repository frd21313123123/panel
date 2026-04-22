# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Running the project

**Windows (dev):**
```cmd
run.bat        # creates .venv, installs deps, starts on http://localhost:8080
```

**Linux/Ubuntu:**
```bash
./run.sh                   # dev mode
sudo ./install-ubuntu.sh   # production: systemd + nginx
```

**Manual start:**
```bash
cd backend
python main.py             # listens on 0.0.0.0:8080
```

Default credentials on first boot: `admin` / `admin` (created automatically if no users exist).

Docker must be running for power actions and console to work. Without Docker, the UI loads but container operations return 503.

## Architecture

### Request flow
Browser → FastAPI (`backend/main.py`) → SQLite via SQLAlchemy → Docker SDK

There is no build step. Frontend is pure HTML/JS/CSS served by Jinja2 (`frontend/templates/`) and static files (`frontend/static/`). No bundler, no npm.

### Backend modules

| File | Responsibility |
|---|---|
| `main.py` | All FastAPI routes, WebSocket handlers, startup hook |
| `database.py` | SQLAlchemy models (`User`, `Egg`, `Server`, `Subuser`, `Schedule`, `Backup`) + `init_db()` seed |
| `auth.py` | JWT creation/decode, bcrypt, `get_current_user` / `require_admin` FastAPI deps |
| `docker_manager.py` | Thin wrapper over Docker SDK: create/start/stop/kill/remove containers, stream logs, exec interactive TTY |
| `files.py` | Sandboxed file manager — all paths validated against server's `data/servers/srv_{id}/` root |
| `backups.py` | tar.gz create/restore/delete inside `data/backups/srv_{id}/` |
| `scheduler.py` | Background thread, checks cron expressions every minute, runs actions via `docker_manager` |

### Data layout on disk
```
data/
  servers/srv_{id}/     ← mounted as /home/container inside each Docker container
  backups/srv_{id}/     ← tar.gz backup archives
panel.db                ← SQLite database
```

### WebSocket endpoints
- `/ws/servers/{sid}/console` — stats polling + docker exec (non-interactive, JSON protocol)
- `/ws/servers/{sid}/term` — interactive PTY via `docker exec` + xterm.js (binary/JSON mixed protocol)

Auth on WS: token from `?token=` query param or `panel_token` cookie.

### Key conventions
- `Server.ports` and `Server.env_vars` are stored as JSON strings in SQLite, parsed by `_parse_json()` in `main.py`.
- `_server_for(db, sid, user, perm=None)` enforces both ownership and subuser permission checks. Always use this — never query `Server` directly in routes.
- Subuser permissions are comma-separated strings: `console`, `files`, `backups`, `schedules`.
- `Egg` = runtime template (Docker image + default command). Seeded on first boot in `database.py:init_db()`.

### Frontend
Each page is a self-contained HTML file with inline `<script>`. Shared utilities are in `frontend/static/js/app.js` (`api`, `toast`, `fmtBytes`, `loadSidebar`, `logout`). xterm.js is loaded from CDN in `server.html`.
