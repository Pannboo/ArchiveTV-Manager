"""Background jobs: discovery scans, schedule upkeep, health checks,
YouTube metadata refresh, cache prefetch, resource monitoring, database
maintenance and backups."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete, or_, select, text

from . import cache, monitor
from .catalogue import IPTV_TYPES
from .config import settings
from .db import backup_database, engine, session_scope, utcnow
from .discovery import due_rules, notify, refresh_stale, run_rule
from .enums import Eligibility, EntryKind, RightsStatus
from .models import (
    AuditLog, CatchupSession, Channel, ChannelItem, PlaybackEvent, PlaybackSource, ScheduleEntry, ScheduleVersion, StreamSession,
)
from .scheduler import maintain, regenerate
from .validator import health_check, validate_source

log = logging.getLogger(__name__)
last_run: dict[str, str] = {}


async def discovery_tick() -> None:
    for rid in due_rules():
        try:
            res = await run_rule(rid)
            log.info("discovery rule %s: found=%s new=%s errors=%s", rid, res.found, res.new, res.errors)
        except Exception:
            log.exception("discovery rule %s failed", rid)


def schedule_tick() -> None:
    with session_scope() as s:
        for ch in s.scalars(select(Channel).where(Channel.enabled.is_(True))).all():
            r = maintain(s, ch)
            if r["rebuilt_from"] or r["added"]:
                log.info("schedule %s: rebuilt_from=%s added=%s", ch.slug, r["rebuilt_from"], r["added"])


async def health_tick(batch: int = 40) -> None:
    """Re-check authorised IPTV sources: full probe when unverified, cheap
    availability check otherwise. Sources airing soon are checked first."""
    now = utcnow()
    stale = now - timedelta(hours=6)
    with session_scope() as s:
        soon = set(
            s.scalars(
                select(ScheduleEntry.playback_source_id).where(
                    ScheduleEntry.start < now + timedelta(hours=12), ScheduleEntry.end > now, ScheduleEntry.playback_source_id.is_not(None)
                )
            ).all()
        )
        rows = s.scalars(
            select(PlaybackSource).where(
                PlaybackSource.rights_status == RightsStatus.AUTHORISED,
                PlaybackSource.playback_type.in_(list(IPTV_TYPES)),
                or_(PlaybackSource.last_validated_at.is_(None), PlaybackSource.last_validated_at < stale, PlaybackSource.health_ok.is_(False)),
            )
        ).all()
        rows = sorted(rows, key=lambda p: (p.id not in soon, p.last_validated_at or now - timedelta(days=999)))[:batch]
    for ps in rows:
        if ps.eligibility in (Eligibility.UNVERIFIED, Eligibility.UNAVAILABLE) or not ps.health_ok:
            await validate_source(ps)
        else:
            await health_check(ps)
        with session_scope() as s:
            s.merge(ps)


async def live_tick() -> None:
    """Re-check authorised live sources that are offline, every minute, and
    reschedule their channels as soon as one comes back on air."""
    with session_scope() as s:
        rows = s.scalars(select(PlaybackSource).where(
            PlaybackSource.live.is_(True), PlaybackSource.health_ok.is_(False),
            PlaybackSource.rights_status == RightsStatus.AUTHORISED)).all()
    for ps in rows:
        if ps.eligibility in (Eligibility.UNVERIFIED, Eligibility.UNAVAILABLE):
            await validate_source(ps)
        else:
            await health_check(ps)
        with session_scope() as s:
            s.merge(ps)
            if ps.health_ok:
                log.info("live source #%s is back on air", ps.id)
                chans = s.scalars(select(Channel).join(ChannelItem).where(
                    ChannelItem.media_item_id == ps.media_item_id, Channel.enabled.is_(True))).all()
                for ch in chans:
                    regenerate(s, ch, actor="live")


async def youtube_refresh_tick() -> None:
    if settings.youtube_api_key:
        n = await refresh_stale("youtube", settings.youtube_refresh_days)
        if n:
            log.info("refreshed %s YouTube records", n)


async def cache_tick() -> None:
    if not settings.cache_active:
        return
    from .stream import manager

    now = utcnow()
    urls = []
    with session_scope() as s:
        for st in manager.status():
            nxt = s.scalars(
                select(ScheduleEntry)
                .where(ScheduleEntry.channel_id == st["channel_id"], ScheduleEntry.start > now, ScheduleEntry.kind != EntryKind.SLATE)
                .order_by(ScheduleEntry.start)
                .limit(2)
            ).all()
            for e in nxt:
                ps = e.playback_source
                if ps and ps.url and ps.allow_cache and ps.playback_type == "direct_url":
                    urls.append(ps.url)
    for u in urls:
        asyncio.create_task(cache.prefetch(u))


async def monitor_tick() -> None:
    from .stream import manager

    snap = monitor.snapshot(manager)
    for a in monitor.new_alerts(snap["alerts"]):
        log.warning("resource alert: %s", a)
        await notify(f"ArchiveTV resource alert: {a}")


def maintenance_tick() -> None:
    """Keep the database bounded: expire operational history, then checkpoint
    the WAL and let SQLite refresh its statistics."""
    cutoff = utcnow() - timedelta(days=settings.history_keep_days)
    with session_scope() as s:
        for model, col in ((PlaybackEvent, PlaybackEvent.at), (CatchupSession, CatchupSession.started_at),
                           (StreamSession, StreamSession.started_at), (AuditLog, AuditLog.at),
                           (ScheduleVersion, ScheduleVersion.created_at)):
            s.execute(delete(model).where(col < cutoff))
    with engine().connect() as c:
        c.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        c.execute(text("PRAGMA optimize"))
        if settings.db_path.exists() and settings.db_path.stat().st_size > settings.db_max_mb * 2**20 * 0.8:
            c.execute(text("VACUUM"))


def backup_tick() -> None:
    p = backup_database()
    log.info("database backup written to %s", p)


def _wrap(name: str, fn: Callable[[], Awaitable[None] | None]) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        try:
            r = fn()
            if asyncio.iscoroutine(r):
                await r
            last_run[name] = utcnow().isoformat() + "Z"
        except Exception:
            log.exception("job %s failed", name)

    return run


# (name, interval seconds, function, delay before first run)
JOB_SPECS: list[tuple[str, int, Callable[[], Awaitable[None] | None], int]] = [
    # Runs in a worker thread so long rebuilds never stall live streams.
    ("schedule", 300, lambda: asyncio.to_thread(schedule_tick), 1),
    ("discovery", 60, discovery_tick, 20),
    ("health", 900, health_tick, 60),
    ("live", 60, live_tick, 45),
    ("youtube_refresh", 6 * 3600, youtube_refresh_tick, 300),
    ("cache", 120, cache_tick, 30),
    ("monitor", 60, monitor_tick, 15),
    ("maintenance", 24 * 3600, lambda: asyncio.to_thread(maintenance_tick), 900),
    ("backup", 24 * 3600, backup_tick, 600),
]
scheduler: AsyncIOScheduler | None = None


def start_jobs() -> AsyncIOScheduler:
    """Start APScheduler on the running event loop. Each job runs at most once
    at a time; runs missed while the process was busy are coalesced."""
    global scheduler
    scheduler = AsyncIOScheduler(timezone=timezone.utc, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300})
    now = datetime.now(timezone.utc)
    for name, interval, fn, delay in JOB_SPECS:
        scheduler.add_job(_wrap(name, fn), IntervalTrigger(seconds=interval), id=name, name=name,
                          next_run_time=now + timedelta(seconds=delay))
    scheduler.start()
    return scheduler


def stop_jobs() -> None:
    global scheduler
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
    scheduler = None


def job_status() -> dict[str, dict]:
    nxt = {j.id: j.next_run_time for j in scheduler.get_jobs()} if scheduler else {}
    return {name: {"last_run": last_run.get(name), "next_run": nxt[name].isoformat() if nxt.get(name) else None}
            for name, *_ in JOB_SPECS}
