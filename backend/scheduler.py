"""Простой cron-планировщик в фоновом потоке."""
import threading
import time
from datetime import datetime
from sqlalchemy.orm import Session

from database import SessionLocal, Schedule, Server
import docker_manager as dm


def _match_field(field: str, value: int) -> bool:
    """Поддержка: '*', 'a,b,c', '*/n', 'n'."""
    field = field.strip()
    if field == "*":
        return True
    if field.startswith("*/"):
        try:
            return value % int(field[2:]) == 0
        except ValueError:
            return False
    parts = field.split(",")
    for p in parts:
        try:
            if int(p) == value:
                return True
        except ValueError:
            continue
    return False


def cron_matches(expr: str, now: datetime) -> bool:
    parts = expr.split()
    if len(parts) != 5:
        return False
    m, h, dom, mon, dow = parts
    return (_match_field(m, now.minute)
            and _match_field(h, now.hour)
            and _match_field(dom, now.day)
            and _match_field(mon, now.month)
            and _match_field(dow, now.weekday()))


def execute(s: Schedule, db: Session):
    srv = db.query(Server).get(s.server_id)
    if not srv:
        return
    try:
        dm.append_event(srv.id, f"Schedule: '{s.name}' running action={s.action}")
        if s.action == "command":
            out = dm.exec_command(srv.id, s.payload or "echo")
            if out:
                dm.append_event(srv.id, f"Schedule command output:\n{out}")
        elif s.action == "restart":
            if dm.inspect(srv.id):
                dm.restart(srv.id)
        elif s.action == "stop":
            dm.stop(srv.id)
        elif s.action == "start":
            if dm.inspect(srv.id):
                dm.start(srv.id)
        s.last_run = datetime.utcnow()
        db.commit()
        dm.append_event(srv.id, f"Schedule: '{s.name}' finished")
    except Exception as e:
        dm.append_event(srv.id, f"Schedule: '{s.name}' failed: {e}")
        print(f"[scheduler] error in schedule {s.id}: {e}")


def loop():
    while True:
        now = datetime.utcnow().replace(second=0, microsecond=0)
        db = SessionLocal()
        try:
            for s in db.query(Schedule).filter(Schedule.enabled == True).all():
                if cron_matches(s.cron, now):
                    if s.last_run and s.last_run.replace(second=0, microsecond=0) == now:
                        continue
                    execute(s, db)
        except Exception as e:
            print(f"[scheduler] loop error: {e}")
        finally:
            db.close()
        # ждём до начала следующей минуты
        sleep_for = 60 - datetime.utcnow().second
        time.sleep(max(1, sleep_for))


def start_background():
    t = threading.Thread(target=loop, daemon=True, name="panel-scheduler")
    t.start()
    return t
