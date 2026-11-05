"""Unified catalogue operations: normalisation, duplicates, review and eligibility."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import settings
from .db import utcnow
from .enums import IPTV_ELIGIBLE, ChannelRole, Eligibility, PlaybackType, ReviewStatus, RightsStatus
from .models import AuditLog, ChannelItem, DiscoveryRule, MediaItem, MediaSource, PlaybackSource
from .providers.base import MediaRecord
from .security import is_youtube_url

IPTV_TYPES = {PlaybackType.DIRECT_URL, PlaybackType.LOCAL_FILE, PlaybackType.YOUTUBE_STREAM}


def youtube_stream_allowed(ps: PlaybackSource) -> bool:
    """The experimental YouTube stream type only counts while it is switched on."""
    return ps.playback_type != PlaybackType.YOUTUBE_STREAM or bool(settings.youtube_streaming)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_NOISE = re.compile(r"\b(full (episode|movie)|hd|sd|1080p|720p|480p|official|remastered|restored)\b")


def title_key(title: str) -> str:
    t = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    t = _NOISE.sub(" ", t)
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", t)
    return _NON_ALNUM.sub(" ", t).strip()


# ---------------------------------------------------------------- eligibility


def source_is_iptv_ok(ps: PlaybackSource) -> bool:
    """A playback source may feed a published IPTV schedule."""
    return (
        ps.playback_type in IPTV_TYPES
        and ps.eligibility in IPTV_ELIGIBLE
        and ps.rights_status == RightsStatus.AUTHORISED
        and ps.health_ok
        and youtube_stream_allowed(ps)
        and not (ps.url and is_youtube_url(ps.url) and ps.playback_type != PlaybackType.YOUTUBE_STREAM)
    )


def best_iptv_source(item: MediaItem) -> PlaybackSource | None:
    for ps in sorted(item.playback_sources, key=lambda p: (p.priority, p.id or 0)):
        if source_is_iptv_ok(ps):
            return ps
    return None


LIVE_SLOT_SECONDS = 3600.0  # a live source outside any block is scheduled in hourly slots


def programme_duration(item: MediaItem, ps: PlaybackSource | None) -> float | None:
    if ps is not None and ps.live:
        return LIVE_SLOT_SECONDS  # nominal: the scheduler cuts live slots at block boundaries
    d = (ps.duration if ps and ps.duration else None) or item.duration
    return float(d) if d and d > 0 else None


def is_schedulable(item: MediaItem) -> bool:
    """Approved + authorised, compatible, healthy playback source + known duration."""
    if item.review_status != ReviewStatus.APPROVED:
        return False
    ps = best_iptv_source(item)
    return ps is not None and programme_duration(item, ps) is not None


def iptv_status(item: MediaItem) -> str:
    """Human-readable reason an item is or isn't IPTV-eligible."""
    if item.review_status != ReviewStatus.APPROVED:
        return "not approved"
    iptv = [p for p in item.playback_sources if p.playback_type in IPTV_TYPES]
    if not iptv:
        if item.provider == "youtube":
            return "no authorised IPTV source (YouTube is discovery only)"
        return "no IPTV-capable source"
    if not any(p.eligibility in IPTV_ELIGIBLE for p in iptv):
        return "no technically compatible source (validate)"
    if not any(p.eligibility in IPTV_ELIGIBLE and p.rights_status == RightsStatus.AUTHORISED for p in iptv):
        return "rights not authorised"
    if not best_iptv_source(item):
        return "failing health checks"
    if not is_schedulable(item):
        return "unknown duration"
    return "eligible"


def item_eligibility(item: MediaItem) -> Eligibility:
    """Best technical classification across an item's sources (for filtering)."""
    order = [
        Eligibility.IPTV_READY,
        Eligibility.IPTV_NEEDS_TRANSCODE,
        Eligibility.EMBED_ONLY,
        Eligibility.REFERENCE_ONLY,
        Eligibility.UNVERIFIED,
        Eligibility.UNAVAILABLE,
    ]
    present = {p.eligibility for p in item.playback_sources}
    return next((e for e in order if e in present), Eligibility.REFERENCE_ONLY)


# ------------------------------------------------------------------- upserts


def is_known(s: Session, provider: str, external_id: str) -> bool:
    return (
        s.scalar(
            select(MediaSource.id).where(
                MediaSource.provider == provider,
                or_(MediaSource.external_id == external_id, MediaSource.parent_external_id == external_id),
            ).limit(1)
        )
        is not None
    )


def find_duplicate(s: Session, item: MediaItem) -> tuple[MediaItem, str] | None:
    """Possible duplicate of another catalogue item, with the reason.

    Checked strongest first: the same file (checksum or URL) on another item,
    then the same normalised title with compatible episode numbers and
    duration. Duplicates are only flagged for review, never removed.
    (Exact provider ids are already unique per MediaSource.)
    """
    me = item.id or -1
    sums = {p.checksum for p in item.playback_sources if p.checksum}
    if sums:
        other = s.scalar(select(PlaybackSource).where(PlaybackSource.checksum.in_(sums), PlaybackSource.media_item_id != me).limit(1))
        if other is not None:
            return (other.media_item.duplicate_of or other.media_item), f"identical file ({other.checksum.split(':')[0]} match)"
    urls = {p.url for p in item.playback_sources if p.url and p.playback_type in IPTV_TYPES}
    if urls:
        other = s.scalar(select(PlaybackSource).where(PlaybackSource.url.in_(urls), PlaybackSource.media_item_id != me).limit(1))
        if other is not None:
            return (other.media_item.duplicate_of or other.media_item), "same media URL"
    if not item.title_key or len(item.title_key) < 4:
        return None
    candidates = s.scalars(select(MediaItem).where(MediaItem.title_key == item.title_key, MediaItem.id != me).limit(20)).all()
    for c in candidates:
        if item.episode is not None and c.episode is not None and (item.season, item.episode) != (c.season, c.episode):
            continue  # same series title, different episode
        if item.duration and c.duration:
            tol = max(5.0, 0.03 * max(item.duration, c.duration))
            if abs(item.duration - c.duration) > tol:
                continue
        return (c.duplicate_of or c), "same title and duration"
    return None


def upsert_record(s: Session, rec: MediaRecord, rule: DiscoveryRule | None = None) -> tuple[MediaItem, bool]:
    """Insert or refresh a provider record. Returns (item, created)."""
    src = s.scalar(select(MediaSource).where(MediaSource.provider == rec.provider, MediaSource.external_id == rec.external_id))
    created = src is None
    now = utcnow()
    if created:
        src = MediaSource(provider=rec.provider, external_id=rec.external_id, discovered_by_rule_id=rule.id if rule else None)
        s.add(src)
    src.parent_external_id = rec.parent_external_id or src.parent_external_id
    src.original_url = rec.original_url
    src.creator = rec.creator
    src.creator_id = rec.creator_id
    src.availability_status = "available" if rec.available else "unavailable"
    src.last_checked_at = now
    src.raw = rec.raw

    item = src.item if not created else None
    if item is None:
        item = MediaItem(source=src, review_status=ReviewStatus.DISCOVERED, title=rec.title)
        s.add(item)
    if created or not item.metadata_locked:
        item.title = rec.title
        item.title_key = title_key(rec.title)
        item.description = rec.description
        item.publication_date = rec.publication_date
        item.duration = rec.duration
        item.thumbnail_url = rec.thumbnail_url
        item.tags = rec.tags
        item.categories = rec.categories
        item.series = rec.series if rec.series is not None else item.series
        item.season = rec.season if rec.season is not None else item.season
        item.episode = rec.episode if rec.episode is not None else item.episode
        item.artwork_in_epg = rec.artwork_in_epg
    item.restrictions = rec.restrictions
    item.flags = rec.flags

    # Provider-supplied playback options: add new ones, refresh known ones by URL.
    existing = {(p.playback_type, p.url): p for p in item.playback_sources if p.url}
    for opt in rec.playback:
        ps = existing.get((opt.playback_type, opt.url))
        if ps is None:
            ps = PlaybackSource(
                playback_type=opt.playback_type,
                url=opt.url,
                rights_status=RightsStatus.UNREVIEWED,
                eligibility=opt.eligibility,
            )
            item.playback_sources.append(ps)
        ps.label = opt.label
        ps.priority = opt.priority
        ps.license_url = opt.license_url
        ps.size_bytes = opt.size_bytes
        ps.checksum = opt.checksum or ps.checksum
        if ps.eligibility == Eligibility.UNVERIFIED:
            ps.width, ps.height, ps.duration = opt.width, opt.height, opt.duration
        if not opt.iptv_capable and ps.playback_type in IPTV_TYPES:
            # Provider says this file must not be used (e.g. access-restricted).
            ps.eligibility = Eligibility.UNAVAILABLE
    s.flush()

    if not rec.available and item.review_status not in (ReviewStatus.REJECTED,):
        if item.review_status != ReviewStatus.UNAVAILABLE:
            _audit(s, item, "provider_unavailable", item.review_status, ReviewStatus.UNAVAILABLE)
        item.review_status = ReviewStatus.UNAVAILABLE
    elif rec.available and item.review_status == ReviewStatus.UNAVAILABLE:
        _audit(s, item, "provider_available_again", item.review_status, ReviewStatus.PENDING_REVIEW)
        item.review_status = ReviewStatus.PENDING_REVIEW

    if created:
        flag_duplicate(s, item)
    return item, created


def file_sha1(path) -> str:
    """Checksum of a local file in the same form providers use."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return f"sha1:{h.hexdigest()}"


def flag_duplicate(s: Session, item: MediaItem) -> MediaItem | None:
    found = find_duplicate(s, item)
    if found is None:
        return None
    dup, why = found
    item.duplicate_of_id = dup.id
    item.flags = list(item.flags) + [f"Possible duplicate of #{dup.id} ({dup.source.provider}: {dup.title}) — {why}"]
    return dup


# -------------------------------------------------------------------- review


def _audit(s: Session, item: MediaItem | None, action: str, frm=None, to=None, note=None, actor="system") -> None:
    s.add(
        AuditLog(
            media_item_id=item.id if item else None,
            action=action,
            from_status=str(frm) if frm else None,
            to_status=str(to) if to else None,
            note=note,
            actor=actor,
        )
    )


def set_review_status(s: Session, item: MediaItem, status: ReviewStatus, actor: str = "admin", note: str | None = None) -> None:
    prev = item.review_status
    item.review_status = status
    item.reviewed_at = utcnow()
    if note:
        item.review_note = note
    _audit(s, item, "review", prev, status, note, actor)
    if status == ReviewStatus.APPROVED and item.source and item.source.rule:
        for ch in item.source.rule.channels:
            assign_to_channel(s, item, ch.id)


def assign_to_channel(s: Session, item: MediaItem, channel_id: int, role: ChannelRole = ChannelRole.PROGRAMME) -> ChannelItem:
    ci = s.scalar(
        select(ChannelItem).where(ChannelItem.channel_id == channel_id, ChannelItem.media_item_id == item.id, ChannelItem.role == role)
    )
    if ci is None:
        last = s.scalar(
            select(ChannelItem.position).where(ChannelItem.channel_id == channel_id).order_by(ChannelItem.position.desc()).limit(1)
        )
        ci = ChannelItem(channel_id=channel_id, media_item_id=item.id, role=role, position=(last or 0) + 10)
        s.add(ci)
    return ci


def set_rights(s: Session, ps: PlaybackSource, rights: RightsStatus, note: str | None = None, actor: str = "admin") -> None:
    if rights == RightsStatus.AUTHORISED and ps.playback_type not in IPTV_TYPES:
        raise ValueError("Only direct media files/streams and local files can be authorised for IPTV")
    if rights == RightsStatus.AUTHORISED and ps.playback_type == PlaybackType.YOUTUBE_STREAM and not settings.youtube_streaming:
        raise ValueError("Experimental YouTube streaming is switched off (ATV_YOUTUBE_STREAMING)")
    if rights == RightsStatus.AUTHORISED and ps.url and is_youtube_url(ps.url) and ps.playback_type != PlaybackType.YOUTUBE_STREAM:
        raise ValueError("YouTube URLs cannot be authorised as IPTV sources")
    prev = ps.rights_status
    ps.rights_status = rights
    if note is not None:
        ps.rights_note = note
    _audit(s, ps.media_item, "rights", prev, rights, f"source #{ps.id}: {note or ''}".strip(), actor)


def audit(s: Session, item: MediaItem | None, action: str, note: str | None = None, actor: str = "admin") -> None:
    _audit(s, item, action, note=note, actor=actor)
