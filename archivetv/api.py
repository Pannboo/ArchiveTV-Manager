"""Versioned REST API (/api/v1).

Authenticated by the admin session cookie or, for scripts, by
``Authorization: Bearer <ATV_API_TOKEN>``. The OpenAPI schema and docs are
served behind the same check at /api/v1/openapi.json and /api/docs.
Feed and stream routes (/playlist.m3u, /epg.xml, /live, /catchup, XC) are
deliberately outside this API.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from . import catchup, jobs, monitor
from .catalogue import assign_to_channel, item_eligibility, iptv_status, is_schedulable, set_review_status
from .config import settings
from .db import session_scope, utcnow
from .discovery import run_rule, validate_items
from .enums import ChannelRole, EntryKind, PlaybackFormat, ReviewStatus, RuleKind, ScheduleMode
from .models import CatchupSession, Channel, ChannelItem, DiscoveryRule, MediaItem, MediaSource, ScheduleEntry
from .providers import get_provider, provider_labels
from .scheduler import RETENTION_CHOICES, extend, history_days, record_version, regenerate, to_local
from .stream import manager

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def require_api(request: Request) -> str:
    user = request.session.get("user")
    if user:
        return user
    auth = request.headers.get("authorization", "")
    if settings.api_token and auth.lower().startswith("bearer ") and secrets.compare_digest(auth[7:].strip(), settings.api_token):
        return "api"
    raise HTTPException(401, "authentication required", headers={"WWW-Authenticate": "Bearer"})


router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api)], tags=["api"])


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


# ------------------------------------------------------------------ models

class RuleIn(BaseModel):
    name: str
    provider: str = "archive"
    kind: RuleKind = RuleKind.SEARCH
    query: str | None = None
    params: dict[str, Any] = Field(default_factory=dict, description="Filters, e.g. collection, creator, subject, year_from, "
                                   "year_to, min_duration, max_duration, excluded_keywords, licensed_only")
    scan_interval_minutes: int = Field(1440, ge=15)
    max_results: int = Field(50, ge=1, le=500)
    enabled: bool = True
    channel_ids: list[int] = Field(default_factory=list)


class ScanIn(BaseModel):
    rule_id: int


class ReviewIn(BaseModel):
    note: str | None = None
    channel_ids: list[int] = Field(default_factory=list, description="Assign to these channels when approving")


class ChannelIn(BaseModel):
    slug: str
    name: str
    tvg_id: str | None = None
    description: str | None = None
    logo_url: str | None = None
    group_title: str = "ArchiveTV"
    number: int | None = None
    enabled: bool = True
    mode: ScheduleMode = ScheduleMode.SEQUENTIAL
    repeat_avoid_count: int = Field(10, ge=0)
    playback_format: PlaybackFormat = PlaybackFormat.AUTO
    fallback_item_id: int | None = None
    idents_between: bool = False
    filler_pad: bool = True
    catchup_enabled: bool = True
    catchup_days: Literal[1, 3, 7, 14, 30] = 7


class ChannelPatch(BaseModel):
    name: str | None = None
    tvg_id: str | None = None
    description: str | None = None
    logo_url: str | None = None
    group_title: str | None = None
    number: int | None = None
    enabled: bool | None = None
    mode: ScheduleMode | None = None
    seed: str | None = None
    repeat_avoid_count: int | None = Field(None, ge=0)
    playback_format: PlaybackFormat | None = None
    fallback_item_id: int | None = None
    idents_between: bool | None = None
    filler_pad: bool | None = None
    catchup_enabled: bool | None = None
    catchup_days: Literal[1, 3, 7, 14, 30] | None = None
    rule_ids: list[int] | None = None


class CatchupIn(BaseModel):
    channel_id: int
    start: str = Field(..., description="Unix seconds or ISO 8601 (UTC unless an offset is given)")
    duration_minutes: float | None = Field(None, gt=0, description="Defaults to the rest of the programme")


# ---------------------------------------------------------------- serialisers

def rule_out(r: DiscoveryRule) -> dict:
    return {
        "id": r.id, "name": r.name, "provider": r.provider, "kind": r.kind, "query": r.query, "params": r.params,
        "scan_interval_minutes": r.scan_interval_minutes, "max_results": r.max_results, "enabled": r.enabled,
        "last_run_at": _iso(r.last_run_at), "last_status": r.last_status, "last_error": r.last_error,
        "last_new_count": r.last_new_count, "channel_ids": [c.id for c in r.channels],
    }


def media_out(it: MediaItem, detail: bool = False, channel_items: list[ChannelItem] | None = None) -> dict:
    d = {
        "id": it.id, "provider": it.source.provider, "external_id": it.source.external_id, "url": it.source.original_url,
        "title": it.title, "creator": it.source.creator, "duration": it.duration, "publication_date": it.publication_date,
        "thumbnail_url": it.thumbnail_url, "review_status": it.review_status, "eligibility": item_eligibility(it),
        "schedulable": is_schedulable(it), "iptv_status": iptv_status(it), "duplicate_of_id": it.duplicate_of_id,
        "flags": it.flags, "updated_at": _iso(it.updated_at),
    }
    if detail:
        d.update({
            "description": it.description, "tags": it.tags, "categories": it.categories, "series": it.series,
            "season": it.season, "episode": it.episode, "restrictions": it.restrictions,
            "review_note": it.review_note, "reviewed_at": _iso(it.reviewed_at),
            "channels": [{"channel_id": ci.channel_id, "role": ci.role} for ci in channel_items],
            "playback_sources": [{
                "id": ps.id, "type": ps.playback_type, "url": ps.url, "local_path": ps.local_path, "label": ps.label,
                "eligibility": ps.eligibility, "rights_status": ps.rights_status, "rights_note": ps.rights_note,
                "license_url": ps.license_url, "health_ok": ps.health_ok,
                "seekable": ps.seekable, "live": ps.live, "video_codec": ps.video_codec, "audio_codec": ps.audio_codec,
                "width": ps.width, "height": ps.height, "duration": ps.duration, "size_bytes": ps.size_bytes,
                "last_validated_at": _iso(ps.last_validated_at), "last_error": ps.last_error,
            } for ps in it.playback_sources],
        })
    return d


def channel_out(ch: Channel, s) -> dict:
    now = utcnow()
    cur = s.scalar(select(ScheduleEntry).where(ScheduleEntry.channel_id == ch.id, ScheduleEntry.start <= now,
                                                ScheduleEntry.end > now).limit(1))
    return {
        "id": ch.id, "slug": ch.slug, "name": ch.name, "tvg_id": ch.tvg_id, "number": ch.number,
        "description": ch.description, "logo_url": ch.logo_url, "group_title": ch.group_title, "enabled": ch.enabled,
        "mode": ch.mode, "seed": ch.seed, "repeat_avoid_count": ch.repeat_avoid_count, "playback_format": ch.playback_format,
        "fallback_item_id": ch.fallback_item_id, "catchup_enabled": ch.catchup_enabled, "catchup_days": history_days(ch),
        "items": len(ch.items), "now_playing": cur.title if cur else None,
    }


def entry_out(e: ScheduleEntry) -> dict:
    return {
        "id": e.id, "start": _iso(e.start), "end": _iso(e.end), "start_local": to_local(e.start).isoformat(),
        "kind": e.kind, "title": e.title, "media_item_id": e.media_item_id, "playback_source_id": e.playback_source_id,
        "block": e.block_name, "overridden": e.overridden,
    }


# ------------------------------------------------------------------ routes

@router.get("/providers")
def providers():
    out = []
    for name, label in provider_labels().items():
        try:
            st = get_provider(name).status()
        except Exception as e:  # noqa: BLE001
            st = {"configured": False, "error": str(e)}
        out.append({"name": name, "label": label, **st})
    return out


@router.get("/discovery/rules")
def list_rules():
    with session_scope() as s:
        return [rule_out(r) for r in s.scalars(select(DiscoveryRule).order_by(DiscoveryRule.id)).all()]


@router.post("/discovery/rules", status_code=201)
def create_rule(body: RuleIn):
    if body.provider not in provider_labels():
        raise HTTPException(400, f"unknown provider {body.provider!r}")
    with session_scope() as s:
        r = DiscoveryRule(name=body.name, provider=body.provider, kind=body.kind, query=body.query, params=body.params,
                          scan_interval_minutes=body.scan_interval_minutes, max_results=body.max_results, enabled=body.enabled)
        if body.channel_ids:
            r.channels = list(s.scalars(select(Channel).where(Channel.id.in_(body.channel_ids))).all())
        s.add(r)
        s.flush()
        return rule_out(r)


@router.post("/discovery/scan")
async def scan(body: ScanIn):
    with session_scope() as s:
        if s.get(DiscoveryRule, body.rule_id) is None:
            raise HTTPException(404, "unknown rule")
    res = await run_rule(body.rule_id, interactive=True)
    return {"rule_id": body.rule_id, "found": res.found, "new": res.new, "skipped_known": res.skipped_known,
            "filtered": res.filtered, "errors": res.errors}


@router.get("/media")
def list_media(status: ReviewStatus | None = None, provider: str | None = None, q: str | None = None,
               limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    with session_scope() as s:
        stmt = select(MediaItem).join(MediaSource)
        if status:
            stmt = stmt.where(MediaItem.review_status == status)
        if provider:
            stmt = stmt.where(MediaSource.provider == provider)
        if q:
            stmt = stmt.where(or_(MediaItem.title.ilike(f"%{q}%"), MediaSource.external_id.ilike(f"%{q}%")))
        total = s.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = s.scalars(stmt.order_by(MediaItem.updated_at.desc()).limit(limit).offset(offset)).all()
        return {"total": total, "items": [media_out(it) for it in rows]}


def _item(s, item_id: int) -> MediaItem:
    it = s.get(MediaItem, item_id)
    if it is None:
        raise HTTPException(404, "unknown media item")
    return it


def _detail(s, it: MediaItem) -> dict:
    cis = list(s.scalars(select(ChannelItem).where(ChannelItem.media_item_id == it.id)).all())
    return media_out(it, detail=True, channel_items=cis)


@router.get("/media/{item_id}")
def get_media(item_id: int):
    with session_scope() as s:
        return _detail(s, _item(s, item_id))


def _review(item_id: int, status: ReviewStatus, body: ReviewIn | None, user: str) -> dict:
    with session_scope() as s:
        it = _item(s, item_id)
        set_review_status(s, it, status, actor=user, note=body.note if body else None)
        if status == ReviewStatus.APPROVED and body:
            for cid in body.channel_ids:
                if s.get(Channel, cid) is None:
                    raise HTTPException(400, f"unknown channel {cid}")
                assign_to_channel(s, it, cid, ChannelRole.PROGRAMME)
        s.flush()
        return _detail(s, it)


@router.post("/media/{item_id}/approve")
def approve(item_id: int, body: ReviewIn | None = None, user: str = Depends(require_api)):
    return _review(item_id, ReviewStatus.APPROVED, body, user)


@router.post("/media/{item_id}/reject")
def reject(item_id: int, body: ReviewIn | None = None, user: str = Depends(require_api)):
    return _review(item_id, ReviewStatus.REJECTED, body, user)


@router.post("/media/{item_id}/quarantine")
def quarantine(item_id: int, body: ReviewIn | None = None, user: str = Depends(require_api)):
    return _review(item_id, ReviewStatus.QUARANTINED, body, user)


@router.post("/media/{item_id}/revalidate")
async def revalidate(item_id: int):
    with session_scope() as s:
        _item(s, item_id)
    await validate_items([item_id], concurrency=1)
    with session_scope() as s:
        return _detail(s, _item(s, item_id))


@router.get("/channels")
def list_channels():
    with session_scope() as s:
        return [channel_out(c, s) for c in s.scalars(select(Channel).order_by(Channel.number, Channel.name)).all()]


@router.post("/channels", status_code=201)
def create_channel(body: ChannelIn):
    slug = body.slug.strip().lower()
    if not _SLUG.match(slug):
        raise HTTPException(400, "slug must be lowercase letters, digits and dashes")
    with session_scope() as s:
        tvg = (body.tvg_id or f"{slug}.archivetv").strip()
        if s.scalar(select(Channel.id).where(or_(Channel.slug == slug, Channel.tvg_id == tvg))):
            raise HTTPException(409, "slug or tvg_id already in use")
        ch = Channel(slug=slug, tvg_id=tvg, seed=secrets.token_hex(4),
                     **body.model_dump(exclude={"slug", "tvg_id"}))
        s.add(ch)
        s.flush()
        return channel_out(ch, s)


def _channel(s, channel_id: int) -> Channel:
    ch = s.get(Channel, channel_id)
    if ch is None:
        raise HTTPException(404, "unknown channel")
    return ch


@router.patch("/channels/{channel_id}")
def patch_channel(channel_id: int, body: ChannelPatch):
    with session_scope() as s:
        ch = _channel(s, channel_id)
        changes = body.model_dump(exclude_unset=True)
        if "tvg_id" in changes and s.scalar(select(Channel.id).where(Channel.tvg_id == changes["tvg_id"], Channel.id != ch.id)):
            raise HTTPException(409, "tvg_id already in use")
        rule_ids = changes.pop("rule_ids", None)
        if rule_ids is not None:
            ch.rules = list(s.scalars(select(DiscoveryRule).where(DiscoveryRule.id.in_(rule_ids))).all()) if rule_ids else []
        for k, v in changes.items():
            setattr(ch, k, v)
        s.flush()
        return channel_out(ch, s)


@router.get("/channels/{channel_id}/schedule")
def channel_schedule(channel_id: int, start: datetime | None = Query(None, alias="from"), end: datetime | None = Query(None, alias="to")):
    """Published timeline (UTC), including retained history. Defaults to the
    last 24 hours and the next 7 days."""
    now = utcnow()
    start = (start.replace(tzinfo=None) if start else now - timedelta(days=1))
    end = (end.replace(tzinfo=None) if end else now + timedelta(days=settings.schedule_days))
    with session_scope() as s:
        _channel(s, channel_id)
        rows = s.scalars(select(ScheduleEntry).where(ScheduleEntry.channel_id == channel_id, ScheduleEntry.end > start,
                                                      ScheduleEntry.start < end).order_by(ScheduleEntry.start)).all()
        return [entry_out(e) for e in rows]


@router.post("/channels/{channel_id}/schedule/generate")
def generate_schedule(channel_id: int, mode: Literal["extend", "regenerate"] = "extend", user: str = Depends(require_api)):
    """``extend`` fills to the 7-day horizon keeping what is published;
    ``regenerate`` rebuilds everything after the programme on air."""
    with session_scope() as s:
        ch = _channel(s, channel_id)
        if mode == "regenerate":
            added = regenerate(s, ch, actor=user)
        else:
            added = extend(s, ch)
            record_version(s, ch, "extend", None, added, actor=user)
        return {"channel_id": channel_id, "mode": mode, "added": added}


@router.get("/catchup/programmes")
def catchup_programmes(channel_id: int | None = None, limit: int = Query(200, ge=1, le=2000)):
    """Past programmes inside each channel's catch-up window, newest first,
    with whether each can be replayed right now."""
    now = utcnow()
    out = []
    with session_scope() as s:
        chans = [_channel(s, channel_id)] if channel_id else list(s.scalars(select(Channel).where(Channel.enabled.is_(True))).all())
        for ch in chans:
            since = now - timedelta(days=history_days(ch))
            rows = s.scalars(select(ScheduleEntry).where(
                ScheduleEntry.channel_id == ch.id, ScheduleEntry.end <= now, ScheduleEntry.end > since,
                ScheduleEntry.kind != EntryKind.SLATE).order_by(ScheduleEntry.start.desc()).limit(limit)).all()
            for e in rows:
                try:
                    catchup.resolve_request(s, ch, max(e.start, since), now=now)
                    available, reason = True, None
                except catchup.CatchupError as err:
                    available, reason = False, err.message
                out.append({**entry_out(e), "channel_id": ch.id, "channel": ch.slug, "available": available, "reason": reason})
    return out


@router.get("/catchup/sessions")
def catchup_sessions(limit: int = Query(50, ge=1, le=500)):
    active = {d["session_id"]: d for d in manager.catchup_status()}
    with session_scope() as s:
        rows = s.scalars(select(CatchupSession).order_by(CatchupSession.started_at.desc()).limit(limit)).all()
        hist = [{
            "session_id": r.id, "channel_id": r.channel_id, "title": r.title, "requested_start": _iso(r.requested_start),
            "duration": r.duration, "protocol": r.protocol, "client": r.client, "started_at": _iso(r.started_at),
            "ended_at": _iso(r.ended_at), "status": r.status, "position": _iso(r.position), "bytes_out": r.bytes_out,
            "error": r.error,
        } for r in rows]
    return {"active": list(active.values()), "history": hist}


@router.post("/catchup/sessions", status_code=201)
def create_catchup_session(body: CatchupIn, request: Request):
    """Validate a historical request and return the URL that plays it. The
    session (and its FFmpeg process) starts when that URL is opened."""
    with session_scope() as s:
        ch = _channel(s, body.channel_id)
        try:
            start = catchup.parse_timestamp(body.start, timezone.utc)
            req = catchup.resolve_request(s, ch, start, body.duration_minutes * 60 if body.duration_minutes else None)
        except catchup.CatchupError as e:
            raise HTTPException(e.status, e.message)
    base = settings.public_base_url or str(request.base_url).rstrip("/")
    q = f"start={int((req.start - datetime(1970, 1, 1)).total_seconds())}&duration={req.duration / 60:.0f}"
    if settings.feed_token:
        q += f"&token={settings.feed_token}"
    return {"channel_id": ch.id, "entry_id": req.entry_id, "title": req.title, "start": _iso(req.start),
            "offset_seconds": req.offset, "duration_seconds": req.duration, "url": f"{base}/catchup/{req.slug}.ts?{q}"}


@router.delete("/catchup/sessions/{session_id}")
async def stop_catchup_session(session_id: str):
    if not await catchup.stop_session(manager, session_id):
        raise HTTPException(404, "no such active session")
    return {"stopped": session_id}


@router.get("/streams/active")
def streams_active():
    return {"live": manager.status(), "catchup": manager.catchup_status()}


@router.get("/system/status")
def system_status():
    return {"version": "2.0", "time": _iso(utcnow()), "timezone": str(settings.timezone),
            "resources": monitor.snapshot(manager), "jobs": jobs.job_status(),
            "retention_choices": list(RETENTION_CHOICES)}
