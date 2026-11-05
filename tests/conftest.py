import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="atv-test-"))
os.environ.setdefault("ATV_DATA_DIR", str(_tmp / "data"))
os.environ.setdefault("ATV_MEDIA_DIR", str(_tmp / "media"))
os.environ.setdefault("ATV_JOBS_ENABLED", "0")
os.environ.setdefault("ATV_ADMIN_PASSWORD", "test-password")
os.environ.setdefault("ATV_LOG_FORMAT", "text")

import pytest  # noqa: E402

from archivetv import db  # noqa: E402
from archivetv.config import settings  # noqa: E402


@pytest.fixture()
def session():
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import StaticPool

    from archivetv import models  # noqa: F401  (register tables)

    settings.ensure_dirs()
    # In-memory SQLite needs a single shared connection.

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(eng, "connect")
    def _fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    db.Base.metadata.create_all(eng)
    db.SessionLocal.configure(bind=eng)
    s = db.SessionLocal()
    yield s
    s.close()
    eng.dispose()


@pytest.fixture()
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
