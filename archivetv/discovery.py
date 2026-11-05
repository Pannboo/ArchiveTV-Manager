"""Discovery engine: run provider searches, normalise into the catalogue,
detect duplicates, validate technically, and queue for manual review.

Nothing here ever approves content or authorises rights.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select

from .catalogue import IPTV_TYPES, is_known, title_key, upsert_record
from .config import settings
from .db import session_scope, utcnow
from .enums import Eligibility, PlaybackType, ReviewStatus
from .models import DiscoveryRule, MediaItem, MediaSource, PlaybackSource
from .providers import MediaRecord, ProviderError, QuotaExhausted, get_provider
from .quota import record_error, record_success
from .validator import validate_source

log = logging.getLogger(__name__)


@dataclass
class DiscoveryResult:
    found: int = 0
    new: int = 0
    skipped_known: int = 0
    filtered: int = 0
    errors: list[str] = field(default_factory=list)
    new_item_ids: list[int] = field(default_factory=list)


def _csv(v: Any) -> list[str]:
    if not v:
        return []
    if isinstance(v, list):
        return [str(x).strip().lower() for x in v if str(x).strip()]
    return [x.strip().lower() for x in str(v).split(",") if x.strip()]


def apply_preferences(rec: MediaRecord, params: dict[str, Any]) -> None:
    """Re-rank a record's IPTV-capable files by the rule's preferred formats
    (ordered keywords matched against format/label/container) and preferred
    resolution (closest height first). Validation and playback both pick the
    lowest priority, so this decides which file is probed and aired."""
    formats = _csv(params.get("preferred_formats"))
    try:
        target = int(params.get("preferred_height") or 0)
    except (TypeError, ValueError):
        target = 0
    if not formats and not target:
        return
    for opt in rec.playback:
        if not opt.iptv_capable:
            continue
        text = f"{opt.label or ''} {opt.container or ''}".lower()
        rank = next((i for i, f in enumerate(formats) if f in text), None)
        prio = opt.priority
        if formats:
            # Rank steps (100) outweigh provider base priorities (< 100).
            prio += -1000 + 100 * rank if rank is not None else 1000
        if target and opt.height:
            prio += min(abs(opt.height - target) // 60, 30)
        opt.priority = prio


def passes_filters(rec: MediaRecord, params: dict[str, Any]) -> tuple[bool, str | None]:
    """Provider-agnostic post-filters applied to every discovered record."""
    p = params or {}
    if p.get("min_duration") and rec.duration and rec.duration < float(p["min_duration"]):
        return False, "shorter than minimum duration"
    if p.get("max_duration") and rec.duration and rec.duration > float(p["max_duration"]):
        return False, "longer than maximum duration"
    text = f"{rec.title}\n{rec.description or ''}".lower()
    for kw in _csv(p.get("excluded_keywords")):
        if kw in text:
            return False, f"excluded keyword '{kw}'"
    creators = {x for x in (rec.creator, rec.creator_id) if x}
    excluded = set(_csv(p.get("excluded_channels")) + _csv(p.get("excluded_creators")))
    if excluded and {c.lower() for c in creators} & excluded:
        return False, "excluded channel/creator"
    if rec.publication_date:
        d = rec.publication_date[:10]
        if p.get("published_after") and len(d) >= 4 and d < str(p["published_after"])[: len(d)]:
            return False, "published before range"
        if p.get("published_before") and len(d) >= 4 and d > str(p["published_before"])[: len(d)]:
            return False, "published after range"
    wanted = _csv(p.get("categories"))
    if wanted and rec.provider == "youtube":
        have = {c.lower() for c in rec.categories} | {str(rec.raw.get("category_id", "")).lower()}
        if not have & set(wanted):
            return False, "category not wanted"
    return True, None


async def run_query(
    provider_name: str,
    kind: str,
    query: str | None,
    params: dict[str, Any] | None = None,
    max_results: int = 50,
    rule_id: int | None = None,
    interactive: bool = False,
) -> DiscoveryResult:
    provider = get_provider(provider_name)
    res = DiscoveryResult()
    params = params or {}
    if hasattr(provider, "use_reserve"):
        provider.use_reserve = interactive
    try:
        stubs = await provider.search(query, params, kind, max_results)
        res.found = len(stubs)
        with session_scope() as s:
            fresh = [st for st in stubs if not is_known(s, st.provider, st.external_id)]
        res.skipped_known = len(stubs) - len(fresh)
        records = await provider.expand(fresh) if fresh else []
        record_success(provider_name)
    except QuotaExhausted as e:
        record_error(provider_name, str(e))
        res.errors.append(str(e))
        return res
    except (ProviderError, httpx.HTTPError) as e:
        record_error(provider_name, str(e))
        res.errors.append(str(e))
        return res

    to_validate: list[int] = []
    with session_scope() as s:
        rule = s.get(DiscoveryRule, rule_id) if rule_id else None
        for rec in records:
            ok, why = passes_filters(rec, params)
            if not ok:
                res.filtered += 1
                continue
            apply_preferences(rec, params)
            item, created = upsert_record(s, rec, rule)
            if created:
                res.new += 1
                s.flush()
                res.new_item_ids.append(item.id)
                if item.review_status == ReviewStatus.DISCOVERED:
                    if params.get("skip_validation"):
                        # Unprobed sources stay UNVERIFIED, so they cannot be
                        # scheduled until validated from the item page.
                        item.review_status = ReviewStatus.PENDING_REVIEW
                    else:
                        item.review_status = ReviewStatus.VALIDATING
                        to_validate.append(item.id)
    await validate_items(to_validate)
    if res.new:
        await notify(f"ArchiveTV: {res.new} new {provider.label} item(s) awaiting review")
    return res


async def import_youtube_live(url: str) -> DiscoveryResult:
    """Catalogue a YouTube channel's live address (``…/@handle/live``) as a
    live item. It follows whatever the channel is broadcasting now, so a
    recurring stream with a new video id each time keeps working. Experimental
    (ATV_YOUTUBE_STREAMING); it still needs approval and authorised rights."""
    from . import ytstream

    res = DiscoveryResult()
    live_url = ytstream.channel_live_url(url)
    if not live_url:
        res.errors.append("not a YouTube channel /live address")
        return res
    if not settings.youtube_streaming:
        res.errors.append("experimental YouTube streaming is switched off (ATV_YOUTUBE_STREAMING)")
        return res
    who = live_url.split("youtube.com/", 1)[1].rsplit("/live", 1)[0]
    ext = f"live:{who}"
    res.found = 1
    with session_scope() as s:
        if is_known(s, "youtube", ext):
            res.skipped_known = 1
            return res
        name = who if who.startswith("@") else who.split("/", 1)[-1]
        item = MediaItem(
            source=MediaSource(provider="youtube", external_id=ext, original_url=live_url, creator=name,
                               availability_status="available"),
            title=f"{name} — YouTube live", title_key=title_key(f"{name} youtube live"), review_status=ReviewStatus.VALIDATING,
            tags=["live"], artwork_in_epg=False,
            flags=["Live stream from a YouTube channel's /live address: airs whenever the channel is live",
                   "Experimental YouTube streaming: only authorise rights for streams you may rebroadcast"],
        )
        item.playback_sources.append(PlaybackSource(
            playback_type=PlaybackType.YOUTUBE_STREAM, url=live_url, label="YouTube live (channel, experimental)",
            live=True, priority=500, eligibility=Eligibility.UNVERIFIED))
        s.add(item)
        s.flush()
        res.new = 1
        res.new_item_ids.append(item.id)
    await validate_items(res.new_item_ids)
    return res


async def validate_items(item_ids: list[int], concurrency: int = 3) -> None:
    """Probe each item's preferred IPTV-capable source, then queue it for review."""
    sem = asyncio.Semaphore(concurrency)

    async def one(item_id: int) -> None:
        async with sem:
            with session_scope() as s:
                item = s.get(MediaItem, item_id)
                if item is None:
                    return
                candidates = [p for p in item.playback_sources if p.playback_type in IPTV_TYPES and p.eligibility != "UNAVAILABLE"]
                ps = min(candidates, key=lambda p: p.priority) if candidates else None
                for p in item.playback_sources:
                    if p.playback_type not in IPTV_TYPES:
                        await validate_source(p)
            # Probe outside the write transaction.
            if ps is not None:
                await validate_source(ps)
                with session_scope() as s:
                    s.merge(ps)
            with session_scope() as s:
                item = s.get(MediaItem, item_id)
                if item and item.review_status in (ReviewStatus.DISCOVERED, ReviewStatus.VALIDATING):
                    item.review_status = ReviewStatus.PENDING_REVIEW

    await asyncio.gather(*(one(i) for i in item_ids))


async def run_rule(rule_id: int, interactive: bool = False) -> DiscoveryResult:
    with session_scope() as s:
        rule = s.get(DiscoveryRule, rule_id)
        if rule is None:
            raise ValueError("rule not found")
        provider, kind, query, params, maxr = rule.provider, rule.kind, rule.query, dict(rule.params or {}), rule.max_results
        rule.last_status = "running"
    res = await run_query(provider, kind, query, params, maxr, rule_id=rule_id, interactive=interactive)
    with session_scope() as s:
        rule = s.get(DiscoveryRule, rule_id)
        if rule:
            rule.last_run_at = utcnow()
            rule.last_new_count = res.new
            rule.last_status = "error" if res.errors else "ok"
            rule.last_error = "; ".join(res.errors)[:2000] or None
    return res


def due_rules() -> list[int]:
    now = utcnow()
    with session_scope() as s:
        rules = s.scalars(select(DiscoveryRule).where(DiscoveryRule.enabled.is_(True))).all()
        return [
            r.id
            for r in rules
            if r.last_run_at is None or r.last_run_at + timedelta(minutes=max(5, r.scan_interval_minutes)) <= now
        ]


async def refresh_stale(provider_name: str, older_than_days: int, limit: int = 200) -> int:
    """Re-fetch provider metadata for items not checked recently (e.g. YouTube's
    requirement to keep stored API data fresh)."""
    cutoff = utcnow() - timedelta(days=older_than_days)
    with session_scope() as s:
        rows = s.scalars(
            select(MediaSource)
            .where(MediaSource.provider == provider_name, (MediaSource.last_checked_at.is_(None)) | (MediaSource.last_checked_at < cutoff))
            .limit(limit)
        ).all()
        stubs = [MediaRecord(provider=provider_name, external_id=r.external_id, title=r.external_id) for r in rows]
    if not stubs:
        return 0
    provider = get_provider(provider_name)
    try:
        records = await provider.expand(stubs)
        record_success(provider_name)
    except (ProviderError, httpx.HTTPError) as e:
        record_error(provider_name, str(e))
        return 0
    with session_scope() as s:
        for rec in records:
            upsert_record(s, rec)
    return len(records)


async def refresh_item(item_id: int) -> bool:
    with session_scope() as s:
        item = s.get(MediaItem, item_id)
        if not item:
            return False
        provider_name, ext = item.source.provider, item.source.external_id
    if provider_name not in ("archive", "youtube"):
        return False
    provider = get_provider(provider_name)
    if hasattr(provider, "use_reserve"):
        provider.use_reserve = True
    rec = await provider.get_metadata(ext)
    if rec is None:
        return False
    with session_scope() as s:
        upsert_record(s, rec)
    return True


async def notify(message: str) -> None:
    url = settings.notify_webhook_url
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            # Works with ntfy (plain body), Discord/Slack-style JSON receivers
            # accept {"content"}/{"text"}.
            await c.post(url, json={"text": message, "content": message})
    except httpx.HTTPError as e:
        log.warning("notification webhook failed: %s", e)
