from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Naive UTC datetime; every timestamp in the database is UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


_engine = None
SessionLocal: sessionmaker[Session] = sessionmaker(expire_on_commit=False)


def init_engine(url: str | None = None):
    global _engine
    if url is None:
        settings.ensure_dirs()
        url = f"sqlite:///{settings.db_path}"
    _engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(_engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    SessionLocal.configure(bind=_engine)
    from . import models  # noqa: F401  (register tables)

    migrate(_engine)
    return _engine


def migrate(eng) -> None:
    """Bring the schema to the latest Alembic revision (runs at every start)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", "archivetv:migrations")
    with eng.begin() as conn:
        tables = set(inspect(conn).get_table_names())
        if tables and "alembic_version" not in tables:
            raise RuntimeError(
                f"{settings.db_path} was created by a pre-2.0 development build without migrations; "
                "move it aside to start with a fresh database"
            )
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


def engine():
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def backup_database(dest_dir: Path | None = None, keep: int | None = None) -> Path:
    """Online SQLite backup using the sqlite3 backup API (safe while running)."""
    dest_dir = dest_dir or settings.data_dir / "backups"
    keep = settings.backup_keep if keep is None else keep
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"archivetv-{utcnow():%Y%m%d-%H%M%S}.sqlite3"
    src = sqlite3.connect(settings.db_path)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    backups = sorted(dest_dir.glob("archivetv-*.sqlite3"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
    return dest
