"""Remaining /api/v1 endpoints: everything the administration UI can do.

The React frontend (frontend/) is built entirely on these and api.py. They
reuse the same catalogue, scheduler and validation functions as the classic
server-rendered pages, so both UIs behave identically.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from . import appsettings, catchup, jobs, logos, monitor, ytstream
from .api import _channel, _detail, _iso, _item, channel_out, entry_out, media_out, require_api, rule_out
from .catalogue import (
    IPTV_TYPES,
    air_problem,
    assign_to_channel,
    audit,
    best_iptv_source,
    file_sha1,
    flag_duplicate,
    is_schedulable,
    put_on_air,
    set_review_status,
    set_rights,
    title_key,
)
from .config import settings
from .db import backup_database, session_scope, utcnow
from .discovery import DiscoveryResult, import_youtube_live, refresh_item, run_query
from .enums import (
    ChannelRole,
    Eligibility,
    PlaybackFormat,
    PlaybackType,
    ProviderType,
    ReviewStatus,
    RightsStatus,
    RuleKind,
    ScheduleMode,
)
from .models import (
    AuditLog,
    Channel,
    ChannelBlock,
    ChannelItem,
    DiscoveryRule,
    MediaItem,
    MediaSource,
    PlaybackEvent,
    PlaybackSource,
    ProviderStatus,
    ScheduleEntry,
    ScheduleVersion,
)
from .providers import get_provider, provider_labels
from .providers.youtube import parse_youtube_url
from .scheduler import RETENTION_CHOICES, check_continuity, current_entry, history_days, maintain, override, regenerate
from .security import UnsafeURLError, check_url_syntax, safe_local_path
from .stream import manager
from .validator import validate_source
from .web.admin import _IA_URL, _attempts, _item_query, _media_files, _revalidate

log = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api)], tags=["admin"])


# ------------------------------------------------------------------ auth

class LoginIn(BaseModel):
    username: str
    password: str


@auth_router.post("/login")
def login(body: LoginIn, request: Request):
    """Start an admin session (cookie). Shares the login rate limit with the classic UI."""
    ip = request.client.host if request.client else "?"
    now = time.monotonic()
    _attempts[ip] = [t for t in _attempts[ip] if now - t < 60]
    if len(_attempts[ip]) >= 5:
        raise HTTPException(429, "too many attempts; wait a minute")
    ok = secrets.compare_digest(body.username.encode(), settings.admin_username.encode()) & secrets.compare_digest(
        body.password.encode(), (settings.admin_password or "").encode())
    if not ok:
        _attempts[ip].append(now)
        raise HTTPException(401, "invalid username or password")
    request.session.clear()
    request.session["user"] = body.username
    return {"user": body.username}


@auth_router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@auth_router.get("/me")
def me(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(401, "not signed in")
    return {"user": user}


# ------------------------------------------------------------------ meta / dashboard

@router.get("/meta")
def meta():
    """Enumerations and constants the UI needs."""
    return {
        "review_statuses": [x.value for x in ReviewStatus], "eligibility": [x.value for x in Eligibility],
        "rights": [x.value for x in RightsStatus], "playback_types": [x.value for x in PlaybackType],
        "roles": [x.value for x in ChannelRole], "modes": [x.value for x in ScheduleMode],
        "formats": [x.value for x in PlaybackFormat], "rule_kinds": [x.value for x in RuleKind],
        "providers": provider_labels(), "retention_choices": list(RETENTION_CHOICES),
        "timezone": str(settings.timezone), "schedule_days": settings.schedule_days,
        "feed_token": bool(settings.feed_token), "minimal_disk": settings.minimal_disk,
        "youtube_streaming": bool(settings.youtube_streaming),
        # Appended to feed/stream links the admin opens (same as the classic Settings page shows).
        "feed_query": f"token={quote(settings.feed_token)}" if settings.feed_token else "",
    }


@router.get("/dashboard")
def dashboard():
    now = utcnow()
    with session_scope() as s:
        counts = dict(s.execute(select(MediaItem.review_status, func.count()).group_by(MediaItem.review_status)).all())
        by_provider = dict(s.execute(select(MediaSource.provider, func.count()).group_by(MediaSource.provider)).all())
        chans = []
        for ch in s.scalars(select(Channel).order_by(Channel.number, Channel.name)).all():
            cur = current_entry(s, ch.id, now)
            nxt = s.scalar(select(ScheduleEntry).where(ScheduleEntry.channel_id == ch.id, ScheduleEntry.start >= (cur.end if cur else now))
                           .order_by(ScheduleEntry.start).limit(1))
            horizon = s.scalar(select(func.max(ScheduleEntry.end)).where(ScheduleEntry.channel_id == ch.id))
            eligible = sum(1 for ci in ch.items if ci.role == ChannelRole.PROGRAMME and ci.media_item and is_schedulable(ci.media_item))
            chans.append({"id": ch.id, "slug": ch.slug, "name": ch.name, "number": ch.number, "enabled": ch.enabled,
                          "now": entry_out(cur) if cur else None, "next": entry_out(nxt) if nxt else None,
                          "eligible": eligible, "horizon": _iso(horizon)})
        events = [{"at": _iso(e.at), "level": e.level, "message": e.message, "channel_id": e.channel_id}
                  for e in s.scalars(select(PlaybackEvent).order_by(PlaybackEvent.at.desc()).limit(10)).all()]
        statuses = {p.provider: p for p in s.scalars(select(ProviderStatus)).all()}
        providers = []
        for name, label in provider_labels().items():
            st = statuses.get(name)
            providers.append({"name": name, "label": label, **get_provider(name).status(),
                              "last_success_at": _iso(st.last_success_at) if st else None,
                              "last_error_at": _iso(st.last_error_at) if st else None,
                              "last_error": st.last_error if st else None})
        rules = [rule_out(r) for r in s.scalars(select(DiscoveryRule).order_by(DiscoveryRule.last_run_at.desc()).limit(8)).all()]
        unavailable = s.scalar(select(func.count()).select_from(PlaybackSource).where(PlaybackSource.health_ok.is_(False)))
        failures = s.scalar(select(func.count()).select_from(PlaybackEvent).where(PlaybackEvent.at > now - timedelta(days=1)))
    return {"counts": counts, "by_provider": by_provider, "channels": chans, "events": events, "providers": providers,
            "rules": rules, "unavailable_sources": unavailable, "failures_24h": failures,
            "streams": manager.status(), "catchup": manager.catchup_status(), "resources": monitor.snapshot(manager),
            "jobs": jobs.job_status()}


@router.get("/events")
def events(limit: int = 100):
    with session_scope() as s:
        return [{"at": _iso(e.at), "level": e.level, "message": e.message, "channel_id": e.channel_id,
                 "playback_source_id": e.playback_source_id}
                for e in s.scalars(select(PlaybackEvent).order_by(PlaybackEvent.at.desc()).limit(min(limit, 1000))).all()]


# ------------------------------------------------------------------ discovery

class RulePatch(BaseModel):
    name: str | None = None
    query: str | None = None
    params: dict | None = None
    scan_interval_minutes: int | None = Field(None, ge=15)
    max_results: int | None = Field(None, ge=1, le=500)
    enabled: bool | None = None
    channel_ids: list[int] | None = None


@router.patch("/discovery/rules/{rule_id}")
def patch_rule(rule_id: int, body: RulePatch):
    with session_scope() as s:
        r = s.get(DiscoveryRule, rule_id)
        if r is None:
            raise HTTPException(404, "unknown rule")
        changes = body.model_dump(exclude_unset=True)
        ids = changes.pop("channel_ids", None)
        for k, v in changes.items():
            setattr(r, k, v)
        if ids is not None:
            r.channels = list(s.scalars(select(Channel).where(Channel.id.in_(ids))).all()) if ids else []
        s.flush()
        return rule_out(r)


@router.delete("/discovery/rules/{rule_id}")
def delete_rule(rule_id: int):
    with session_scope() as s:
        r = s.get(DiscoveryRule, rule_id)
        if r is None:
            raise HTTPException(404, "unknown rule")
        s.delete(r)
    return {"deleted": rule_id}


class ImportIn(BaseModel):
    url: str


UNRECOGNISED_URL = "unrecognised link: paste an archive.org/details/… link or a YouTube video, playlist, @channel or @channel/live URL"


async def _import(url: str) -> DiscoveryResult:
    """Import one Archive item, or a YouTube video / playlist / channel, by URL.
    Raises ValueError for links that aren't from a supported site."""
    url = url.strip()
    m = _IA_URL.search(url)
    if ytstream.channel_live_url(url):
        return await import_youtube_live(url)
    if m:
        return await run_query("archive", RuleKind.VIDEO, m.group(1), {}, 1, interactive=True)
    if parse_youtube_url(url):
        kind, _ = parse_youtube_url(url)  # type: ignore[misc]
        return await run_query("youtube", RuleKind.VIDEO, url, {}, 50 if kind != "video" else 1, interactive=True)
    raise ValueError(UNRECOGNISED_URL)


@router.post("/discovery/import")
async def import_url(body: ImportIn):
    """Import one Archive item, or a YouTube video / playlist / channel, by URL."""
    try:
        res = await _import(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"found": res.found, "new": res.new, "skipped_known": res.skipped_known, "errors": res.errors,
            "item_ids": res.new_item_ids}


# ------------------------------------------------------------------ media

@router.get("/catalogue")
def catalogue(provider: str | None = None, status: str | None = None, q: str | None = None, eligibility: str | None = None,
              channel_id: int | None = None, page: int = 1, per_page: int = 50, sort: Literal["updated", "created", "title"] = "updated",
              detail: bool = False):
    """Search the catalogue (all statuses) — the review queue is status=PENDING_REVIEW."""
    per = max(1, min(per_page, 200))
    order = {"updated": MediaItem.updated_at.desc(), "created": MediaItem.created_at.desc(), "title": MediaItem.title}[sort]
    with session_scope() as s:
        stmt = _item_query(provider or None, status or None, q, eligibility or None, channel_id)
        total = s.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = s.scalars(stmt.order_by(order).offset((max(page, 1) - 1) * per).limit(per)).all()
        return {"total": total, "page": page, "pages": max(1, -(-total // per)),
                "items": [media_out(it, detail=detail, channel_items=[]) for it in rows]}


class MediaPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    publication_date: str | None = None
    series: str | None = None
    season: int | None = None
    episode: int | None = None
    duration: float | None = Field(None, gt=0)
    tags: list[str] | None = None
    categories: list[str] | None = None
    artwork_in_epg: bool | None = None
    metadata_locked: bool | None = None


@router.patch("/media/{item_id}")
def patch_media(item_id: int, body: MediaPatch, user: str = Depends(require_api)):
    """Edit metadata. Edited items are locked against provider refreshes unless
    metadata_locked is explicitly set false."""
    with session_scope() as s:
        it = _item(s, item_id)
        changes = body.model_dump(exclude_unset=True)
        for k, v in changes.items():
            setattr(it, k, v)
        if "title" in changes:
            it.title_key = title_key(it.title)
        if "metadata_locked" not in changes:
            it.metadata_locked = True
        audit(s, it, "edit_metadata", ", ".join(sorted(changes)), actor=user)
        s.flush()
        return _detail(s, it)


class StatusIn(BaseModel):
    status: ReviewStatus
    note: str | None = None


@router.post("/media/{item_id}/status")
def media_status(item_id: int, body: StatusIn, user: str = Depends(require_api)):
    with session_scope() as s:
        it = _item(s, item_id)
        set_review_status(s, it, body.status, actor=user, note=body.note)
        s.flush()
        return _detail(s, it)


@router.post("/media/{item_id}/refresh")
async def media_refresh(item_id: int):
    with session_scope() as s:
        _item(s, item_id)
    try:
        ok = await refresh_item(item_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"refresh failed: {e}")
    if not ok:
        raise HTTPException(404, "provider returned no record (item may be gone)")
    with session_scope() as s:
        return _detail(s, _item(s, item_id))


class AssignIn(BaseModel):
    role: ChannelRole = ChannelRole.PROGRAMME


@router.put("/media/{item_id}/channels/{channel_id}")
def assign(item_id: int, channel_id: int, body: AssignIn, user: str = Depends(require_api)):
    with session_scope() as s:
        it = _item(s, item_id)
        _channel(s, channel_id)
        assign_to_channel(s, it, channel_id, body.role)
        audit(s, it, "assign_channel", f"channel #{channel_id} ({body.role})", actor=user)
        s.flush()
        return _detail(s, it)


@router.delete("/media/{item_id}/channels/{channel_id}")
def unassign(item_id: int, channel_id: int, role: ChannelRole = ChannelRole.PROGRAMME, user: str = Depends(require_api)):
    with session_scope() as s:
        it = _item(s, item_id)
        ci = s.scalar(select(ChannelItem).where(ChannelItem.channel_id == channel_id, ChannelItem.media_item_id == item_id,
                                                ChannelItem.role == role))
        if ci is None:
            raise HTTPException(404, "not assigned")
        s.delete(ci)
        audit(s, it, "unassign_channel", f"channel #{channel_id} ({role})", actor=user)
        s.flush()
        return _detail(s, it)


class SourceIn(BaseModel):
    playback_type: Literal["direct_url", "local_file"]
    url: str | None = None
    local_path: str | None = None
    label: str | None = None
    rights_note: str | None = None
    priority: int = 50
    authorise: bool = False


@router.post("/media/{item_id}/sources", status_code=201)
async def add_source(item_id: int, body: SourceIn, user: str = Depends(require_api)):
    """Attach an independently authorised playback source (e.g. a rights-holder
    file for a YouTube-discovered programme). It is validated before saving."""
    with session_scope() as s:
        _item(s, item_id)
    ps = PlaybackSource(media_item_id=item_id, label=body.label, rights_note=body.rights_note, priority=body.priority)
    try:
        if body.playback_type == PlaybackType.DIRECT_URL:
            url = (body.url or "").strip()
            check_url_syntax(url)
            ps.playback_type, ps.url = PlaybackType.DIRECT_URL, url
        else:
            p = safe_local_path(body.local_path or "")
            ps.playback_type, ps.local_path = PlaybackType.LOCAL_FILE, str(p.relative_to(settings.media_dir))
    except UnsafeURLError as e:
        raise HTTPException(400, str(e))
    await validate_source(ps)
    with session_scope() as s:
        it = _item(s, item_id)
        s.add(ps)
        s.flush()
        audit(s, it, "add_source", f"source #{ps.id} {ps.playback_type}: {ps.url or ps.local_path}", actor=user)
        if body.authorise and ps.eligibility in (Eligibility.IPTV_READY, Eligibility.IPTV_NEEDS_TRANSCODE):
            set_rights(s, ps, RightsStatus.AUTHORISED, note=body.rights_note or "authorised when added", actor=user)
        s.flush()
        return _detail(s, it)


class AirIn(BaseModel):
    channel_id: int | None = None
    role: ChannelRole = ChannelRole.PROGRAMME


def _refresh_schedules(s, channel_ids: set[int]) -> None:
    """Pick up newly airable content now rather than at the next schedule job."""
    s.flush()
    for cid in channel_ids:
        ch = s.get(Channel, cid)
        if ch is not None:
            maintain(s, ch)


def _air_items(s, item_ids: list[int], channel_id: int | None, role: ChannelRole, actor: str) -> list[dict]:
    """Put items on air (and on a channel). Returns one result per item."""
    out, touched = [], set()
    for iid in item_ids:
        it = s.get(MediaItem, iid)
        if it is None:
            continue
        put_on_air(s, it, actor=actor)
        if channel_id:
            assign_to_channel(s, it, channel_id, role)
        s.flush()
        on = {ci.channel_id for ci in s.scalars(select(ChannelItem).where(ChannelItem.media_item_id == iid)).all()}
        problem = air_problem(it) or (None if on else "ready, but not on any channel yet: pick a channel")
        out.append({"id": it.id, "title": it.title, "on_air": problem is None, "problem": problem})
        touched |= on
    _refresh_schedules(s, touched)
    return out


@router.post("/media/{item_id}/air")
def media_air(item_id: int, body: AirIn, user: str = Depends(require_api)):
    """One click: approve, authorise its validated sources, optionally add to a
    channel, and refresh the affected schedules."""
    with session_scope() as s:
        it = _item(s, item_id)
        if body.channel_id is not None:
            _channel(s, body.channel_id)
        res = _air_items(s, [item_id], body.channel_id, body.role, user)[0]
        d = _detail(s, it)
        d["air"] = res
        return d


class LinksIn(BaseModel):
    urls: list[str] = Field(..., min_length=1, max_length=50)
    role: ChannelRole = ChannelRole.PROGRAMME


@router.post("/channels/{channel_id}/links")
async def channel_links(channel_id: int, body: LinksIn, request: Request, user: str = Depends(require_api)):
    """Paste links, get them on air: import each link (or reuse it if it is
    already in the catalogue), validate it, approve it, authorise its playable
    sources and add it to this channel, then refresh the schedule."""
    with session_scope() as s:
        _channel(s, channel_id)
    results = []
    for raw in body.urls:
        url = raw.strip()
        if not url:
            continue
        r: dict = {"url": url, "items": [], "error": None}
        results.append(r)
        try:
            res = await _import(url)
        except ValueError as e:
            r["error"] = str(e)
            continue
        ids = res.new_item_ids + [i for i in res.known_item_ids if i not in res.new_item_ids]
        if not ids:
            r["error"] = "; ".join(res.errors) or "nothing playable was found at this link"
            continue
        # A YouTube video imported while streaming was off has no stream
        # source yet: refresh it from YouTube, which adds one now.
        if settings.youtube_streaming:
            with session_scope() as s:
                no_stream = [i for i in res.known_item_ids
                             if (it := s.get(MediaItem, i)) is not None and it.provider == "youtube"
                             and not any(p.playback_type == PlaybackType.YOUTUBE_STREAM for p in it.playback_sources)]
            for iid in no_stream:
                try:
                    await refresh_item(iid)
                except Exception as e:  # noqa: BLE001 - reported per link below via air_problem
                    log.info("refresh of item %s failed: %s", iid, e)
        # Items imported earlier but never validated get a fresh check first.
        with session_scope() as s:
            stale = [i for i in res.known_item_ids
                     if (it := s.get(MediaItem, i)) is not None and not any(p.eligibility in (Eligibility.IPTV_READY,
                         Eligibility.IPTV_NEEDS_TRANSCODE) for p in it.playback_sources if p.playback_type in IPTV_TYPES)]
        if stale:
            await _revalidate(stale)
        with session_scope() as s:
            r["items"] = _air_items(s, ids, channel_id, body.role, user)
    return {"results": results, "channel": channel_detail(channel_id, request)}


class BulkIn(BaseModel):
    ids: list[int] = Field(..., min_length=1)
    action: Literal["air", "approve", "reject", "quarantine", "pending", "assign", "authorise", "catalogue_only", "revalidate"]
    note: str | None = None
    channel_id: int | None = None
    role: ChannelRole = ChannelRole.PROGRAMME


@router.post("/media/bulk")
async def bulk(body: BulkIn, user: str = Depends(require_api)):
    status_map = {"approve": ReviewStatus.APPROVED, "reject": ReviewStatus.REJECTED, "quarantine": ReviewStatus.QUARANTINED,
                  "pending": ReviewStatus.PENDING_REVIEW}
    revalidate, skipped = [], []
    if body.action == "air":
        with session_scope() as s:
            if body.channel_id is not None:
                _channel(s, body.channel_id)
            res = _air_items(s, body.ids, body.channel_id, body.role, user)
        not_airing = [r for r in res if not r["on_air"]]
        return {"action": "air", "count": len(res), "on_air": len(res) - len(not_airing), "not_airing": not_airing,
                "skipped_no_iptv_source": [r["id"] for r in not_airing]}
    with session_scope() as s:
        if body.channel_id is not None:
            _channel(s, body.channel_id)
        for it in s.scalars(select(MediaItem).where(MediaItem.id.in_(body.ids))).all():
            if body.action in status_map:
                set_review_status(s, it, status_map[body.action], actor=user, note=body.note)
                if body.action == "approve" and body.channel_id:
                    assign_to_channel(s, it, body.channel_id, body.role)
            elif body.action == "assign":
                if not body.channel_id:
                    raise HTTPException(400, "channel_id is required to assign")
                assign_to_channel(s, it, body.channel_id, body.role)
                audit(s, it, "assign_channel", f"channel #{body.channel_id}", actor=user)
            elif body.action in ("authorise", "catalogue_only"):
                rights = RightsStatus.AUTHORISED if body.action == "authorise" else RightsStatus.CATALOGUE_ONLY
                n = 0
                for ps in it.playback_sources:
                    if ps.playback_type in IPTV_TYPES and ps.eligibility in (Eligibility.IPTV_READY, Eligibility.IPTV_NEEDS_TRANSCODE):
                        set_rights(s, ps, rights, note=body.note or "bulk rights review", actor=user)
                        n += 1
                if n == 0:
                    skipped.append(it.id)
            elif body.action == "revalidate":
                revalidate.append(it.id)
    if revalidate:
        await _revalidate(revalidate)
    return {"action": body.action, "count": len(body.ids), "skipped_no_iptv_source": skipped}


# ------------------------------------------------------------------ playback sources

def source_out(ps: PlaybackSource) -> dict:
    return {
        "id": ps.id, "media_item_id": ps.media_item_id, "item_title": ps.media_item.title if ps.media_item else None,
        "type": ps.playback_type, "url": ps.url, "local_path": ps.local_path, "label": ps.label,
        "eligibility": ps.eligibility, "rights_status": ps.rights_status, "rights_note": ps.rights_note,
        "health_ok": ps.health_ok, "seekable": ps.seekable, "live": ps.live, "video_codec": ps.video_codec, "audio_codec": ps.audio_codec,
        "width": ps.width, "height": ps.height, "duration": ps.duration, "priority": ps.priority,
        "allow_cache": ps.allow_cache, "failure_count": ps.failure_count, "last_error": ps.last_error,
        "last_validated_at": _iso(ps.last_validated_at), "checksum": ps.checksum,
    }


@router.get("/sources")
def list_sources(rights: str | None = None, eligibility: str | None = None, type: str | None = None, unhealthy: bool = False,
                 limit: int = 500):
    with session_scope() as s:
        stmt = select(PlaybackSource)
        if rights:
            stmt = stmt.where(PlaybackSource.rights_status == rights)
        if eligibility:
            stmt = stmt.where(PlaybackSource.eligibility == eligibility)
        if type:
            stmt = stmt.where(PlaybackSource.playback_type == type)
        if unhealthy:
            stmt = stmt.where(PlaybackSource.health_ok.is_(False))
        rows = s.scalars(stmt.order_by(PlaybackSource.media_item_id, PlaybackSource.priority).limit(min(limit, 2000))).all()
        return [source_out(p) for p in rows]


def _source(s, source_id: int) -> PlaybackSource:
    ps = s.get(PlaybackSource, source_id)
    if ps is None:
        raise HTTPException(404, "unknown playback source")
    return ps


class SourcePatch(BaseModel):
    priority: int | None = None
    allow_cache: bool | None = None
    label: str | None = None


@router.patch("/sources/{source_id}")
def patch_source(source_id: int, body: SourcePatch):
    with session_scope() as s:
        ps = _source(s, source_id)
        for k, v in body.model_dump(exclude_unset=True).items():
            setattr(ps, k, v)
        s.flush()
        return source_out(ps)


class RightsIn(BaseModel):
    rights: RightsStatus
    note: str | None = None


@router.post("/sources/{source_id}/rights")
def source_rights(source_id: int, body: RightsIn, user: str = Depends(require_api)):
    with session_scope() as s:
        ps = _source(s, source_id)
        try:
            set_rights(s, ps, body.rights, note=body.note, actor=user)
        except ValueError as e:
            raise HTTPException(400, str(e))
        s.flush()
        return source_out(ps)


@router.post("/sources/{source_id}/validate")
async def source_validate(source_id: int):
    with session_scope() as s:
        ps = _source(s, source_id)
    r = await validate_source(ps)
    with session_scope() as s:
        s.merge(ps)
    with session_scope() as s:
        return {"ok": r.ok, "message": r.message, "source": source_out(_source(s, source_id))}


@router.delete("/sources/{source_id}")
def delete_source(source_id: int, user: str = Depends(require_api)):
    with session_scope() as s:
        ps = _source(s, source_id)
        audit(s, ps.media_item, "delete_source", f"source #{ps.id} {ps.url or ps.local_path}", actor=user)
        s.delete(ps)
    return {"deleted": source_id}


@router.get("/local/files")
def local_files():
    """Video files in the media folder, and which are already in the catalogue."""
    with session_scope() as s:
        known = set(s.scalars(select(MediaSource.external_id).where(MediaSource.provider == ProviderType.LOCAL)).all())
    return [{"path": p, "imported": p in known} for p in _media_files()]


class LocalImportIn(BaseModel):
    paths: list[str] = Field(..., min_length=1)
    approve: bool = False
    authorise: bool = False
    channel_id: int | None = None
    role: ChannelRole = ChannelRole.PROGRAMME


@router.post("/local/import")
async def local_import(body: LocalImportIn, user: str = Depends(require_api)):
    """Catalogue files from the media folder (idents, filler, rights-holder
    files). They go through review unless approved/authorised explicitly here."""
    import asyncio

    created = []
    for rel in body.paths:
        try:
            p = safe_local_path(rel)
        except UnsafeURLError:
            continue
        relp = str(p.relative_to(settings.media_dir))
        checksum = await asyncio.to_thread(file_sha1, p)
        with session_scope() as s:
            if s.scalar(select(MediaSource.id).where(MediaSource.provider == ProviderType.LOCAL, MediaSource.external_id == relp)):
                continue
            src = MediaSource(provider=ProviderType.LOCAL, external_id=relp, original_url=None, creator="Local asset",
                              availability_status="available")
            title = re.sub(r"[_.-]+", " ", p.stem).strip().title()
            it = MediaItem(source=src, title=title, title_key=title_key(title), review_status=ReviewStatus.VALIDATING,
                           flags=["Administrator-supplied file — confirm you hold usage rights"])
            it.playback_sources.append(PlaybackSource(playback_type=PlaybackType.LOCAL_FILE, local_path=relp, label=p.name,
                                                      priority=10, checksum=checksum))
            s.add(it)
            s.flush()
            flag_duplicate(s, it)
            created.append(it.id)
    await _revalidate(created)
    with session_scope() as s:
        for iid in created:
            it = s.get(MediaItem, iid)
            it.duration = it.duration or (it.playback_sources[0].duration if it.playback_sources else None)
            if body.approve:
                set_review_status(s, it, ReviewStatus.APPROVED, actor=user, note="approved on local import")
            else:
                it.review_status = ReviewStatus.PENDING_REVIEW
            if body.authorise:
                for ps in it.playback_sources:
                    if ps.eligibility in (Eligibility.IPTV_READY, Eligibility.IPTV_NEEDS_TRANSCODE):
                        set_rights(s, ps, RightsStatus.AUTHORISED, note="administrator-supplied file", actor=user)
            if body.channel_id:
                assign_to_channel(s, it, body.channel_id, body.role)
    return {"imported": created}


# ------------------------------------------------------------------ channels

@router.get("/channels/{channel_id}")
def channel_detail(channel_id: int, request: Request):
    base = settings.public_base_url or str(request.base_url).rstrip("/")
    with session_scope() as s:
        ch = _channel(s, channel_id)
        d = channel_out(ch, s)
        d.update({
            "idents_between": ch.idents_between, "filler_pad": ch.filler_pad, "logo_file": ch.logo_file,
            "logo": logos.url(ch, base), "rule_ids": [r.id for r in ch.rules],
            "items": [{"id": ci.id, "media_item_id": ci.media_item_id, "title": ci.media_item.title if ci.media_item else None,
                       "role": ci.role, "position": ci.position, "duration": ci.media_item.duration if ci.media_item else None,
                       "review_status": ci.media_item.review_status if ci.media_item else None,
                       "schedulable": bool(ci.media_item and is_schedulable(ci.media_item)),
                       "problem": air_problem(ci.media_item) if ci.media_item else "item deleted",
                       "provider": ci.media_item.provider if ci.media_item else None,
                       "thumbnail_url": ci.media_item.thumbnail_url if ci.media_item else None,
                       "live": bool(ci.media_item and any(p.live for p in ci.media_item.playback_sources))} for ci in ch.items],
            "blocks": [{"id": b.id, "name": b.name, "days": b.days, "start_time": b.start_time, "end_time": b.end_time,
                        "tag_filter": b.tag_filter, "mode": b.mode} for b in ch.blocks],
        })
        return d


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: int, confirm: str):
    """Delete a channel (catalogue items are untouched). ``confirm`` must equal its slug."""
    with session_scope() as s:
        ch = _channel(s, channel_id)
        if confirm != ch.slug:
            raise HTTPException(400, "confirm must equal the channel slug")
    st = manager.streams.get(channel_id)
    if st:
        await st.stop()
    with session_scope() as s:
        s.delete(s.get(Channel, channel_id))
    return {"deleted": channel_id}


class ChannelItemsIn(BaseModel):
    item_ids: list[int] = Field(..., min_length=1)
    role: ChannelRole = ChannelRole.PROGRAMME


@router.post("/channels/{channel_id}/items")
def add_channel_items(channel_id: int, body: ChannelItemsIn, request: Request):
    with session_scope() as s:
        _channel(s, channel_id)
        for iid in body.item_ids:
            it = s.get(MediaItem, iid)
            if it is not None:
                assign_to_channel(s, it, channel_id, body.role)
    return channel_detail(channel_id, request)


class ChannelItemPatch(BaseModel):
    role: ChannelRole | None = None
    position: int | None = None


@router.patch("/channels/{channel_id}/items/{ci_id}")
def patch_channel_item(channel_id: int, ci_id: int, body: ChannelItemPatch, request: Request, user: str = Depends(require_api)):
    with session_scope() as s:
        ci = s.get(ChannelItem, ci_id)
        if ci is None or ci.channel_id != channel_id:
            raise HTTPException(404, "unknown channel item")
        changes = body.model_dump(exclude_unset=True)
        changed = any(getattr(ci, k) != v for k, v in changes.items())
        for k, v in changes.items():
            setattr(ci, k, v)
        if changed:
            _rebuild_upcoming(s, channel_id, user)
    return channel_detail(channel_id, request)


def _rebuild_upcoming(s, channel_id: int, actor: str) -> None:
    """Apply a playlist change to the published schedule straight away. The
    programme on air now is kept; everything after it is rebuilt."""
    s.flush()
    s.expire_all()  # the channel's item list may predate the change
    ch = s.get(Channel, channel_id)
    if ch is not None and ch.enabled:
        regenerate(s, ch, actor=actor)


class ChannelOrderIn(BaseModel):
    item_ids: list[int] = Field(..., min_length=1, description="Channel item ids in the new playing order")


@router.post("/channels/{channel_id}/items/order")
def order_channel_items(channel_id: int, body: ChannelOrderIn, request: Request, user: str = Depends(require_api)):
    """Set the playing order in one go (positions 10, 20, …) and rebuild the upcoming schedule once."""
    with session_scope() as s:
        ch = _channel(s, channel_id)
        by_id = {ci.id: ci for ci in ch.items}
        if set(body.item_ids) - set(by_id):
            raise HTTPException(400, "unknown channel item in order")
        rest = [ci.id for ci in sorted(ch.items, key=lambda c: c.position) if ci.id not in body.item_ids]
        for n, cid in enumerate(body.item_ids + rest, start=1):
            by_id[cid].position = n * 10
        _rebuild_upcoming(s, channel_id, user)
    return channel_detail(channel_id, request)


@router.delete("/channels/{channel_id}/items/{ci_id}")
def delete_channel_item(channel_id: int, ci_id: int, request: Request, user: str = Depends(require_api)):
    with session_scope() as s:
        ci = s.get(ChannelItem, ci_id)
        if ci is None or ci.channel_id != channel_id:
            raise HTTPException(404, "unknown channel item")
        s.delete(ci)
        _rebuild_upcoming(s, channel_id, user)
    return channel_detail(channel_id, request)


class BlockIn(BaseModel):
    name: str
    start_time: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    days: str = Field("0123456", pattern=r"^[0-6]{1,7}$", description="Mon=0 … Sun=6")
    tag_filter: str | None = None
    mode: Literal["sequential", "shuffle"] = "shuffle"


@router.post("/channels/{channel_id}/blocks", status_code=201)
def add_block(channel_id: int, body: BlockIn, request: Request):
    with session_scope() as s:
        _channel(s, channel_id)
        s.add(ChannelBlock(channel_id=channel_id, name=body.name, start_time=body.start_time, end_time=body.end_time,
                           tag_filter=body.tag_filter or None, mode=body.mode, days="".join(sorted(set(body.days)))))
    return channel_detail(channel_id, request)


@router.delete("/blocks/{block_id}")
def delete_block(block_id: int):
    with session_scope() as s:
        b = s.get(ChannelBlock, block_id)
        if b is None:
            raise HTTPException(404, "unknown block")
        s.delete(b)
    return {"deleted": block_id}


@router.post("/channels/{channel_id}/logo")
async def upload_logo(channel_id: int, request: Request, file: UploadFile = File(...)):
    data = await file.read(logos.MAX_BYTES + 1)
    with session_scope() as s:
        ch = _channel(s, channel_id)
        try:
            ch.logo_file = logos.save(ch.slug, data, ch.logo_file)
        except logos.LogoError as e:
            raise HTTPException(400, str(e))
    return channel_detail(channel_id, request)


@router.delete("/channels/{channel_id}/logo")
def delete_logo(channel_id: int, request: Request):
    with session_scope() as s:
        ch = _channel(s, channel_id)
        logos.remove(ch.logo_file)
        ch.logo_file = None
    return channel_detail(channel_id, request)


# ------------------------------------------------------------------ schedule

@router.get("/channels/{channel_id}/schedule/day")
def schedule_day(channel_id: int, day: str | None = None):
    """One local (display-timezone) day of a channel's timeline, with
    continuity problems, the entry on air, and items eligible for overrides."""
    tz = settings.timezone
    today = datetime.now(tz).date()
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date() if day else today
    except ValueError:
        raise HTTPException(400, "day must be YYYY-MM-DD")
    start_local = datetime.combine(d, datetime.min.time(), tzinfo=tz)
    start = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end = (start_local + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        ch = _channel(s, channel_id)
        rows = list(s.scalars(select(ScheduleEntry).where(ScheduleEntry.channel_id == ch.id, ScheduleEntry.end > start,
                                                           ScheduleEntry.start < end).order_by(ScheduleEntry.start)).all())
        cur = current_entry(s, ch.id)
        eligible = [{"id": ci.media_item.id, "title": ci.media_item.title, "duration": ci.media_item.duration}
                    for ci in ch.items if ci.role == ChannelRole.PROGRAMME and ci.media_item and is_schedulable(ci.media_item)]
        back = history_days(ch)
        return {"day": d.isoformat(), "entries": [entry_out(e) for e in rows], "problems": check_continuity(rows),
                "current_id": cur.id if cur else None, "eligible": eligible, "catchup_enabled": ch.catchup_enabled,
                "days": [(today + timedelta(days=i)).isoformat() for i in range(-back, settings.schedule_days + 1)],
                "now": _iso(utcnow())}


@router.get("/channels/{channel_id}/schedule/versions")
def schedule_versions(channel_id: int, limit: int = 50):
    with session_scope() as s:
        _channel(s, channel_id)
        rows = s.scalars(select(ScheduleVersion).where(ScheduleVersion.channel_id == channel_id)
                         .order_by(ScheduleVersion.id.desc()).limit(min(limit, 500))).all()
        return [{"id": v.id, "created_at": _iso(v.created_at), "updated_at": _iso(v.updated_at), "reason": v.reason,
                 "actor": v.actor, "changed_from": _iso(v.changed_from), "horizon": _iso(v.horizon),
                 "entries_added": v.entries_added, "entries_removed": v.entries_removed, "note": v.note} for v in rows]


class OverrideIn(BaseModel):
    item_id: int


@router.post("/schedule/entries/{entry_id}/override")
def schedule_override(entry_id: int, body: OverrideIn, user: str = Depends(require_api)):
    with session_scope() as s:
        e = s.get(ScheduleEntry, entry_id)
        if e is None:
            raise HTTPException(404, "unknown schedule entry")
        ch = s.get(Channel, e.channel_id)
        try:
            new = override(s, ch, entry_id, body.item_id, actor=user)
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        audit(s, new.media_item, "schedule_override", f"{ch.slug} at {new.start.isoformat()}Z", actor=user)
        return entry_out(new)


@router.post("/schedule/maintain")
def schedule_maintain():
    out = {}
    with session_scope() as s:
        for ch in s.scalars(select(Channel).where(Channel.enabled.is_(True))).all():
            r = maintain(s, ch)
            out[ch.slug] = {"rebuilt_from": _iso(r["rebuilt_from"]), "added": r["added"]}
    return out


# ------------------------------------------------------------------ catch-up test

class CatchupTestIn(BaseModel):
    channel_id: int
    at: str = Field(..., description="Local (display timezone) time, ISO 8601, or Unix seconds")


@router.post("/catchup/test")
async def catchup_test(body: CatchupTestIn):
    """Resolve a historical time and check its source answers, without streaming."""
    with session_scope() as s:
        ch = _channel(s, body.channel_id)
        try:
            start = catchup.parse_timestamp(body.at, settings.timezone)
            req = catchup.resolve_request(s, ch, start)
        except catchup.CatchupError as e:
            return {"ok": False, "status": e.status, "message": e.message}
    try:
        await catchup.preflight(req)
    except catchup.CatchupError as e:
        return {"ok": False, "status": e.status, "message": e.message}
    return {"ok": True, "title": req.title, "start": _iso(req.start), "offset_seconds": req.offset,
            "programme_start": _iso(req.programme_start), "programme_end": _iso(req.programme_end)}


# ------------------------------------------------------------------ settings

@router.get("/settings")
def get_settings(request: Request):
    base = settings.public_base_url or str(request.base_url).rstrip("/")
    with session_scope() as s:
        statuses = {p.provider: {"quota_used": p.quota_used, "quota_exhausted_until": _iso(p.quota_exhausted_until)}
                    for p in s.scalars(select(ProviderStatus)).all()}
    backups = sorted((settings.data_dir / "backups").glob("*.sqlite3"), reverse=True)[:10]
    return {
        "base_url": base,
        "tunables": [{"key": k, "type": kind.__name__, "label": label, "help": help_, "value": getattr(settings, k)}
                     for k, (kind, label, help_) in appsettings.TUNABLES.items()],
        "xtream": {"server_url": base, "username": settings.xc_username,
                   "password_source": "ATV_XC_PASSWORD" if (os.environ.get("ATV_XC_PASSWORD") or os.environ.get("ATV_XC_PASSWORD_FILE"))
                   else "docker exec archivetv cat /data/run/xc_password"},
        "feeds": {"m3u": f"{base}/playlist.m3u", "xmltv": f"{base}/epg.xml", "token_set": bool(settings.feed_token)},
        "youtube_configured": bool(settings.youtube_api_key), "provider_status": statuses,
        "media_dir": str(settings.media_dir), "allowed_hosts": ["archive.org"] + settings.extra_media_hosts,
        "minimal_disk": settings.minimal_disk, "backups": [b.name for b in backups],
    }


@router.patch("/settings")
def patch_settings(values: dict[str, str | int | bool | None]):
    """Update admin-tunable (non-secret) settings."""
    unknown = set(values) - set(appsettings.TUNABLES)
    if unknown:
        raise HTTPException(400, f"unknown settings: {', '.join(sorted(unknown))}")
    current = {k: getattr(settings, k) for k in appsettings.TUNABLES}
    merged = {k: ("" if v is None else str(v)) for k, v in current.items() if not isinstance(v, bool)}
    for k, v in current.items():
        if isinstance(v, bool) and v:
            merged[k] = "1"
    for k, v in values.items():
        if isinstance(v, bool):
            if v:
                merged[k] = "1"
            else:
                merged.pop(k, None)
        else:
            merged[k] = "" if v is None else str(v)
    errors = appsettings.save(merged)
    if errors:
        raise HTTPException(400, "; ".join(errors))
    return {"ok": True}


@router.post("/system/backup")
def system_backup():
    return {"backup": backup_database().name}


@router.get("/media/{item_id}/history")
def media_history(item_id: int):
    with session_scope() as s:
        _item(s, item_id)
        rows = s.scalars(select(AuditLog).where(AuditLog.media_item_id == item_id).order_by(AuditLog.at.desc()).limit(100)).all()
        dups = s.scalars(select(MediaItem).where(or_(MediaItem.duplicate_of_id == item_id,
                                                     MediaItem.id == s.get(MediaItem, item_id).duplicate_of_id))).all()
        best = best_iptv_source(s.get(MediaItem, item_id))
        return {"history": [{"at": _iso(a.at), "actor": a.actor, "action": a.action, "from": a.from_status, "to": a.to_status,
                             "note": a.note} for a in rows],
                "duplicates": [media_out(d) for d in dups], "best_source_id": best.id if best else None}
