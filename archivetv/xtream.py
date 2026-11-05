"""Xtream Codes-compatible upstream interface for Dispatcharr.

Dispatcharr only proxies catch-up for Xtream Codes (XC) accounts, so
ArchiveTV exposes the subset of the XC portal API that Dispatcharr 0.33
uses when it ingests an account and serves catch-up:

    GET /player_api.php                       authentication / account info
    GET /player_api.php?action=get_live_categories
    GET /player_api.php?action=get_live_streams   (tv_archive / tv_archive_duration)
    GET /player_api.php?action=get_vod_*|get_series*   -> [] (no VOD)
    GET /xmltv.php                            programme guide incl. history
    GET /live/{user}/{pass}/{stream_id}.ts    live channel (shared stream)
    GET /timeshift/{user}/{pass}/{minutes}/{start}/{stream_id}.ts
    GET /streaming/timeshift.php?username=&password=&stream=&start=&duration=

``server_info.timezone`` is "UTC", so Dispatcharr passes catch-up start
times through unconverted and they are parsed as UTC. Stream ids are the
internal channel ids, which never change.

Errors use the statuses Dispatcharr's candidate cascade understands: 404
(try the next URL layout / not available), 403 (catch-up disabled) and 503
(capacity).
"""

from __future__ import annotations

import secrets
import time
import zlib
from datetime import timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from . import logos
from .config import settings
from .db import session_scope
from .feeds import build_xmltv, published_channels
from .models import Channel
from .scheduler import history_days
from .stream import manager

router = APIRouter()
UTC = timezone.utc


def _authorised(username: str | None, password: str | None) -> bool:
    if not settings.xc_password or username is None or password is None:
        return False
    return secrets.compare_digest(username, settings.xc_username) & secrets.compare_digest(password, settings.xc_password)


def _require(username: str | None, password: str | None) -> None:
    if not _authorised(username, password):
        raise HTTPException(401, "invalid credentials")


def category_id(group: str) -> str:
    """Stable category id derived from the group title, so renumbering never
    drops streams from Dispatcharr's enabled-category filter."""
    return str(100000 + zlib.crc32(group.encode()) % 900000)


def _server_info(request: Request) -> dict:
    url = request.url
    now = int(time.time())
    return {
        "url": url.hostname or "archivetv", "port": str(url.port or 80), "https_port": "443",
        "server_protocol": url.scheme, "rtmp_port": "0", "timezone": "UTC",
        "timestamp_now": now, "time_now": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now)),
    }


def _user_info() -> dict:
    return {
        "username": settings.xc_username, "password": settings.xc_password, "message": "ArchiveTV Manager",
        "auth": 1, "status": "Active", "exp_date": None, "is_trial": "0", "active_cons": str(len(manager.catchup_status())),
        "created_at": "0", "max_connections": str(settings.max_streams + settings.max_catchup_sessions),
        "allowed_output_formats": ["ts"],
    }


def live_streams(base: str | None = None) -> list[dict]:
    with session_scope() as s:
        out = []
        for i, ch in enumerate(published_channels(s), start=1):
            days = history_days(ch) if ch.catchup_enabled else 0
            out.append({
                "num": ch.number if ch.number is not None else i,
                "name": ch.name,
                "stream_type": "live",
                "stream_id": ch.id,
                "stream_icon": logos.url(ch, base) or "",
                "epg_channel_id": ch.tvg_id,
                "added": str(int(ch.created_at.replace(tzinfo=UTC).timestamp())) if ch.created_at else "0",
                "is_adult": 0,
                "category_id": category_id(ch.group_title),
                "category_ids": [int(category_id(ch.group_title))],
                "custom_sid": "",
                "tv_archive": 1 if days else 0,
                "tv_archive_duration": days,
                "direct_source": "",
            })
        return out


def live_categories() -> list[dict]:
    with session_scope() as s:
        groups = sorted({ch.group_title for ch in published_channels(s)})
    return [{"category_id": category_id(g), "category_name": g, "parent_id": 0} for g in groups]


@router.get("/player_api.php")
def player_api(request: Request, username: str | None = None, password: str | None = None, action: str | None = None):
    if not _authorised(username, password):
        # XC portals answer bad credentials with a JSON body, not an HTTP error.
        return JSONResponse({"user_info": {"auth": 0}, "error": "invalid credentials"}, status_code=401)
    if action in (None, ""):
        return {"user_info": _user_info(), "server_info": _server_info(request)}
    if action == "get_live_categories":
        return live_categories()
    if action == "get_live_streams":
        streams = live_streams(settings.public_base_url or str(request.base_url).rstrip("/"))
        cat = request.query_params.get("category_id")
        return [x for x in streams if x["category_id"] == cat] if cat else streams
    if action in ("get_vod_categories", "get_vod_streams", "get_series_categories", "get_series"):
        return []
    raise HTTPException(400, f"unsupported action {action!r}")


@router.get("/xmltv.php")
def xmltv(request: Request, username: str | None = None, password: str | None = None):
    _require(username, password)
    with session_scope() as s:
        body = build_xmltv(s, base_url=settings.public_base_url or str(request.base_url).rstrip("/"))
    return Response(body, media_type="application/xml", headers={"Cache-Control": "no-cache"})


def _channel_slug(stream_id: str) -> str:
    sid = stream_id.rsplit(".", 1)[0] if stream_id.endswith((".ts", ".m3u8")) else stream_id
    if not sid.isdigit():
        raise HTTPException(404, "unknown stream")
    with session_scope() as s:
        ch = s.scalar(select(Channel).where(Channel.id == int(sid)))
        if ch is None or not ch.enabled:
            raise HTTPException(404, "unknown stream")
        return ch.slug


@router.get("/live/{username}/{password}/{stream_id}")
async def xc_live(username: str, password: str, stream_id: str, request: Request):
    _require(username, password)
    from .main import live_by_slug

    return await live_by_slug(_channel_slug(stream_id))


async def _xc_catchup(username: str, password: str, minutes: str, start: str, stream_id: str, request: Request):
    _require(username, password)
    slug = _channel_slug(stream_id)
    try:
        secs = float(minutes) * 60 if minutes else None
    except ValueError:
        secs = None
    from .main import open_catchup

    try:
        return await open_catchup(slug, start, secs, request, "xc", tz=UTC)
    except HTTPException as e:
        # Map onto the statuses Dispatcharr's catch-up cascade acts on.
        if e.status_code in (403, 503):
            raise
        raise HTTPException(404, e.detail)


@router.get("/timeshift/{username}/{password}/{minutes}/{start}/{stream_id}")
async def xc_timeshift(username: str, password: str, minutes: str, start: str, stream_id: str, request: Request):
    return await _xc_catchup(username, password, minutes, start, stream_id, request)


@router.get("/streaming/timeshift.php")
async def xc_timeshift_query(request: Request, username: str | None = None, password: str | None = None,
                             stream: str | None = None, start: str | None = None, utc: str | None = None,
                             duration: str | None = None):
    if stream is None:
        raise HTTPException(404, "unknown stream")
    return await _xc_catchup(username or "", password or "", duration or "", start or utc or "", stream, request)
