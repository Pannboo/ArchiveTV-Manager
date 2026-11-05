"""ArchiveTV Manager application entry point.

Public (Dispatcharr-facing) endpoints:
    GET /playlist.m3u      GET /epg.xml      GET /live/{channel_id}.ts      GET /health
    GET /catchup/{channel_id}.ts?start=...   GET /startover/{channel_id}.ts
    Xtream Codes-compatible upstream interface: see xtream.py
Everything under /admin requires an administrator session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from starlette.middleware.sessions import SessionMiddleware

from . import appsettings, jobs
from .config import settings
from .db import init_engine, session_scope, utcnow
from .feeds import build_m3u, build_xmltv
from .models import Channel, ScheduleEntry
from .providers import close_all
from .scheduler import maintain
from . import catchup, logos
from .stream import StreamLimitError, manager


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d)


def setup_logging() -> None:
    h = logging.StreamHandler(sys.stdout)
    if os.environ.get("ATV_LOG_FORMAT", "json") == "json":
        h.setFormatter(JsonFormatter())
    else:
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(os.environ.get("ATV_LOG_LEVEL", "INFO"))
    logging.getLogger("httpx").setLevel(logging.WARNING)


log = logging.getLogger("archivetv")


def _persisted_secret(name: str, value: str | None, nbytes: int = 32) -> str:
    """Use the configured value, else a generated one persisted in the data dir."""
    if value:
        return value
    p = settings.data_dir / "run" / name
    try:
        return p.read_text().strip()
    except OSError:
        v = secrets.token_urlsafe(nbytes)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(v)
        os.chmod(p, 0o600)
        return v


settings.ensure_dirs()
if not os.environ.get("ATV_SECRET_KEY") and not os.environ.get("ATV_SECRET_KEY_FILE"):
    settings.secret_key = _persisted_secret("secret_key", None)
_generated_password = settings.admin_password is None
settings.admin_password = _persisted_secret("admin_password", settings.admin_password, 12)
settings.xc_password = _persisted_secret("xc_password", settings.xc_password, 12)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    init_engine()
    appsettings.apply_overrides()
    killed = manager.cleanup_orphans()
    if killed:
        log.warning("killed %s orphaned ffmpeg processes", killed)
    if _generated_password:
        log.warning("No ATV_ADMIN_PASSWORD set; generated admin password stored in %s", settings.data_dir / "run" / "admin_password")
    for tool in (settings.ffmpeg_path, settings.ffprobe_path):
        if not shutil.which(tool):
            log.error("%s not found on PATH — streaming and validation will fail", tool)
    if settings.jobs_enabled:
        jobs.start_jobs()
    try:
        yield
    finally:
        jobs.stop_jobs()
        await manager.stop_all()
        await close_all()


app = FastAPI(title="ArchiveTV Manager", version="2.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="strict", https_only=False, max_age=7 * 86400)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")


def _check_token(request: Request) -> None:
    if settings.feed_token and not secrets.compare_digest(request.query_params.get("token", ""), settings.feed_token):
        raise HTTPException(status_code=403, detail="invalid feed token")


def base_url(request: Request) -> str:
    return settings.public_base_url or str(request.base_url).rstrip("/")


@app.get("/playlist.m3u")
def playlist(request: Request):
    _check_token(request)
    with session_scope() as s:
        body = build_m3u(s, base_url(request))
    return Response(body, media_type="audio/x-mpegurl", headers={"Cache-Control": "no-cache"})


@app.get("/epg.xml")
def epg(request: Request):
    _check_token(request)
    with session_scope() as s:
        body = build_xmltv(s, base_url=base_url(request))
    return Response(body, media_type="application/xml", headers={"Cache-Control": "no-cache"})


@app.get("/live/{channel_id}.ts")
async def live(channel_id: str, request: Request):
    _check_token(request)
    return await live_by_slug(channel_id)


async def live_by_slug(slug: str) -> StreamingResponse:
    try:
        return await _live_by_slug(slug)
    except OperationalError:
        raise HTTPException(503, "database unavailable")


async def _live_by_slug(slug: str) -> StreamingResponse:
    with session_scope() as s:
        ch = s.scalar(select(Channel).where(Channel.slug == slug))
        if ch is None or not ch.enabled:
            raise HTTPException(404, "unknown channel")
        cid = ch.id
        if s.scalar(select(func.count()).select_from(ScheduleEntry).where(ScheduleEntry.channel_id == cid, ScheduleEntry.end > utcnow())) == 0:
            maintain(s, ch)
    try:
        stream, q = await manager.subscribe(cid)
    except StreamLimitError as e:
        raise HTTPException(503, str(e))
    return ts_response(stream, q)


# MPEG-TS null packets (PID 0x1FFF): legal padding every player discards.
NULL_PACKET = b"\x47\x1f\xff\x10" + b"\xff" * 184
KEEPALIVE = NULL_PACKET * 7  # > 1 KiB: enough for Dispatcharr's TS-sync peek


def ts_response(stream, q) -> StreamingResponse:
    async def body():
        try:
            # Until the source has opened (remote seeks can take seconds), send
            # null packets so upstream proxies see a live TS stream at once
            # and their read timeouts (Dispatcharr's is ~5 s) never fire.
            yield KEEPALIVE
            while True:
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=1.0)
                    break
                except asyncio.TimeoutError:
                    yield NULL_PACKET * 2
            while chunk is not None:
                yield chunk
                chunk = await q.get()
        finally:
            stream.remove_viewer(q)

    return StreamingResponse(body(), media_type="video/mp2t", headers={"Cache-Control": "no-cache", "Connection": "close"})


async def open_catchup(slug: str, start_raw: str | None, duration: float | None, request: Request, protocol: str,
                       start_over: bool = False, end_raw: str | None = None, tz=None):
    """Validate a historical request and start an independent catch-up session."""
    try:
        with session_scope() as s:
            ch = s.scalar(select(Channel).where(Channel.slug == slug))
            if ch is None:
                raise catchup.CatchupError(404, "unknown channel")
            start = catchup.start_over_time(s, ch) if start_over else catchup.parse_timestamp(start_raw or "", tz)
            if end_raw and duration is None:
                duration = (catchup.parse_timestamp(end_raw, tz) - start).total_seconds()
            # Dispatcharr (XC) sends whole-minute times: snap to the programme start.
            req = catchup.resolve_request(s, ch, start, duration, snap_seconds=60 if protocol == "xc" else 0)
        stream, q = await catchup.start_session(manager, req, protocol, request.headers.get("user-agent"))
    except catchup.CatchupError as e:
        raise HTTPException(e.status, e.message)
    except StreamLimitError as e:
        raise HTTPException(503, str(e))
    return ts_response(stream, q)


@app.get("/catchup/{channel_id}.ts")
async def catchup_stream(channel_id: str, request: Request, start: str | None = None, utc: str | None = None,
                         duration: float | None = None, end: str | None = None, utcend: str | None = None):
    """Historical playback. ``start``/``utc``: Unix seconds or ISO time;
    ``duration`` in minutes, or ``end``/``utcend``. Playback continues into the
    following programmes until the duration is reached."""
    _check_token(request)
    secs = duration * 60 if duration else None
    return await open_catchup(channel_id, start or utc, secs, request, "direct", end_raw=end or utcend)


@app.get("/startover/{channel_id}.ts")
async def startover_stream(channel_id: str, request: Request):
    """Restart the programme currently on air from its beginning."""
    _check_token(request)
    return await open_catchup(channel_id, None, None, request, "direct", start_over=True)


@app.get("/logos/{name}")
def logo(name: str):
    p = logos.path_for(name)
    if p is None:
        raise HTTPException(404, "no such logo")
    return FileResponse(p, media_type=logos.MEDIA_TYPES[p.suffix[1:]], headers={"Cache-Control": "public, max-age=86400"})


@app.get("/health")
async def health():
    checks: dict = {}
    ok = True
    try:
        with session_scope() as s:
            s.execute(text("SELECT 1"))
            channels = s.scalars(select(Channel).where(Channel.enabled.is_(True))).all()
            horizon = {
                c.slug: (s.scalar(select(func.max(ScheduleEntry.end)).where(ScheduleEntry.channel_id == c.id)) or None) for c in channels
            }
        checks["database"] = "ok"
        checks["schedule_horizon"] = {k: (v.isoformat() + "Z" if v else None) for k, v in horizon.items()}
    except Exception as e:  # noqa: BLE001
        ok = False
        checks["database"] = f"error: {e}"
    checks["ffmpeg"] = "ok" if shutil.which(settings.ffmpeg_path) else "missing"
    checks["ffprobe"] = "ok" if shutil.which(settings.ffprobe_path) else "missing"
    ok = ok and checks["ffmpeg"] == "ok"
    checks["active_streams"] = manager.status()
    checks["catchup_sessions"] = manager.catchup_status()
    checks["jobs"] = jobs.job_status()
    return Response(json.dumps({"status": "ok" if ok else "degraded", **checks}, default=str), status_code=200 if ok else 503,
                    media_type="application/json")


APP_DIR = Path(__file__).parent / "web" / "app"  # built React frontend (frontend/ → npm run build)


@app.get("/")
def root():
    return RedirectResponse("/app/" if (APP_DIR / "index.html").is_file() else "/admin")


@app.get("/app", include_in_schema=False)
def app_redirect():
    return RedirectResponse("/app/")


@app.get("/app/{path:path}", include_in_schema=False)
def spa(path: str):
    """Serve the admin SPA: real files under /app/, index.html for client-side routes."""
    index = APP_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(404, "frontend not built (cd frontend && npm ci && npm run build)")
    if path:
        f = (APP_DIR / path).resolve()
        if f.is_file() and APP_DIR.resolve() in f.parents:
            immutable = path.startswith("assets/")
            return FileResponse(f, headers={"Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache"})
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


from fastapi import Depends  # noqa: E402
from fastapi.openapi.docs import get_swagger_ui_html  # noqa: E402

from . import api as _api  # noqa: E402
from . import api_admin as _api_admin  # noqa: E402
from . import xtream as _xtream  # noqa: E402
from .web import admin as _admin  # noqa: E402

app.include_router(_api.router)
app.include_router(_api_admin.auth_router)
app.include_router(_api_admin.router)
app.include_router(_xtream.router, tags=["xtream codes"])
app.include_router(_admin.router, include_in_schema=False)


@app.get("/api/v1/openapi.json", include_in_schema=False)
def openapi_schema(user: str = Depends(_api.require_api)):
    return app.openapi()


@app.get("/api/docs", include_in_schema=False)
def api_docs(user: str = Depends(_api.require_api)):
    return get_swagger_ui_html(openapi_url="/api/v1/openapi.json", title="ArchiveTV Manager API")


@app.exception_handler(_admin.LoginRequired)
async def _login_required(request: Request, exc: _admin.LoginRequired):
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


def run() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run("archivetv.main:app", host=os.environ.get("ATV_HOST", "0.0.0.0"), port=int(os.environ.get("ATV_PORT", "8090")),
                proxy_headers=True, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()

