"""Administration dashboard (server-rendered, no build step)."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import appsettings, catchup, jobs, logos, monitor, ytstream
from ..catalogue import (
    IPTV_TYPES,
    assign_to_channel,
    audit,
    best_iptv_source,
    file_sha1,
    flag_duplicate,
    is_schedulable,
    item_eligibility,
    iptv_status,
    set_review_status,
    set_rights,
    title_key,
)
from ..config import settings
from ..db import backup_database, get_db, session_scope, utcnow
from ..discovery import import_youtube_live, refresh_item, run_query, run_rule, validate_items
from ..enums import (
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
from ..models import (
    AuditLog,
    CatchupSession,
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
from ..providers import get_provider, provider_labels
from ..providers.youtube import parse_youtube_url
from ..scheduler import RETENTION_CHOICES, check_continuity, current_entry, history_days, maintain, override, regenerate, to_local
from ..security import UnsafeURLError, check_url_syntax, safe_local_path
from ..stream import manager
from ..validator import validate_source

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class LoginRequired(Exception):
    pass


def require_admin(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise LoginRequired()
    return user


def local(dt: datetime | None, fmt: str = "%a %d %b %H:%M") -> str:
    return to_local(dt).strftime(fmt) if dt else "—"


def fmt_duration(sec: float | None) -> str:
    if not sec:
        return "—"
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


templates.env.filters["local"] = local
templates.env.filters["dur"] = fmt_duration
templates.env.globals.update(
    ReviewStatus=ReviewStatus,
    Eligibility=Eligibility,
    RightsStatus=RightsStatus,
    PlaybackType=PlaybackType,
    ChannelRole=ChannelRole,
    ScheduleMode=ScheduleMode,
    PlaybackFormat=PlaybackFormat,
    RuleKind=RuleKind,
    provider_labels=provider_labels,
    iptv_status=iptv_status,
    item_eligibility=item_eligibility,
    tz_name=lambda: str(settings.timezone),
    RETENTION_CHOICES=RETENTION_CHOICES,
    history_days=history_days,
)


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    flash = request.session.pop("flash", None)
    with session_scope() as s:
        pending = s.scalar(select(func.count()).select_from(MediaItem).where(MediaItem.review_status == ReviewStatus.PENDING_REVIEW))
    return templates.TemplateResponse(request, name, {"user": request.session.get("user"), "flash": flash, "pending_count": pending, **ctx})


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session["flash"] = {"msg": msg, "kind": kind}


def back(request: Request, default: str) -> RedirectResponse:
    ref = request.headers.get("referer") or ""
    host = request.url.netloc
    if ref and re.match(rf"^https?://{re.escape(host)}/admin", ref):
        return RedirectResponse(ref, status_code=303)
    return RedirectResponse(default, status_code=303)


# ---------------------------------------------------------------- auth

_attempts: dict[str, list[float]] = defaultdict(list)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/admin"):
    return render(request, "login.html", next=next if next.startswith("/admin") else "/admin")


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/admin")):
    ip = request.client.host if request.client else "?"
    now = time.monotonic()
    _attempts[ip] = [t for t in _attempts[ip] if now - t < 60]
    if len(_attempts[ip]) >= 5:
        flash(request, "Too many attempts; wait a minute.", "error")
        return RedirectResponse("/login", status_code=303)
    ok = secrets.compare_digest(username.encode(), settings.admin_username.encode()) & secrets.compare_digest(
        password.encode(), (settings.admin_password or "").encode()
    )
    if not ok:
        _attempts[ip].append(now)
        flash(request, "Invalid username or password.", "error")
        return RedirectResponse("/login", status_code=303)
    request.session.clear()
    request.session["user"] = username
    return RedirectResponse(next if next.startswith("/admin") else "/admin", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------- dashboard


@router.get("/admin", response_class=HTMLResponse)
def dashboard(request: Request, user: str = Depends(require_admin), s: Session = Depends(get_db)):
    counts = dict(s.execute(select(MediaItem.review_status, func.count()).group_by(MediaItem.review_status)).all())
    by_provider = dict(s.execute(select(MediaSource.provider, func.count()).group_by(MediaSource.provider)).all())
    channels = s.scalars(select(Channel).order_by(Channel.number, Channel.name)).all()
    now = utcnow()
    chan_rows = []
    for ch in channels:
        cur = current_entry(s, ch.id, now)
        nxt = s.scalar(select(ScheduleEntry).where(ScheduleEntry.channel_id == ch.id, ScheduleEntry.start >= (cur.end if cur else now)).order_by(ScheduleEntry.start).limit(1))
        horizon = s.scalar(select(func.max(ScheduleEntry.end)).where(ScheduleEntry.channel_id == ch.id))
        eligible = sum(1 for ci in ch.items if ci.role == ChannelRole.PROGRAMME and ci.media_item and is_schedulable(ci.media_item))
        chan_rows.append({"ch": ch, "cur": cur, "next": nxt, "horizon": horizon, "eligible": eligible})
    events = s.scalars(select(PlaybackEvent).order_by(PlaybackEvent.at.desc()).limit(10)).all()
    statuses = {p.provider: p for p in s.scalars(select(ProviderStatus)).all()}
    rules = s.scalars(select(DiscoveryRule).order_by(DiscoveryRule.last_run_at.desc())).all()
    providers = []
    for name, label in provider_labels().items():
        st = statuses.get(name)
        info = get_provider(name).status()
        providers.append({"name": name, "label": label, "st": st, "info": info})
    return render(
        request,
        "dashboard.html",
        counts=counts,
        by_provider=by_provider,
        chan_rows=chan_rows,
        events=events,
        providers=providers,
        rules=rules[:8],
        streams=manager.status(),
        catchups=manager.catchup_status(),
        res=monitor.snapshot(manager),
        unavailable=s.scalar(select(func.count()).select_from(PlaybackSource).where(PlaybackSource.health_ok.is_(False))),
        failures_24h=s.scalar(select(func.count()).select_from(PlaybackEvent).where(PlaybackEvent.at > now - timedelta(days=1))),
        jobs=jobs.last_run,
    )


# ---------------------------------------------------------------- discovery

RULE_PARAM_KEYS = [
    "collection", "creator", "subject", "year_from", "year_to", "sort", "licensed_only",
    "channel", "playlist", "published_after", "published_before", "min_duration", "max_duration",
    "categories", "excluded_channels", "excluded_keywords", "region", "embeddable_only",
    "preferred_formats", "preferred_height", "skip_validation",
]


def _rule_params(form: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in RULE_PARAM_KEYS:
        v = form.get(k)
        if v in (None, ""):
            continue
        if k in ("min_duration", "max_duration"):
            try:
                out[k] = _parse_duration_input(str(v))
            except ValueError:
                continue
        elif k in ("licensed_only", "embeddable_only", "skip_validation"):
            out[k] = True
        else:
            out[k] = str(v).strip()
    return out


def _parse_duration_input(v: str) -> float:
    v = v.strip()
    if ":" in v:
        secs = 0.0
        for p in v.split(":"):
            secs = secs * 60 + float(p)
        return secs
    if v.endswith("m"):
        return float(v[:-1]) * 60
    return float(v) * 60  # bare numbers are minutes in the UI


@router.get("/admin/discovery", response_class=HTMLResponse)
def discovery_page(request: Request, user: str = Depends(require_admin), s: Session = Depends(get_db), edit: int | None = None):
    rules = s.scalars(select(DiscoveryRule).order_by(DiscoveryRule.provider, DiscoveryRule.name)).all()
    channels = s.scalars(select(Channel).order_by(Channel.name)).all()
    editing = s.get(DiscoveryRule, edit) if edit else None
    statuses = {p.provider: p for p in s.scalars(select(ProviderStatus)).all()}
    yt = get_provider("youtube").status()
    return render(request, "discovery.html", rules=rules, channels=channels, editing=editing, statuses=statuses, yt=yt)


@router.post("/admin/discovery")
async def discovery_save(request: Request, user: str = Depends(require_admin)):
    form = dict(await request.form())
    channel_ids = [int(x) for x in (await request.form()).getlist("channel_ids")]
    rule_id = int(form["rule_id"]) if form.get("rule_id") else None
    provider = form.get("provider", "archive")
    kind = form.get("kind", RuleKind.SEARCH)
    if provider not in provider_labels() or kind not in set(RuleKind):
        raise HTTPException(400, "bad provider/kind")
    with session_scope() as s:
        rule = s.get(DiscoveryRule, rule_id) if rule_id else DiscoveryRule()
        rule.name = (form.get("name") or "").strip() or f"{provider} {kind}"
        rule.provider = provider
        rule.kind = kind
        rule.query = (form.get("query") or "").strip() or None
        rule.params = _rule_params(form)
        rule.scan_interval_minutes = max(15, int(form.get("scan_interval_minutes") or 1440))
        rule.max_results = max(1, min(500, int(form.get("max_results") or 50)))
        rule.enabled = bool(form.get("enabled"))
        rule.channels = [c for c in s.scalars(select(Channel).where(Channel.id.in_(channel_ids))).all()] if channel_ids else []
        s.add(rule)
    flash(request, f"Discovery rule '{form.get('name') or kind}' saved.")
    return RedirectResponse("/admin/discovery", status_code=303)


@router.post("/admin/discovery/{rule_id}/run")
async def discovery_run(rule_id: int, request: Request, user: str = Depends(require_admin)):
    res = await run_rule(rule_id, interactive=True)
    if res.errors:
        flash(request, f"Scan finished with errors: {'; '.join(res.errors)}", "error")
    else:
        flash(request, f"Scan complete: {res.found} found, {res.new} new, {res.skipped_known} already known, {res.filtered} filtered out.")
    return RedirectResponse("/admin/discovery", status_code=303)


@router.post("/admin/discovery/{rule_id}/delete")
def discovery_delete(rule_id: int, request: Request, user: str = Depends(require_admin)):
    with session_scope() as s:
        r = s.get(DiscoveryRule, rule_id)
        if r:
            s.delete(r)
    flash(request, "Rule deleted. Items it discovered remain in the catalogue.")
    return RedirectResponse("/admin/discovery", status_code=303)


_IA_URL = re.compile(r"archive\.org/(?:details|download|embed)/([^/?#]+)")


@router.post("/admin/import")
async def import_url(request: Request, url: str = Form(...), user: str = Depends(require_admin)):
    """Import a single Archive item or YouTube video/playlist/channel by URL."""
    url = url.strip()
    m = _IA_URL.search(url)
    if ytstream.channel_live_url(url):
        res = await import_youtube_live(url)
    elif m:
        res = await run_query("archive", RuleKind.VIDEO, m.group(1), {}, 1, interactive=True)
    elif parse_youtube_url(url):
        kind, _ = parse_youtube_url(url)  # type: ignore[misc]
        res = await run_query("youtube", RuleKind.VIDEO, url, {}, 50 if kind != "video" else 1, interactive=True)
    else:
        flash(request, "Unrecognised URL. Paste an archive.org/details/… link or a YouTube video, playlist or @channel URL.", "error")
        return RedirectResponse("/admin/discovery", status_code=303)
    if res.errors:
        flash(request, f"Import failed: {'; '.join(res.errors)}", "error")
    elif res.new:
        flash(request, f"Imported {res.new} item(s) into the review queue.")
    else:
        flash(request, f"Nothing new ({res.skipped_known} already in catalogue).")
    return RedirectResponse("/admin/review", status_code=303)


# ---------------------------------------------------------------- review & catalogue


def _item_query(provider: str | None, status: str | None, q: str | None, eligibility: str | None, channel: int | None):
    stmt = select(MediaItem).join(MediaSource)
    if provider:
        stmt = stmt.where(MediaSource.provider == provider)
    if status:
        stmt = stmt.where(MediaItem.review_status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(MediaItem.title.ilike(like), MediaItem.description.ilike(like), MediaSource.creator.ilike(like), MediaSource.external_id.ilike(like)))
    if eligibility:
        stmt = stmt.where(MediaItem.playback_sources.any(PlaybackSource.eligibility == eligibility))
    if channel:
        stmt = stmt.where(MediaItem.id.in_(select(ChannelItem.media_item_id).where(ChannelItem.channel_id == channel)))
    return stmt


@router.get("/admin/review", response_class=HTMLResponse)
def review_page(
    request: Request,
    provider: str | None = None,
    status: str = ReviewStatus.PENDING_REVIEW,
    q: str | None = None,
    page: int = 1,
    user: str = Depends(require_admin),
    s: Session = Depends(get_db),
):
    per = 20
    stmt = _item_query(provider or None, status or None, q, None, None)
    total = s.scalar(select(func.count()).select_from(stmt.subquery()))
    items = s.scalars(stmt.order_by(MediaItem.created_at.desc()).offset((page - 1) * per).limit(per)).all()
    channels = s.scalars(select(Channel).order_by(Channel.name)).all()
    return render(request, "review.html", items=items, channels=channels, provider=provider, status=status, q=q, page=page,
                  pages=max(1, -(-total // per)), total=total)


@router.post("/admin/review/bulk")
async def review_bulk(request: Request, user: str = Depends(require_admin)):
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids")]
    action = form.get("action")
    note = (form.get("note") or "").strip() or None
    channel_id = int(form["channel_id"]) if form.get("channel_id") else None
    if not ids:
        flash(request, "Select at least one item.", "error")
        return back(request, "/admin/review")
    status_map = {
        "approve": ReviewStatus.APPROVED,
        "reject": ReviewStatus.REJECTED,
        "quarantine": ReviewStatus.QUARANTINED,
        "pending": ReviewStatus.PENDING_REVIEW,
    }
    revalidate: list[int] = []
    msg = ""
    with session_scope() as s:
        items = s.scalars(select(MediaItem).where(MediaItem.id.in_(ids))).all()
        for item in items:
            if action in status_map:
                set_review_status(s, item, status_map[action], actor=user, note=note)
                if action == "approve" and channel_id:
                    assign_to_channel(s, item, channel_id)
            elif action == "assign" and channel_id:
                assign_to_channel(s, item, channel_id)
                audit(s, item, "assign_channel", f"channel #{channel_id}", actor=user)
            elif action in ("authorise", "catalogue_only"):
                rights = RightsStatus.AUTHORISED if action == "authorise" else RightsStatus.CATALOGUE_ONLY
                n = 0
                for ps in item.playback_sources:
                    if ps.playback_type in IPTV_TYPES and ps.eligibility in (Eligibility.IPTV_READY, Eligibility.IPTV_NEEDS_TRANSCODE):
                        set_rights(s, ps, rights, note=note or "bulk rights review", actor=user)
                        n += 1
                if n == 0 and action == "authorise":
                    msg = " Some items had no validated IPTV-capable source to authorise."
            elif action == "revalidate":
                revalidate.append(item.id)
    if revalidate:
        await _revalidate(revalidate)
    flash(request, f"{action.replace('_', ' ').title() if action else 'Done'}: {len(ids)} item(s).{msg}")
    return back(request, "/admin/review")


async def _revalidate(item_ids: list[int]) -> None:
    for iid in item_ids:
        with session_scope() as s:
            item = s.get(MediaItem, iid)
            sources = list(item.playback_sources) if item else []
        for ps in sources:
            await validate_source(ps)
            with session_scope() as s:
                s.merge(ps)


@router.get("/admin/catalogue", response_class=HTMLResponse)
def catalogue_page(
    request: Request,
    provider: str | None = None,
    status: str | None = None,
    q: str | None = None,
    eligibility: str | None = None,
    channel: int | None = None,
    page: int = 1,
    user: str = Depends(require_admin),
    s: Session = Depends(get_db),
):
    per = 50
    stmt = _item_query(provider or None, status or None, q, eligibility or None, channel)
    total = s.scalar(select(func.count()).select_from(stmt.subquery()))
    items = s.scalars(stmt.order_by(MediaItem.updated_at.desc()).offset((page - 1) * per).limit(per)).all()
    channels = s.scalars(select(Channel).order_by(Channel.name)).all()
    return render(request, "catalogue.html", items=items, channels=channels, provider=provider, status=status, q=q,
                  eligibility=eligibility, channel=channel, page=page, pages=max(1, -(-total // per)), total=total)


@router.get("/admin/items/{item_id}", response_class=HTMLResponse)
def item_page(item_id: int, request: Request, user: str = Depends(require_admin), s: Session = Depends(get_db)):
    item = s.get(MediaItem, item_id)
    if not item:
        raise HTTPException(404)
    history = s.scalars(select(AuditLog).where(AuditLog.media_item_id == item_id).order_by(AuditLog.at.desc()).limit(50)).all()
    channels = s.scalars(select(Channel).order_by(Channel.name)).all()
    assigned = s.scalars(select(ChannelItem).where(ChannelItem.media_item_id == item_id)).all()
    dups = s.scalars(select(MediaItem).where(or_(MediaItem.duplicate_of_id == item_id, MediaItem.id == item.duplicate_of_id))).all()
    media_files = _media_files()
    return render(request, "item.html", item=item, history=history, channels=channels, assigned=assigned, dups=dups,
                  best=best_iptv_source(item), media_files=media_files)


@router.post("/admin/items/{item_id}")
async def item_save(item_id: int, request: Request, user: str = Depends(require_admin)):
    form = await request.form()
    with session_scope() as s:
        item = s.get(MediaItem, item_id)
        if not item:
            raise HTTPException(404)
        item.title = (form.get("title") or item.title).strip()
        item.title_key = title_key(item.title)
        item.description = form.get("description") or None
        item.publication_date = form.get("publication_date") or None
        item.series = form.get("series") or None
        item.season = int(form["season"]) if (form.get("season") or "").isdigit() else None
        item.episode = int(form["episode"]) if (form.get("episode") or "").isdigit() else None
        item.tags = [t.strip() for t in (form.get("tags") or "").split(",") if t.strip()]
        item.categories = [t.strip() for t in (form.get("categories") or "").split(",") if t.strip()]
        item.artwork_in_epg = bool(form.get("artwork_in_epg"))
        if form.get("duration"):
            try:
                item.duration = _parse_duration_input(form["duration"]) if ":" in form["duration"] else float(form["duration"])
            except ValueError:
                pass
        item.metadata_locked = True
        audit(s, item, "edit_metadata", actor=user)
    flash(request, "Metadata saved (locked against provider refresh).")
    return RedirectResponse(f"/admin/items/{item_id}", status_code=303)


@router.post("/admin/items/{item_id}/status")
def item_status(item_id: int, request: Request, status: str = Form(...), note: str = Form(""), user: str = Depends(require_admin)):
    if status not in set(ReviewStatus):
        raise HTTPException(400)
    with session_scope() as s:
        item = s.get(MediaItem, item_id)
        if not item:
            raise HTTPException(404)
        set_review_status(s, item, ReviewStatus(status), actor=user, note=note or None)
    flash(request, f"Status set to {status}.")
    return back(request, f"/admin/items/{item_id}")


@router.post("/admin/items/{item_id}/channels")
def item_channels(item_id: int, request: Request, channel_id: int = Form(...), role: str = Form(ChannelRole.PROGRAMME),
                  remove: str = Form(""), user: str = Depends(require_admin)):
    with session_scope() as s:
        item = s.get(MediaItem, item_id)
        if not item:
            raise HTTPException(404)
        if remove:
            ci = s.scalar(select(ChannelItem).where(ChannelItem.channel_id == channel_id, ChannelItem.media_item_id == item_id, ChannelItem.role == role))
            if ci:
                s.delete(ci)
            audit(s, item, "unassign_channel", f"channel #{channel_id} ({role})", actor=user)
        else:
            assign_to_channel(s, item, channel_id, ChannelRole(role))
            audit(s, item, "assign_channel", f"channel #{channel_id} ({role})", actor=user)
    return back(request, f"/admin/items/{item_id}")


@router.post("/admin/items/{item_id}/revalidate")
async def item_revalidate(item_id: int, request: Request, user: str = Depends(require_admin)):
    await _revalidate([item_id])
    flash(request, "Playback sources revalidated.")
    return back(request, f"/admin/items/{item_id}")


@router.post("/admin/items/{item_id}/refresh")
async def item_refresh(item_id: int, request: Request, user: str = Depends(require_admin)):
    try:
        ok = await refresh_item(item_id)
        flash(request, "Provider metadata refreshed." if ok else "Provider returned no record (item may be gone).", "ok" if ok else "error")
    except Exception as e:  # noqa: BLE001
        flash(request, f"Refresh failed: {e}", "error")
    return back(request, f"/admin/items/{item_id}")


def _media_files() -> list[str]:
    base = settings.media_dir
    exts = {".mp4", ".mkv", ".ts", ".mov", ".m4v", ".mpg", ".mpeg", ".webm", ".avi"}
    try:
        return sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file() and p.suffix.lower() in exts)[:2000]
    except OSError:
        return []


@router.post("/admin/items/{item_id}/sources")
async def item_add_source(item_id: int, request: Request, user: str = Depends(require_admin)):
    """Attach an independently authorised playback source to any catalogue item
    (e.g. a rights-holder MP4 for a YouTube-discovered programme)."""
    form = await request.form()
    kind = form.get("playback_type")
    label = (form.get("label") or "").strip() or None
    rights_note = (form.get("rights_note") or "").strip() or None
    ps = PlaybackSource(media_item_id=item_id, label=label, rights_note=rights_note, priority=int(form.get("priority") or 50))
    try:
        if kind == PlaybackType.DIRECT_URL:
            url = (form.get("url") or "").strip()
            check_url_syntax(url)
            ps.playback_type, ps.url = PlaybackType.DIRECT_URL, url
        elif kind == PlaybackType.LOCAL_FILE:
            p = safe_local_path(form.get("local_path") or "")
            ps.playback_type, ps.local_path = PlaybackType.LOCAL_FILE, str(p.relative_to(settings.media_dir))
        else:
            raise UnsafeURLError("choose a direct URL or a local file")
    except UnsafeURLError as e:
        flash(request, str(e), "error")
        return RedirectResponse(f"/admin/items/{item_id}", status_code=303)
    await validate_source(ps)
    with session_scope() as s:
        item = s.get(MediaItem, item_id)
        if not item:
            raise HTTPException(404)
        s.add(ps)
        s.flush()
        audit(s, item, "add_source", f"source #{ps.id} {ps.playback_type}: {ps.url or ps.local_path}", actor=user)
        if form.get("authorise") and ps.eligibility in (Eligibility.IPTV_READY, Eligibility.IPTV_NEEDS_TRANSCODE):
            set_rights(s, ps, RightsStatus.AUTHORISED, note=rights_note or "authorised when added", actor=user)
    flash(request, f"Source added: {ps.eligibility}" + (f" — {ps.last_error}" if ps.last_error else ""),
          "ok" if ps.eligibility != Eligibility.UNAVAILABLE else "error")
    return RedirectResponse(f"/admin/items/{item_id}", status_code=303)


@router.post("/admin/sources/{source_id}/rights")
def source_rights(source_id: int, request: Request, rights: str = Form(...), note: str = Form(""), user: str = Depends(require_admin)):
    with session_scope() as s:
        ps = s.get(PlaybackSource, source_id)
        if not ps or rights not in set(RightsStatus):
            raise HTTPException(404)
        try:
            set_rights(s, ps, RightsStatus(rights), note=note or None, actor=user)
            item_id = ps.media_item_id
        except ValueError as e:
            flash(request, str(e), "error")
            return back(request, "/admin/sources")
    flash(request, f"Rights set to {rights}.")
    return back(request, f"/admin/items/{item_id}")


@router.post("/admin/sources/{source_id}/validate")
async def source_validate(source_id: int, request: Request, user: str = Depends(require_admin)):
    with session_scope() as s:
        ps = s.get(PlaybackSource, source_id)
        if not ps:
            raise HTTPException(404)
    r = await validate_source(ps)
    with session_scope() as s:
        s.merge(ps)
    flash(request, f"Validation: {r.eligibility}" + (f" — {r.message}" if r.message else ""), "ok" if r.ok else "error")
    return back(request, "/admin/sources")


@router.post("/admin/sources/{source_id}/update")
def source_update(source_id: int, request: Request, priority: int = Form(100), allow_cache: str = Form(""), user: str = Depends(require_admin)):
    with session_scope() as s:
        ps = s.get(PlaybackSource, source_id)
        if not ps:
            raise HTTPException(404)
        ps.priority = priority
        ps.allow_cache = bool(allow_cache)
    return back(request, "/admin/sources")


@router.post("/admin/sources/{source_id}/delete")
def source_delete(source_id: int, request: Request, user: str = Depends(require_admin)):
    with session_scope() as s:
        ps = s.get(PlaybackSource, source_id)
        if ps:
            audit(s, ps.media_item, "delete_source", f"source #{ps.id} {ps.url or ps.local_path}", actor=user)
            s.delete(ps)
    flash(request, "Playback source removed.")
    return back(request, "/admin/sources")


@router.get("/admin/preview/local/{source_id}")
def preview_local(source_id: int, user: str = Depends(require_admin), s: Session = Depends(get_db)):
    ps = s.get(PlaybackSource, source_id)
    if not ps or ps.playback_type != PlaybackType.LOCAL_FILE:
        raise HTTPException(404)
    try:
        return FileResponse(safe_local_path(ps.local_path or ""))
    except UnsafeURLError:
        raise HTTPException(404)


@router.get("/admin/sources", response_class=HTMLResponse)
def sources_page(request: Request, rights: str | None = None, eligibility: str | None = None, ptype: str | None = None,
                 unhealthy: str | None = None, user: str = Depends(require_admin), s: Session = Depends(get_db)):
    stmt = select(PlaybackSource)
    if rights:
        stmt = stmt.where(PlaybackSource.rights_status == rights)
    if eligibility:
        stmt = stmt.where(PlaybackSource.eligibility == eligibility)
    if ptype:
        stmt = stmt.where(PlaybackSource.playback_type == ptype)
    if unhealthy:
        stmt = stmt.where(PlaybackSource.health_ok.is_(False))
    rows = s.scalars(stmt.order_by(PlaybackSource.media_item_id, PlaybackSource.priority).limit(500)).all()
    channels = s.scalars(select(Channel).order_by(Channel.name)).all()
    return render(request, "sources.html", rows=rows, rights=rights, eligibility=eligibility, ptype=ptype, unhealthy=unhealthy,
                  media_files=_media_files(), channels=channels)


@router.post("/admin/local/import")
async def local_import(request: Request, user: str = Depends(require_admin)):
    """Create catalogue items for files in the media directory (station idents,
    filler, rights-holder files). They still go through review unless the
    administrator approves and authorises them here explicitly."""
    form = await request.form()
    paths = form.getlist("paths")
    approve = bool(form.get("approve"))
    authorise = bool(form.get("authorise"))
    channel_id = int(form["channel_id"]) if form.get("channel_id") else None
    role = form.get("role") or ChannelRole.PROGRAMME
    created = []
    for rel in paths:
        try:
            p = safe_local_path(str(rel))
        except UnsafeURLError:
            continue
        relp = str(p.relative_to(settings.media_dir))
        checksum = await asyncio.to_thread(file_sha1, p)
        with session_scope() as s:
            if s.scalar(select(MediaSource.id).where(MediaSource.provider == ProviderType.LOCAL, MediaSource.external_id == relp)):
                continue
            src = MediaSource(provider=ProviderType.LOCAL, external_id=relp, original_url=None, creator="Local asset", availability_status="available")
            title = re.sub(r"[_.-]+", " ", p.stem).strip().title()
            item = MediaItem(source=src, title=title, title_key=title_key(title), review_status=ReviewStatus.VALIDATING,
                             flags=["Administrator-supplied file — confirm you hold usage rights"])
            ps = PlaybackSource(playback_type=PlaybackType.LOCAL_FILE, local_path=relp, label=p.name, priority=10, checksum=checksum)
            item.playback_sources.append(ps)
            s.add(item)
            s.flush()
            flag_duplicate(s, item)
            created.append(item.id)
    await _revalidate(created)
    with session_scope() as s:
        for iid in created:
            item = s.get(MediaItem, iid)
            item.duration = item.duration or (item.playback_sources[0].duration if item.playback_sources else None)
            if approve:
                set_review_status(s, item, ReviewStatus.APPROVED, actor=user, note="approved on local import")
            else:
                item.review_status = ReviewStatus.PENDING_REVIEW
            if authorise:
                for ps in item.playback_sources:
                    if ps.eligibility in (Eligibility.IPTV_READY, Eligibility.IPTV_NEEDS_TRANSCODE):
                        set_rights(s, ps, RightsStatus.AUTHORISED, note="administrator-supplied file", actor=user)
            if channel_id:
                assign_to_channel(s, item, channel_id, ChannelRole(role))
    flash(request, f"Imported {len(created)} local file(s).")
    return back(request, "/admin/sources")


# ---------------------------------------------------------------- channels

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


@router.get("/admin/channels", response_class=HTMLResponse)
def channels_page(request: Request, user: str = Depends(require_admin), s: Session = Depends(get_db)):
    channels = s.scalars(select(Channel).order_by(Channel.number, Channel.name)).all()
    return render(request, "channels.html", channels=channels)


@router.get("/admin/channels/{channel_id}", response_class=HTMLResponse)
def channel_page(channel_id: int, request: Request, user: str = Depends(require_admin), s: Session = Depends(get_db)):
    ch = s.get(Channel, channel_id)
    if not ch:
        raise HTTPException(404)
    rules = s.scalars(select(DiscoveryRule).order_by(DiscoveryRule.name)).all()
    approved = s.scalars(
        select(MediaItem).where(MediaItem.review_status == ReviewStatus.APPROVED, ~MediaItem.id.in_(select(ChannelItem.media_item_id).where(ChannelItem.channel_id == channel_id))).order_by(MediaItem.title).limit(300)
    ).all()
    return render(request, "channel.html", ch=ch, rules=rules, approved=approved)


def _channel_from_form(ch: Channel, form) -> None:
    ch.name = (form.get("name") or ch.name or "Channel").strip()
    ch.description = form.get("description") or None
    ch.logo_url = (form.get("logo_url") or "").strip() or None
    ch.group_title = (form.get("group_title") or "ArchiveTV").strip()
    ch.number = int(form["number"]) if (form.get("number") or "").isdigit() else None
    ch.enabled = bool(form.get("enabled"))
    ch.mode = form.get("mode") if form.get("mode") in set(ScheduleMode) else ScheduleMode.SEQUENTIAL
    ch.seed = (form.get("seed") or ch.seed or "archivetv").strip()[:64]
    ch.repeat_avoid_count = max(0, int(form.get("repeat_avoid_count") or 0))
    ch.idents_between = bool(form.get("idents_between"))
    ch.filler_pad = bool(form.get("filler_pad"))
    ch.playback_format = form.get("playback_format") if form.get("playback_format") in set(PlaybackFormat) else PlaybackFormat.AUTO
    ch.fallback_item_id = int(form["fallback_item_id"]) if (form.get("fallback_item_id") or "").isdigit() else None
    ch.catchup_enabled = bool(form.get("catchup_enabled"))
    days = int(form["catchup_days"]) if (form.get("catchup_days") or "").isdigit() else ch.catchup_days
    ch.catchup_days = days if days in RETENTION_CHOICES else settings.catchup_days


@router.post("/admin/channels")
async def channel_create(request: Request, user: str = Depends(require_admin)):
    form = await request.form()
    slug = (form.get("slug") or "").strip().lower()
    if not _SLUG.match(slug):
        flash(request, "Channel ID must be lowercase letters, digits and dashes.", "error")
        return RedirectResponse("/admin/channels", status_code=303)
    with session_scope() as s:
        if s.scalar(select(Channel.id).where(Channel.slug == slug)):
            flash(request, "That channel ID already exists.", "error")
            return RedirectResponse("/admin/channels", status_code=303)
        ch = Channel(slug=slug, tvg_id=(form.get("tvg_id") or f"{slug}.archivetv").strip(), name=form.get("name") or slug, seed=secrets.token_hex(4))
        _channel_from_form(ch, {**dict(form), "enabled": "1", "filler_pad": "1", "catchup_enabled": "1",
                                "catchup_days": str(settings.catchup_days),
                                "repeat_avoid_count": form.get("repeat_avoid_count") or "10"})
        s.add(ch)
        s.flush()
        cid = ch.id
    flash(request, f"Channel '{slug}' created. Assign approved content, then it will appear in the M3U.")
    return RedirectResponse(f"/admin/channels/{cid}", status_code=303)


@router.post("/admin/channels/{channel_id}")
async def channel_update(channel_id: int, request: Request, user: str = Depends(require_admin)):
    form = await request.form()
    with session_scope() as s:
        ch = s.get(Channel, channel_id)
        if not ch:
            raise HTTPException(404)
        _channel_from_form(ch, form)
        tvg = (form.get("tvg_id") or "").strip()
        if tvg and tvg != ch.tvg_id and not s.scalar(select(Channel.id).where(Channel.tvg_id == tvg)):
            ch.tvg_id = tvg
        rule_ids = [int(x) for x in form.getlist("rule_ids")]
        ch.rules = list(s.scalars(select(DiscoveryRule).where(DiscoveryRule.id.in_(rule_ids))).all()) if rule_ids else []
    flash(request, "Channel saved. Upcoming schedule is updated as content changes; use Regenerate to rebuild it now.")
    return RedirectResponse(f"/admin/channels/{channel_id}", status_code=303)


@router.post("/admin/channels/{channel_id}/logo")
async def channel_logo(channel_id: int, request: Request, user: str = Depends(require_admin)):
    form = await request.form()
    upload = form.get("logo")
    with session_scope() as s:
        ch = s.get(Channel, channel_id)
        if not ch:
            raise HTTPException(404)
        if form.get("remove"):
            logos.remove(ch.logo_file)
            ch.logo_file = None
            flash(request, "Uploaded logo removed.")
        elif upload is None or isinstance(upload, str):
            flash(request, "Choose an image to upload.", "error")
        else:
            try:
                ch.logo_file = logos.save(ch.slug, await upload.read(logos.MAX_BYTES + 1), ch.logo_file)
                flash(request, "Logo uploaded. Refresh the account in Dispatcharr to pick it up.")
            except logos.LogoError as e:
                flash(request, str(e), "error")
    return RedirectResponse(f"/admin/channels/{channel_id}", status_code=303)


@router.post("/admin/channels/{channel_id}/items")
async def channel_items(channel_id: int, request: Request, user: str = Depends(require_admin)):
    form = await request.form()
    with session_scope() as s:
        ch = s.get(Channel, channel_id)
        if not ch:
            raise HTTPException(404)
        add_ids = [int(x) for x in form.getlist("add_ids")]
        add_role = form.get("add_role") or ChannelRole.PROGRAMME
        for iid in add_ids:
            item = s.get(MediaItem, iid)
            if item:
                assign_to_channel(s, item, channel_id, ChannelRole(add_role))
        for ci in list(ch.items):
            if form.get(f"remove_{ci.id}"):
                s.delete(ci)
                continue
            pos = form.get(f"pos_{ci.id}")
            if pos is not None and pos.lstrip("-").isdigit():
                ci.position = int(pos)
            role = form.get(f"role_{ci.id}")
            if role in set(ChannelRole):
                ci.role = role
    return RedirectResponse(f"/admin/channels/{channel_id}", status_code=303)


@router.post("/admin/channels/{channel_id}/blocks")
def channel_block_add(channel_id: int, request: Request, name: str = Form(...), start_time: str = Form(...), end_time: str = Form(...),
                      tag_filter: str = Form(""), mode: str = Form(ScheduleMode.SHUFFLE), days: list[str] = Form(["0", "1", "2", "3", "4", "5", "6"]),
                      user: str = Depends(require_admin)):
    if not re.match(r"^\d{2}:\d{2}$", start_time) or not re.match(r"^\d{2}:\d{2}$", end_time):
        flash(request, "Times must be HH:MM.", "error")
        return RedirectResponse(f"/admin/channels/{channel_id}", status_code=303)
    with session_scope() as s:
        s.add(ChannelBlock(channel_id=channel_id, name=name, start_time=start_time, end_time=end_time, tag_filter=tag_filter or None,
                           mode=mode if mode in (ScheduleMode.SEQUENTIAL, ScheduleMode.SHUFFLE) else ScheduleMode.SHUFFLE,
                           days="".join(sorted(d for d in days if d in "0123456"))))
    flash(request, "Block added. It applies to newly generated schedule entries; Regenerate to apply it now.")
    return RedirectResponse(f"/admin/channels/{channel_id}", status_code=303)


@router.post("/admin/blocks/{block_id}/delete")
def channel_block_delete(block_id: int, request: Request, user: str = Depends(require_admin)):
    with session_scope() as s:
        b = s.get(ChannelBlock, block_id)
        cid = b.channel_id if b else None
        if b:
            s.delete(b)
    return RedirectResponse(f"/admin/channels/{cid}" if cid else "/admin/channels", status_code=303)


@router.post("/admin/channels/{channel_id}/regenerate")
def channel_regenerate(channel_id: int, request: Request, user: str = Depends(require_admin)):
    with session_scope() as s:
        ch = s.get(Channel, channel_id)
        if not ch:
            raise HTTPException(404)
        n = regenerate(s, ch, actor=user)
    flash(request, f"Schedule regenerated ({n} entries). The programme on air now was kept.")
    return back(request, f"/admin/schedule?channel={channel_id}")


@router.post("/admin/channels/{channel_id}/delete")
async def channel_delete(channel_id: int, request: Request, confirm: str = Form(""), user: str = Depends(require_admin)):
    with session_scope() as s:
        ch = s.get(Channel, channel_id)
        if not ch or confirm != ch.slug:
            flash(request, "Type the channel ID to confirm deletion.", "error")
            return RedirectResponse(f"/admin/channels/{channel_id}", status_code=303)
    st = manager.streams.get(channel_id)
    if st:
        await st.stop()
    with session_scope() as s:
        s.delete(s.get(Channel, channel_id))
    flash(request, "Channel deleted. Catalogue items are untouched.")
    return RedirectResponse("/admin/channels", status_code=303)


# ---------------------------------------------------------------- schedule


@router.get("/admin/schedule", response_class=HTMLResponse)
def schedule_page(request: Request, channel: int | None = None, day: str | None = None, user: str = Depends(require_admin),
                  s: Session = Depends(get_db)):
    channels = s.scalars(select(Channel).order_by(Channel.number, Channel.name)).all()
    ch = s.get(Channel, channel) if channel else (channels[0] if channels else None)
    tz = settings.timezone
    today = datetime.now(tz).date()
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date() if day else today
    except ValueError:
        d = today
    start_local = datetime.combine(d, datetime.min.time(), tzinfo=tz)
    start = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end = (start_local + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    entries, problems, cur, eligible, versions = [], [], None, [], []
    if ch:
        versions = s.scalars(select(ScheduleVersion).where(ScheduleVersion.channel_id == ch.id)
                             .order_by(ScheduleVersion.id.desc()).limit(15)).all()
        entries = s.scalars(select(ScheduleEntry).where(ScheduleEntry.channel_id == ch.id, ScheduleEntry.end > start, ScheduleEntry.start < end).order_by(ScheduleEntry.start)).all()
        problems = check_continuity(list(entries))
        cur = current_entry(s, ch.id)
        eligible = [ci.media_item for ci in ch.items if ci.role == ChannelRole.PROGRAMME and ci.media_item and is_schedulable(ci.media_item)]
    back_days = history_days(ch) if ch else 1
    days = [today + timedelta(days=i) for i in range(-back_days, settings.schedule_days + 1)]
    return render(request, "schedule.html", channels=channels, ch=ch, entries=entries, problems=problems, cur=cur, day=d, days=days,
                  eligible=eligible, now=utcnow(), tok=f"&token={settings.feed_token}" if settings.feed_token else "",
                  versions=versions)


@router.post("/admin/schedule/{entry_id}/override")
def schedule_override(entry_id: int, request: Request, item_id: int = Form(...), user: str = Depends(require_admin)):
    with session_scope() as s:
        e = s.get(ScheduleEntry, entry_id)
        if not e:
            raise HTTPException(404)
        ch = s.get(Channel, e.channel_id)
        try:
            new = override(s, ch, entry_id, item_id, actor=user)
            audit(s, new.media_item, "schedule_override", f"{ch.slug} at {new.start.isoformat()}Z", actor=user)
            flash(request, "Programme overridden; later entries were rebuilt.")
        except ValueError as ex:
            flash(request, str(ex), "error")
    return back(request, "/admin/schedule")


@router.post("/admin/schedule/maintain")
def schedule_maintain(request: Request, user: str = Depends(require_admin)):
    with session_scope() as s:
        for ch in s.scalars(select(Channel).where(Channel.enabled.is_(True))).all():
            maintain(s, ch)
    flash(request, "Schedules checked and extended.")
    return back(request, "/admin/schedule")


# ---------------------------------------------------------------- settings


@router.get("/admin/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: str = Depends(require_admin), s: Session = Depends(get_db)):
    base = settings.public_base_url or str(request.base_url).rstrip("/")
    tok = f"?token={settings.feed_token}" if settings.feed_token else ""
    backups = sorted((settings.data_dir / "backups").glob("*.sqlite3"), reverse=True)[:10]
    values = {k: getattr(settings, k) for k in appsettings.TUNABLES}
    statuses = {p.provider: p for p in s.scalars(select(ProviderStatus)).all()}
    return render(request, "settings.html", base=base, tok=tok, tunables=appsettings.TUNABLES, values=values, backups=backups,
                  yt_configured=bool(settings.youtube_api_key), feed_token_set=bool(settings.feed_token), statuses=statuses,
                  media_dir=settings.media_dir, allowed_hosts=["archive.org"] + settings.extra_media_hosts,
                  xc_user=settings.xc_username, xc_password_env=bool(os.environ.get("ATV_XC_PASSWORD") or os.environ.get("ATV_XC_PASSWORD_FILE")),
                  minimal_disk=settings.minimal_disk)


@router.post("/admin/settings")
async def settings_save(request: Request, user: str = Depends(require_admin)):
    form = await request.form()
    errors = appsettings.save({k: v for k, v in form.items() if isinstance(v, str)})
    flash(request, "; ".join(errors) if errors else "Settings saved.", "error" if errors else "ok")
    return RedirectResponse("/admin/settings", status_code=303)


@router.post("/admin/settings/backup")
def settings_backup(request: Request, user: str = Depends(require_admin)):
    p = backup_database()
    flash(request, f"Backup written: {p.name}")
    return RedirectResponse("/admin/settings", status_code=303)


# ---------------------------------------------------------------- catch-up

@router.get("/admin/catchup", response_class=HTMLResponse)
def catchup_page(request: Request, channel: int | None = None, user: str = Depends(require_admin), s: Session = Depends(get_db)):
    channels = s.scalars(select(Channel).order_by(Channel.number, Channel.name)).all()
    ch = s.get(Channel, channel) if channel else (channels[0] if channels else None)
    now = utcnow()
    progs = []
    if ch:
        since = now - timedelta(days=history_days(ch))
        rows = s.scalars(select(ScheduleEntry).where(ScheduleEntry.channel_id == ch.id, ScheduleEntry.end <= now,
                                                      ScheduleEntry.end > since, ScheduleEntry.kind != "slate")
                         .order_by(ScheduleEntry.start.desc()).limit(150)).all()
        for e in rows:
            try:
                catchup.resolve_request(s, ch, max(e.start, since), now=now)
                progs.append((e, None))
            except catchup.CatchupError as err:
                progs.append((e, err.message))
    sessions = s.scalars(select(CatchupSession).order_by(CatchupSession.started_at.desc()).limit(25)).all()
    return render(request, "catchup.html", channels=channels, ch=ch, progs=progs, active=manager.catchup_status(),
                  sessions=sessions, tok=f"&token={settings.feed_token}" if settings.feed_token else "",
                  base=settings.public_base_url or str(request.base_url).rstrip("/"))


@router.post("/admin/catchup/test")
async def catchup_test(request: Request, user: str = Depends(require_admin)):
    """Resolve a historical time and check its source answers, without streaming."""
    form = await request.form()
    cid = int(form.get("channel_id") or 0)
    try:
        with session_scope() as s:
            ch = s.get(Channel, cid)
            if ch is None:
                raise catchup.CatchupError(404, "unknown channel")
            start = catchup.parse_timestamp(str(form.get("at") or ""), settings.timezone)
            req = catchup.resolve_request(s, ch, start)
        await catchup.preflight(req)
        flash(request, f"OK: {local(req.start, '%a %d %b %H:%M:%S')} is {fmt_duration(req.offset)} into '{req.title}' "
                       f"(aired {local(req.programme_start, '%H:%M')}); its source answers and can seek there.")
    except catchup.CatchupError as e:
        flash(request, f"Not playable: {e.message}", "error")
    return RedirectResponse(f"/admin/catchup?channel={cid}", status_code=303)


@router.post("/admin/catchup/{session_id}/stop")
async def catchup_stop(session_id: str, request: Request, user: str = Depends(require_admin)):
    ok = await catchup.stop_session(manager, session_id)
    flash(request, "Session stopped." if ok else "That session has already ended.", "ok" if ok else "error")
    return back(request, "/admin/catchup")


@router.get("/admin/api/status")
def api_status(user: str = Depends(require_admin)):
    return JSONResponse({"streams": manager.status(), "catchup": manager.catchup_status(), "jobs": jobs.last_run})


@router.post("/admin/review/validate-pending")
async def validate_pending(request: Request, user: str = Depends(require_admin)):
    with session_scope() as s:
        ids = list(s.scalars(select(MediaItem.id).where(MediaItem.review_status.in_([ReviewStatus.DISCOVERED, ReviewStatus.VALIDATING]))).all())
    await validate_items(ids)
    flash(request, f"Validated {len(ids)} item(s).")
    return back(request, "/admin/review")
