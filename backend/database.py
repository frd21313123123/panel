from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Boolean, Text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
import os

DB_PATH = os.environ.get("PANEL_DB", "sqlite:///./panel.db")
engine = create_engine(DB_PATH, connect_args={"check_same_thread": False} if DB_PATH.startswith("sqlite") else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    servers = relationship("Server", back_populates="owner", cascade="all, delete-orphan")


class Egg(Base):
    """Шаблон среды выполнения (аналог Pterodactyl Egg)."""
    __tablename__ = "eggs"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    language = Column(String(32), nullable=False)
    docker_image = Column(String(255), nullable=False)
    default_cmd = Column(String(512), nullable=False)
    description = Column(Text, default="")


class Server(Base):
    __tablename__ = "servers"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    egg_id = Column(Integer, ForeignKey("eggs.id"), nullable=False)
    container_id = Column(String(128), default="")
    status = Column(String(32), default="offline")
    memory_mb = Column(Integer, default=512)
    cpu_limit = Column(Integer, default=100)
    disk_mb = Column(Integer, default=1024)
    startup_cmd = Column(String(512), default="")
    data_dir = Column(String(512), default="")
    ports = Column(Text, default="")  # JSON: [{"host":8000,"container":8000,"proto":"tcp"}]
    env_vars = Column(Text, default="")  # JSON dict
    git_repo = Column(String(512), default="")
    git_branch = Column(String(128), default="")
    git_subdir = Column(String(255), default="")
    git_auto_update = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="servers")
    egg = relationship("Egg")
    subusers = relationship("Subuser", back_populates="server", cascade="all, delete-orphan")
    schedules = relationship("Schedule", back_populates="server", cascade="all, delete-orphan")
    backups = relationship("Backup", back_populates="server", cascade="all, delete-orphan")


class Subuser(Base):
    __tablename__ = "subusers"
    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    permissions = Column(String(255), default="console,files")  # comma-sep
    server = relationship("Server", back_populates="subusers")
    user = relationship("User")


class Schedule(Base):
    __tablename__ = "schedules"
    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id"), nullable=False)
    name = Column(String(64), nullable=False)
    cron = Column(String(64), nullable=False)  # minute hour dom month dow
    action = Column(String(32), default="command")  # command|restart|stop|start
    payload = Column(String(512), default="")
    enabled = Column(Boolean, default=True)
    last_run = Column(DateTime)
    server = relationship("Server", back_populates="schedules")


class Backup(Base):
    __tablename__ = "backups"
    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id"), nullable=False)
    name = Column(String(128), nullable=False)
    filename = Column(String(255), nullable=False)
    size = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    server = relationship("Server", back_populates="backups")


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate_server_table()
    db = SessionLocal()
    try:
        if db.query(Egg).count() == 0:
            seed = [
                Egg(name="Python 3.12", language="python", docker_image="python:3.12-slim",
                    default_cmd="python main.py", description="Python 3.12 runtime"),
                Egg(name="Node.js 20", language="javascript", docker_image="node:20-alpine",
                    default_cmd="node index.js", description="Node.js 20 LTS"),
                Egg(name="Bun", language="javascript", docker_image="oven/bun:latest",
                    default_cmd="bun run index.js", description="Bun JS runtime"),
                Egg(name="Go 1.22", language="go", docker_image="golang:1.22-alpine",
                    default_cmd="go run main.go", description="Go 1.22"),
                Egg(name="Ubuntu shell", language="bash", docker_image="ubuntu:24.04",
                    default_cmd="bash start.sh", description="Чистый Ubuntu 24.04"),
            ]
            db.add_all(seed)
            db.commit()
    finally:
        db.close()


def _migrate_server_table():
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(servers)").fetchall()}
        if "git_repo" not in columns:
            conn.exec_driver_sql("ALTER TABLE servers ADD COLUMN git_repo VARCHAR(512) DEFAULT ''")
        if "git_branch" not in columns:
            conn.exec_driver_sql("ALTER TABLE servers ADD COLUMN git_branch VARCHAR(128) DEFAULT ''")
        if "git_subdir" not in columns:
            conn.exec_driver_sql("ALTER TABLE servers ADD COLUMN git_subdir VARCHAR(255) DEFAULT ''")
        if "git_auto_update" not in columns:
            conn.exec_driver_sql("ALTER TABLE servers ADD COLUMN git_auto_update BOOLEAN DEFAULT 0")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
