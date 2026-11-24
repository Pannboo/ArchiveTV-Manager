"""Channel packs: shareable recipes that build a channel and keep it filled.

A pack is a plain JSON file (see ``archivetv/packs/*.json``)::

    {"id": "monster-movies", "name": "Monster Movies", "number": 13,
     "order": "shuffle", "policy": "auto_air", "target_hours": 150,
     "theme": "Monster, creature and B-movie horror features",
     "rules": [{"provider": "archive", "query": "monster OR creature",
                "params": {"collection": "feature_films", "min_duration": 3600}}],
     "blocks": [{"name": "Creature Feature", "days": "45", "start": "22:00", "end": "02:00",
                 "tag_filter": "horror"}]}

Installing one creates the channel, its discovery rules (Internet Archive or
YouTube) and time blocks, and starts filling it. From then on the fill job
compares the channel's playable hours with ``target_hours``: below it, the
pack's rules scan for more, paging further into the results each time
nothing new turns up; at or above it, they rest. Sources that die drop out
through the normal health checks, so the channel refills itself.

What happens to a find is the pack's **policy**:

* ``inbox``: it waits in the review queue like anything else discovered.
* ``auto_air``: it goes on air without review, but only with evidence that
  it may be rebroadcast: a public-domain or Creative Commons licence on the
  Archive item, a collection the pack declares public domain
  (``public_domain_collections``), or a Creative Commons YouTube upload
  (with YouTube streaming on, the default). Anything else goes to the
  review queue with the reason. Approval and authorisation are logged
  against the pack, so they can be traced and undone.

An optional **theme check** asks the AI backend (a local model on the lab
network, or Claude; see ai.py) whether each find fits the pack's ``theme``
before it airs; off-theme finds go to the review queue. Off by default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .catalogue import IPTV_TYPES, audit, is_schedulable, programme_duration, best_iptv_source, set_review_status, set_rights
from .config import settings
from .db import session_scope, utcnow
from .enums import IPTV_ELIGIBLE, ChannelRole, PackPolicy, PlaybackType, ReviewStatus, RightsStatus, RuleKind, ScheduleMode
from .models import Channel, ChannelBlock, ChannelItem, ChannelPack, DiscoveryRule, MediaItem, MediaSource

log = logging.getLogger(__name__)

STARTER_DIR = Path(__file__).parent / "packs"
FILL_INTERVAL = timedelta(minutes=30)  # between fill rounds while below target
FULL_RECHECK = timedelta(hours=6)  # how often a full pack re-checks its hours
MAX_PAGE = 40  # pages of Archive results a rule walks before starting over
PACK_FORMAT = 1
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# ------------------------------------------------------------------ format


class PackRule(BaseModel):
    name: str | None = None
    provider: Literal["archive", "youtube"] = "archive"
    kind: RuleKind = RuleKind.SEARCH
    query: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    max_results: int = Field(50, ge=1, le=500)
    policy: PackPolicy | None = None  # overrides the pack's policy for this rule


class PackBlock(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    days: str = "0123456"  # Mon=0 .. Sun=6
    start: str
    end: str
    tag_filter: str | None = None
    mode: ScheduleMode = ScheduleMode.SHUFFLE

    @field_validator("days")
    @classmethod
    def _days(cls, v: str) -> str:
        v = "".join(sorted(set(v)))
        if not v or any(c not in "0123456" for c in v):
            raise ValueError("days: digits 0 (Monday) to 6 (Sunday)")
        return v

    @field_validator("start", "end")
    @classmethod
    def _hm(cls, v: str) -> str:
        if not _HM.match(v):
            raise ValueError("times are HH:MM (24-hour)")
        return v


class PackSpec(BaseModel):
    format: int = PACK_FORMAT
    id: str
    name: str = Field(..., min_length=1, max_length=256)
    version: int = Field(1, ge=1)
    description: str | None = None
    author: str | None = None
    homepage: str | None = None
    number: int | None = Field(None, ge=1, le=9999)
    group: str = "ArchiveTV"
    logo_url: str | None = None  # inherited as the channel's logo; without one a logo is drawn in the look
    look: str | None = None  # break-card and logo look (themes.py); None = guessed from the name
    logo_text: str | None = Field(None, max_length=10)  # the drawn logo's wordmark, e.g. "NOIR"
    break_style: Literal["lineup", "bumper"] | None = None  # None = the look's default
    order: Literal["shuffle", "sequential"] = "shuffle"
    policy: PackPolicy = PackPolicy.INBOX
    target_hours: float = Field(100, ge=1, le=10000)
    refresh_days: int = Field(7, ge=1, le=90)  # weekly freshness: re-check, look for new uploads, rotate
    rotate_percent: int = Field(15, ge=0, le=50)  # share of the most-aired tapes rested at each refresh
    theme: str | None = Field(None, max_length=600)
    public_domain_collections: list[str] = Field(default_factory=list)
    breaks: bool = True
    break_seconds: int = Field(12, ge=4, le=120)
    repeat_avoid: int | None = Field(None, ge=0, le=500)
    rules: list[PackRule] = Field(..., min_length=1, max_length=20)
    blocks: list[PackBlock] = Field(default_factory=list, max_length=50)

    @field_validator("look")
    @classmethod
    def _look(cls, v: str | None) -> str | None:
        from .themes import THEMES

        if v is not None and v not in THEMES:
            raise ValueError(f"look: one of {', '.join(THEMES)}")
        return v

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        v = v.strip().lower()
        if not _SLUG.match(v):
            raise ValueError("id: lowercase letters, digits and dashes")
        return v


class PackError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def parse_spec(data: Any) -> PackSpec:
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except ValueError as e:
            raise PackError(400, f"not valid JSON: {e}")
    if not isinstance(data, dict):
        raise PackError(400, "a pack is a JSON object")
    try:
        return PackSpec.model_validate(data)
    except ValueError as e:
        raise PackError(400, f"invalid pack: {e}")


def starter_packs() -> list[PackSpec]:
    out = []
    for p in sorted(STARTER_DIR.glob("*.json")):
        try:
            out.append(parse_spec(p.read_text(encoding="utf-8")))
        except (OSError, PackError) as e:
            log.error("starter pack %s is invalid: %s", p.name, e)
    return out


def starter_pack(key: str) -> PackSpec | None:
    return next((p for p in starter_packs() if p.id == key), None)


# ----------------------------------------------------------------- install


def _free_slug(s: Session, want: str) -> str:
    slug, n = want, 2
    while s.scalar(select(Channel.id).where((Channel.slug == slug) | (Channel.tvg_id == f"{slug}.archivetv"))):
        slug, n = f"{want}-{n}", n + 1
    return slug


def _rule_params(rule: PackRule) -> dict:
    p = dict(rule.params)
    if rule.provider == "archive":
        p.setdefault("sort", "downloads desc")  # stable order, so paging moves forward
        p.setdefault("page", 1)
    if rule.policy:
        p["pack_policy"] = str(rule.policy)
    return p


def _apply_rules_and_blocks(s: Session, pack: ChannelPack, spec: PackSpec, ch: Channel) -> None:
    # Finds the old rules made stay the pack's: re-linked below to a new rule
    # of the same provider, so waiting ones are still reviewed and aired by it.
    old_ids = [r.id for r in pack.rules if r.id is not None]
    found_by: dict[str, list[int]] = {}
    if old_ids:
        for src_id, provider in s.execute(select(MediaSource.id, MediaSource.provider)
                                          .where(MediaSource.discovered_by_rule_id.in_(old_ids))).all():
            found_by.setdefault(provider, []).append(src_id)
    pack.rules.clear()  # delete-orphan: the old rules go
    ch.blocks.clear()
    s.flush()
    for r in spec.rules:
        rule = DiscoveryRule(
            name=f"{spec.name}: {r.name or (r.query or r.kind)}"[:256], provider=r.provider, kind=r.kind, query=r.query,
            params=_rule_params(r), max_results=r.max_results, scan_interval_minutes=24 * 60, enabled=True,
        )
        pack.rules.append(rule)
        s.add(rule)
        rule.channels = [ch]
    s.flush()
    for provider, src_ids in found_by.items():
        heir = next((r for r in pack.rules if r.provider == provider), None)
        if heir is not None:
            # An UPDATE statement, not attribute writes: deleting the old rules
            # nulled these links in the database (ON DELETE SET NULL) while the
            # loaded objects still show the old id, which a new rule may reuse.
            s.execute(update(MediaSource).where(MediaSource.id.in_(src_ids)).values(discovered_by_rule_id=heir.id))
    for b in spec.blocks:
        ch.blocks.append(ChannelBlock(name=b.name, days=b.days, start_time=b.start, end_time=b.end,
                                      tag_filter=b.tag_filter, mode=b.mode))


def install(s: Session, spec: PackSpec, *, theme_check: bool = False, actor: str = "admin",
            upgrade: bool = False) -> ChannelPack:
    """Create the pack's channel, rules and blocks. With ``upgrade`` an
    installed pack of the same id is updated in place (its channel, items
    and history are kept)."""
    pack = s.scalar(select(ChannelPack).where(ChannelPack.key == spec.id))
    if pack is not None and not upgrade:
        raise PackError(409, f"pack '{spec.id}' is already installed")
    if pack is None:
        slug = _free_slug(s, spec.id)
        number = spec.number
        if number is not None and s.scalar(select(Channel.id).where(Channel.number == number)):
            number = None  # keep the admin's numbering; they can renumber later
        import secrets

        ch = Channel(slug=slug, tvg_id=f"{slug}.archivetv", name=spec.name, number=number, description=spec.description,
                     logo_url=spec.logo_url, group_title=spec.group, seed=secrets.token_hex(4))
        s.add(ch)
        s.flush()
        pack = ChannelPack(key=spec.id, name=spec.name, channel_id=ch.id, spec={})
        s.add(pack)
    else:
        ch = pack.channel
        if ch is None:
            raise PackError(409, "the pack's channel was deleted; uninstall the pack and install it again")
    ch.mode = ScheduleMode.SEQUENTIAL if spec.order == "sequential" else ScheduleMode.SHUFFLE
    ch.breaks_enabled = spec.breaks
    ch.break_seconds = spec.break_seconds
    if spec.repeat_avoid is not None:
        ch.repeat_avoid_count = spec.repeat_avoid
    fresh = pack.id is None or not pack.spec
    pack.name = spec.name
    pack.version = spec.version
    if fresh:
        # An upgrade keeps the admin's own choices; only a new install takes the file's.
        pack.policy = str(spec.policy)
        pack.target_hours = spec.target_hours
        pack.refresh_days = spec.refresh_days
        pack.rotate_percent = spec.rotate_percent
        pack.theme_check = bool(theme_check and spec.theme)
    elif theme_check:
        pack.theme_check = bool(spec.theme)
    pack.spec = spec.model_dump(mode="json")
    pack.enabled = True
    pack.last_fill_at = None  # fill straight away
    if spec.logo_url and not ch.logo_file:
        ch.logo_url = spec.logo_url
    _apply_rules_and_blocks(s, pack, spec, ch)
    s.flush()
    from . import logos

    try:
        logos.generate(s, ch, force=ch.logo_generated)  # drawn in the pack's look unless it brings a logo
    except RuntimeError as e:
        log.warning("could not draw a logo for %s: %s", ch.slug, e)
    audit(s, None, "pack_upgrade" if upgrade else "pack_install", f"{spec.id} v{spec.version} -> channel {ch.slug}", actor)
    return pack


def uninstall(s: Session, pack: ChannelPack, *, disable_channel: bool = False, actor: str = "admin") -> None:
    """Remove the pack and its rules. The channel, its items and its history
    stay (disable or delete the channel separately)."""
    if disable_channel and pack.channel is not None:
        pack.channel.enabled = False
    audit(s, None, "pack_uninstall", pack.key, actor)
    s.delete(pack)


def export(pack: ChannelPack) -> dict:
    """The pack as a file, with the admin's current target and policy."""
    spec = dict(pack.spec or {})
    spec["target_hours"] = pack.target_hours
    spec["policy"] = pack.policy
    spec["refresh_days"] = pack.refresh_days
    spec["rotate_percent"] = pack.rotate_percent
    ch = pack.channel
    if ch is not None:
        spec["breaks"] = bool(ch.breaks_enabled)
        spec["break_seconds"] = int(ch.break_seconds or 12)
        spec["blocks"] = [{"name": b.name, "days": b.days, "start": b.start_time, "end": b.end_time,
                           "tag_filter": b.tag_filter, "mode": b.mode} for b in ch.blocks]
    return parse_spec(spec).model_dump(mode="json", exclude_none=True)


# ---------------------------------------------------------------- status


def playable_hours(s: Session, ch: Channel) -> float:
    """Hours of distinct programmes the channel can air right now."""
    total = 0.0
    for ci in s.scalars(select(ChannelItem).where(ChannelItem.channel_id == ch.id, ChannelItem.role == ChannelRole.PROGRAMME)).all():
        item = ci.media_item
        if item is None or not is_schedulable(item):
            continue
        ps = best_iptv_source(item)
        if ps is not None and ps.live:
            continue
        total += programme_duration(item, ps) or 0.0
    return total / 3600.0


def _channel_counts(s: Session, pack_id: int) -> dict:
    from .models import FollowedChannel

    rows = s.execute(select(FollowedChannel.status, func.count()).where(FollowedChannel.pack_id == pack_id)
                     .group_by(FollowedChannel.status)).all()
    return {status: n for status, n in rows}


def _ai_available() -> bool:
    from . import ai

    return ai.available()


def pack_out(s: Session, pack: ChannelPack) -> dict:
    ch = pack.channel
    hours = playable_hours(s, ch) if ch is not None else 0.0
    rule_ids = [r.id for r in pack.rules]
    waiting = 0
    if rule_ids:
        waiting = s.scalar(
            select(func.count()).select_from(MediaItem).join(MediaSource)
            .where(MediaSource.discovered_by_rule_id.in_(rule_ids), MediaItem.review_status == ReviewStatus.PENDING_REVIEW)
        ) or 0
    return {
        "id": pack.id, "key": pack.key, "name": pack.name, "version": pack.version, "policy": pack.policy,
        "target_hours": pack.target_hours, "playable_hours": round(hours, 1), "enabled": pack.enabled,
        "theme_check": pack.theme_check, "theme": (pack.spec or {}).get("theme"),
        "theme_check_available": _ai_available(),
        "description": (pack.spec or {}).get("description"),
        "channel": {"id": ch.id, "slug": ch.slug, "name": ch.name, "number": ch.number, "enabled": ch.enabled} if ch else None,
        "rules": [{"id": r.id, "name": r.name, "provider": r.provider, "page": (r.params or {}).get("page"),
                   "last_run_at": r.last_run_at.isoformat() + "Z" if r.last_run_at else None,
                   "last_status": r.last_status, "last_new_count": r.last_new_count} for r in pack.rules],
        "waiting_review": waiting, "aired_count": pack.aired_count, "inbox_count": pack.inbox_count,
        "installed_at": pack.installed_at.isoformat() + "Z", "last_fill_at": pack.last_fill_at.isoformat() + "Z" if pack.last_fill_at else None,
        "last_fill_status": pack.last_fill_status, "last_error": pack.last_error,
        "refresh_days": pack.refresh_days, "rotate_percent": pack.rotate_percent,
        "last_refresh_at": pack.last_refresh_at.isoformat() + "Z" if pack.last_refresh_at else None,
        "next_refresh_at": (refresh_due_at(pack).isoformat() + "Z"), "last_refresh": pack.last_refresh or {},
        "resting": len((pack.state or {}).get("rested") or {}),
        "youtube_channels": _channel_counts(s, pack.id),
    }


# -------------------------------------------------------------- rights


def _is_open_licence(url: str | None) -> bool:
    u = (url or "").lower()
    return "creativecommons.org/publicdomain" in u or "creativecommons.org/licenses" in u or "publicdomain" in u


LAB_REASON = "lab mode: every playable find airs (licence not checked)"


def auto_air_evidence(item: MediaItem, ps, public_domain_collections: list[str]) -> tuple[bool, str]:
    """(may this find air without review, why). Conservative: no evidence, no
    air, unless lab mode (``packs_air_everything``) is on, where anything
    playable airs."""
    if ps is None:
        return False, "no playable source passed validation"
    provider = item.source.provider if item.source else None
    if settings.packs_air_everything:
        if provider == "archive" and (item.restrictions or {}).get("access_restricted"):
            return False, "the Archive marks this item access-restricted (it can't be streamed)"
        if provider == "youtube" and ps.playback_type != PlaybackType.YOUTUBE_STREAM:
            return False, "YouTube streaming is switched off"
        return True, LAB_REASON
    if provider == "archive":
        restr = item.restrictions or {}
        if restr.get("access_restricted"):
            return False, "the Archive marks this item access-restricted"
        lic = ps.license_url or restr.get("license_url")
        if _is_open_licence(lic):
            return True, f"licence on the Archive item: {lic}"
        mine = {c.lower() for c in (item.categories or [])}
        pd = mine & {c.lower() for c in public_domain_collections}
        if pd:
            return True, f"in the '{sorted(pd)[0]}' collection, which the pack declares public domain"
        return False, "no public-domain or Creative Commons licence on the Archive item"
    if provider == "youtube":
        if (item.restrictions or {}).get("youtube_license") != "creativeCommon":
            return False, "standard YouTube licence: needs your review"
        if ps.playback_type != PlaybackType.YOUTUBE_STREAM:
            return False, "Creative Commons, but YouTube streaming is switched off"
        return True, "Creative Commons (CC BY) YouTube upload"
    return False, "only Internet Archive and YouTube finds can air without review"


def _candidate_source(item: MediaItem):
    """Best validated IPTV source, whatever its rights so far."""
    ok = [p for p in item.playback_sources if p.playback_type in IPTV_TYPES and p.eligibility in IPTV_ELIGIBLE and p.health_ok]
    return min(ok, key=lambda p: p.priority) if ok else None


# ----------------------------------------------------------- theme check


async def theme_check(pack_name: str, theme: str, items: list[tuple[int, dict]]) -> dict[int, tuple[bool, str]]:
    """Whether each find fits the pack's theme, on the configured AI backend
    (a local model or Claude, see ai.py). Failures count as "doesn't fit"."""
    from . import ai

    return await ai.theme_check(pack_name, theme, items)


def _item_info(item: MediaItem) -> dict:
    return {
        "title": item.title,
        "description": (item.description or "")[:1500],
        "year": (item.publication_date or "")[:4] or None,
        "creator": item.source.creator if item.source else None,
        "subjects": list(item.tags or [])[:20],
        "collections": list(item.categories or [])[:10],
        "minutes": round(item.duration / 60) if item.duration else None,
    }


# ---------------------------------------------------- after discovery


def _flag(item: MediaItem, note: str) -> None:
    flags = [f for f in (item.flags or []) if not f.startswith("Pack: ")]
    item.flags = flags + [f"Pack: {note}"]


async def after_validation(rule_id: int, item_ids: list[int], recount: bool = True) -> dict:
    """Apply the pack's policy to new finds of one of its rules, once validated.
    ``recount=False`` when re-applying it to finds already counted (weekly refresh)."""
    result = {"aired": 0, "inbox": 0}
    if not item_ids:
        return result
    with session_scope() as s:
        rule = s.get(DiscoveryRule, rule_id)
        pack = rule.pack if rule else None
        if pack is None:
            return result
        policy = (rule.params or {}).get("pack_policy") or pack.policy
        spec = pack.spec or {}
        pd = list(spec.get("public_domain_collections") or [])
        from . import ai

        theme = spec.get("theme") if pack.theme_check and ai.available() else None
        pack_id, pack_key, pack_name = pack.id, pack.key, pack.name
        to_air: list[tuple[int, int, str]] = []  # (item id, source id, evidence)
        for iid in item_ids:
            item = s.get(MediaItem, iid)
            if item is None or item.review_status != ReviewStatus.PENDING_REVIEW or item.duplicate_of_id:
                continue
            kind = (item.ai_meta or {}).get("kind")
            if kind in ai.NOT_PROGRAMMES:
                # The AI clean-up read it as a clip, trailer or reaction video, not a programme.
                _flag(item, f"'{pack_name}' found this; AI says it's a {kind}, not a full programme: waiting for review")
                result["inbox"] += 1
                continue
            ps = _candidate_source(item)
            ok, why = auto_air_evidence(item, ps, pd)
            if policy != PackPolicy.AUTO_AIR or not ok:
                _flag(item, f"'{pack_name}' found this; waiting for review ({why if policy == PackPolicy.AUTO_AIR else 'pack policy is review first'})")
                result["inbox"] += 1
                continue
            to_air.append((iid, ps.id, why))
        infos = {iid: _item_info(s.get(MediaItem, iid)) for iid, _, _ in to_air} if theme else {}
    verdicts = await theme_check(pack_name, theme, list(infos.items())) if theme and infos else {}
    with session_scope() as s:
        for iid, psid, why in to_air:
            item = s.get(MediaItem, iid)
            ps = next((p for p in item.playback_sources if p.id == psid), None) if item else None
            if item is None or ps is None or item.review_status != ReviewStatus.PENDING_REVIEW:
                continue
            fits, reason = verdicts.get(iid, (True, ""))
            if not fits:
                _flag(item, f"'{pack_name}' theme check: off-theme ({reason})")
                result["inbox"] += 1
                continue
            actor = f"pack:{pack_key}"
            note = f"auto-aired by pack '{pack_key}': {why}" + (f"; theme check: {reason}" if reason else "")
            if ps.rights_status != RightsStatus.AUTHORISED:
                set_rights(s, ps, RightsStatus.AUTHORISED, note=note[:1000], actor=actor)
            set_review_status(s, item, ReviewStatus.APPROVED, actor=actor, note=note[:1000])
            result["aired"] += 1
        pack = s.get(ChannelPack, pack_id)
        if pack is not None:
            pack.aired_count += result["aired"]
            if recount:
                pack.inbox_count += result["inbox"]
    return result


# ------------------------------------------------------------- filling


def ytfollow_rss_note() -> str:
    return "followed channel: checked through its free RSS feed until its weekly in-depth relist"


def _has_followed(pack_id: int) -> bool:
    from .models import FollowedChannel

    with session_scope() as s:
        return s.scalar(select(FollowedChannel.id).where(FollowedChannel.pack_id == pack_id,
                                                         FollowedChannel.status == "followed").limit(1)) is not None


def _plan_rules(s: Session, pack: ChannelPack, now) -> tuple[list[int], list[tuple[int, str]]]:
    """Which rules scan this round, and which wait (with why). Archive rules
    always scan; YouTube keyword searches spend the shared daily budget and
    back off once the pack follows enough channels; followed channels are
    relisted weekly and otherwise watched through RSS."""
    from . import ytfollow

    run: list[int] = []
    held: list[tuple[int, str]] = []
    yt_ok = None
    followed = ytfollow.followed_count(s, pack.id)
    for r in pack.rules:
        if not r.enabled:
            continue
        if r.provider != "youtube":
            run.append(r.id)
            continue
        if yt_ok is None:
            yt_ok = youtube_available()
        if not yt_ok and not ytfollow.is_channel_rule(r):
            held.append((r.id, YT_SKIP))
        elif ytfollow.is_channel_rule(r):
            if r.last_run_at is None or now - r.last_run_at >= ytfollow.REHARVEST:
                if yt_ok:
                    run.append(r.id)
                else:
                    held.append((r.id, YT_SKIP + " (new uploads are still picked up from its RSS feed)"))
            else:
                held.append((r.id, ytfollow_rss_note()))
        elif ytfollow.is_search_rule(r):
            if not ytfollow.search_due(r, followed, now):
                held.append((r.id, "search rests: runs at most every 12 hours, weekly once the pack follows "
                                   f"{ytfollow.CURATED_ENOUGH}+ channels"))
            elif not ytfollow.take_search():
                held.append((r.id, "today's YouTube search budget is spent (Settings: YouTube searches per day)"))
            else:
                run.append(r.id)
        else:
            run.append(r.id)  # playlists and single videos cost little
    return run, held


async def fill_pack(pack_id: int, force: bool = False) -> dict:
    """One fill round: scan the pack's rules if the channel is below target."""
    from .discovery import run_rule
    from .scheduler import maintain

    now = utcnow()
    with session_scope() as s:
        pack = s.get(ChannelPack, pack_id)
        if pack is None or not pack.enabled or pack.channel is None:
            return {"skipped": "disabled"}
        hours = playable_hours(s, pack.channel)
        due = pack.last_fill_at is None or now - pack.last_fill_at >= (FULL_RECHECK if hours >= pack.target_hours else FILL_INTERVAL)
        if not force and not due:
            return {"skipped": "not due", "hours": hours}
        pack.last_fill_at = now
        if hours >= pack.target_hours:
            pack.last_fill_status, pack.last_error = "full", None
            return {"status": "full", "hours": hours}
        rule_ids, held = _plan_rules(s, pack, now)
        pack.last_fill_status = "filling"
    for rid, why in held:
        _skip_rule(rid, why)
    new = found = 0
    errors: list[str] = []
    exhausted = 0
    if _has_followed(pack_id):
        from . import ytfollow

        new += await ytfollow.check_feeds(pack_id)  # free: followed channels' RSS feeds
    for rid in rule_ids:
        res = await run_rule(rid)
        new += res.new
        found += res.found
        errors += res.errors
        with session_scope() as s:
            rule = s.get(DiscoveryRule, rid)
            if rule is None or rule.provider != "archive" or res.errors:
                continue
            params = dict(rule.params or {})
            page = int(params.get("page") or 1)
            if res.found == 0 or page >= MAX_PAGE:
                params["page"] = 1  # end of the results: start over (new uploads appear at the top)
                exhausted += 1
            elif res.new == 0:
                params["page"] = page + 1  # everything here is known: look further next time
            rule.params = params
    with session_scope() as s:
        pack = s.get(ChannelPack, pack_id)
        if pack is None:
            return {}
        if pack.channel is not None:
            maintain(s, pack.channel)
        archive_rules = [r for r in rule_ids if (rr := s.get(DiscoveryRule, r)) is not None and rr.provider == "archive"]
        status = "error" if errors and not new else (
            "exhausted" if archive_rules and exhausted == len(archive_rules) and not new else "filling")
        pack.last_fill_status = status
        pack.last_error = "; ".join(errors)[:2000] or None
        return {"status": status, "new": new, "found": found, "errors": errors}


# ------------------------------------------------------- weekly freshness
#
# Spelunkr-style upkeep, once per pack every ``refresh_days`` (default 7):
#
# 1. Re-check every tape: fetch fresh metadata from the Internet Archive and
#    YouTube. A tape that's gone is marked unavailable and drops off the
#    schedule; one whose licence no longer backs the pack's auto-air goes
#    back to review. (Files are also health-checked every few hours; this
#    catches what a download test can't: licence and metadata changes.)
# 2. Re-validate the pack's finds still waiting for review and apply the
#    pack's policy again (e.g. a Creative Commons YouTube video that now has
#    a stream source).
# 3. Look for new uploads: every rule scans its newest results (Archive
#    "addeddate", YouTube "date"), whether or not the channel is full.
# 4. Rotate: rest the most-aired ``rotate_percent`` of the channel's tapes
#    for REST_CYCLES refreshes, and bring back tapes whose rest is over. The
#    published schedule is rebuilt from the first slot a rested tape held.
#    Resting tapes stay approved in the catalogue.

REST_CYCLES = 4  # a rested tape returns after this many refreshes (4 weeks by default)


async def reapply_waiting(pack_id: int, waiting: list[int] | None = None, revalidate: bool = True) -> int:
    """Run the pack's policy again over its finds waiting for review (after a
    metadata refresh, or when lab mode is switched on). Returns how many aired."""
    from .discovery import validate_items

    if waiting is None:
        with session_scope() as s:
            pack = s.get(ChannelPack, pack_id)
            if pack is None:
                return 0
            waiting = _pack_item_ids(s, pack)[1]
    if not waiting:
        return 0
    if revalidate:
        await validate_items(waiting)
    by_rule: dict[int, list[int]] = {}
    with session_scope() as s:
        for iid in waiting:
            item = s.get(MediaItem, iid)
            if item is not None and item.source and item.source.discovered_by_rule_id:
                by_rule.setdefault(item.source.discovered_by_rule_id, []).append(iid)
    aired = 0
    for rid, ids in by_rule.items():
        aired += (await after_validation(rid, ids, recount=False)).get("aired", 0)
    return aired


async def reapply_all() -> int:
    """Lab mode switched on: air what every auto-air pack has been holding back."""
    with session_scope() as s:
        ids = list(s.scalars(select(ChannelPack.id).where(ChannelPack.enabled.is_(True),
                                                          ChannelPack.policy == PackPolicy.AUTO_AIR)).all())
    total = 0
    for pid in ids:
        try:
            total += await reapply_waiting(pid, revalidate=False)
        except Exception:
            log.exception("pack %s: re-applying the policy failed", pid)
    log.info("lab mode: %s waiting find(s) put on air", total)
    return total


def youtube_available() -> bool:
    """YouTube search rules need an API key or unofficial discovery."""
    from .providers import get_provider

    try:
        return bool(get_provider("youtube").status().get("configured"))
    except Exception:  # noqa: BLE001
        return False


def _skip_rule(rule_id: int, why: str) -> None:
    with session_scope() as s:
        rule = s.get(DiscoveryRule, rule_id)
        if rule is not None:
            # Not a run: last_run_at stays, so a rule held back today runs as soon as it may.
            rule.last_status, rule.last_error = "skipped", why


YT_SKIP = "skipped: YouTube search needs an API key (or unofficial discovery) under Settings"
MIN_KEEP_SHARE = 0.5  # never rest tapes below this share of the target hours


def refresh_due_at(pack: ChannelPack):
    base = pack.last_refresh_at or pack.installed_at or utcnow()
    return base + timedelta(days=max(1, pack.refresh_days or 7))


def _pack_item_ids(s: Session, pack: ChannelPack) -> tuple[list[int], list[int]]:
    """(items on the pack's channel, items its rules found still in review)."""
    on_air = list(s.scalars(select(ChannelItem.media_item_id).where(ChannelItem.channel_id == pack.channel_id)).all()) \
        if pack.channel_id else []
    rule_ids = [r.id for r in pack.rules]
    waiting = list(s.scalars(
        select(MediaItem.id).join(MediaSource)
        .where(MediaSource.discovered_by_rule_id.in_(rule_ids), MediaItem.review_status == ReviewStatus.PENDING_REVIEW)
    ).all()) if rule_ids else []
    return on_air, waiting


async def refresh_metadata(item_ids: list[int]) -> dict:
    """Fetch fresh provider metadata for these items (Archive and YouTube)
    and fold it in. Never creates new catalogue items."""
    from .catalogue import upsert_record
    from .providers import MediaRecord, ProviderError, get_provider

    out = {"checked": 0, "gone": 0, "errors": []}
    with session_scope() as s:
        rows = [(i.id, i.source.provider, i.source.external_id, i.source.parent_external_id)
                for i in (s.get(MediaItem, x) for x in item_ids) if i is not None and i.source is not None]
    known = {(p, e) for _, p, e, _ in rows}
    records = []
    archive_ids = sorted({(parent or ext.split("/", 1)[0]) for _, p, ext, parent in rows if p == "archive"})
    gone_identifiers: set[str] = set()
    if archive_ids:
        prov = get_provider("archive")
        sem = asyncio.Semaphore(4)

        async def one(identifier: str):
            async with sem:
                try:
                    recs = await prov.get_item_records(identifier)
                except ProviderError as e:
                    out["errors"].append(str(e)[:200])
                    return
                if not recs:
                    gone_identifiers.add(identifier)  # removed or darked on the Archive
                records.extend(recs)

        await asyncio.gather(*(one(i) for i in archive_ids))
    yt_ids = [ext for _, p, ext, _ in rows if p == "youtube" and not ext.startswith("live:")]
    if yt_ids:
        prov = get_provider("youtube")
        try:
            for i in range(0, len(yt_ids), 50):
                records.extend(await prov.expand([MediaRecord(provider="youtube", external_id=v, title=v) for v in yt_ids[i: i + 50]]))
        except ProviderError as e:
            out["errors"].append(str(e)[:200])
    with session_scope() as s:
        for rec in records:
            if (rec.provider, rec.external_id) in known:
                upsert_record(s, rec)
                out["checked"] += 1
        for item_id, p, ext, parent in rows:
            if p == "archive" and (parent or ext.split("/", 1)[0]) in gone_identifiers:
                item = s.get(MediaItem, item_id)
                if item is not None and item.review_status != ReviewStatus.UNAVAILABLE:
                    audit(s, item, "provider_unavailable", "gone from the Internet Archive (weekly check)", "system")
                    item.review_status = ReviewStatus.UNAVAILABLE
                    item.source.availability_status = "unavailable"
                    out["gone"] += 1
    return out


def _recheck_licences(s: Session, pack: ChannelPack, item_ids: list[int]) -> int:
    """Send back to review the tapes this pack put on air whose licence
    evidence no longer holds (metadata just refreshed)."""
    from .models import AuditLog

    actor = f"pack:{pack.key}"
    pd = list((pack.spec or {}).get("public_domain_collections") or [])
    revoked = 0
    for iid in item_ids:
        item = s.get(MediaItem, iid)
        if item is None or item.review_status != ReviewStatus.APPROVED:
            continue
        last = s.scalar(select(AuditLog).where(AuditLog.media_item_id == iid, AuditLog.action == "review")
                        .order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(1))
        if last is None or last.actor != actor:
            continue  # approved by a person: their decision stands
        ok, why = auto_air_evidence(item, _candidate_source(item), pd)
        if ok:
            continue
        for ps in item.playback_sources:
            if ps.rights_status == RightsStatus.AUTHORISED:
                set_rights(s, ps, RightsStatus.UNREVIEWED, note=f"weekly check: {why}", actor=actor)
        set_review_status(s, item, ReviewStatus.PENDING_REVIEW, actor=actor, note=f"weekly check: {why}")
        _flag(item, f"'{pack.name}' weekly check: {why}; back to review")
        revoked += 1
    return revoked


def _rotate(s: Session, pack: ChannelPack, now) -> dict:
    """Bring rested tapes back, then rest the most-aired share."""
    from .catalogue import assign_to_channel
    from .models import ScheduleEntry
    from .scheduler import GRACE, extend, record_version, truncate_from

    ch = pack.channel
    state = dict(pack.state or {})
    rested = {int(k): v for k, v in (state.get("rested") or {}).items()}
    back = 0
    for iid, until in list(rested.items()):
        if until <= now.isoformat():
            rested.pop(iid)
            item = s.get(MediaItem, iid)
            if item is not None and is_schedulable(item):
                assign_to_channel(s, item, ch.id)
                back += 1
    s.flush()
    out = {"rotated_back": back, "rotated_out": 0}
    pct = max(0, min(50, pack.rotate_percent or 0))
    if pct:
        since = now - timedelta(days=max(1, pack.refresh_days or 7))
        plays = dict(s.execute(
            select(ScheduleEntry.media_item_id, func.count())
            .where(ScheduleEntry.channel_id == ch.id, ScheduleEntry.kind == "programme",
                   ScheduleEntry.start >= since, ScheduleEntry.start < now, ScheduleEntry.media_item_id.is_not(None))
            .group_by(ScheduleEntry.media_item_id)
        ).all())
        items = [ci for ci in s.scalars(select(ChannelItem).where(ChannelItem.channel_id == ch.id,
                                                                    ChannelItem.role == ChannelRole.PROGRAMME)).all()]
        count = int(round(len(items) * pct / 100))
        hours = playable_hours(s, ch)
        floor = pack.target_hours * MIN_KEEP_SHARE
        until = (now + timedelta(days=max(1, pack.refresh_days or 7) * REST_CYCLES)).isoformat()
        chosen = []
        for ci in sorted((ci for ci in items if plays.get(ci.media_item_id)), key=lambda ci: (-plays[ci.media_item_id], ci.id)):
            if len(chosen) >= count:
                break
            item = ci.media_item
            dur = (programme_duration(item, best_iptv_source(item)) or 0) / 3600 if item and is_schedulable(item) else 0
            if hours - dur < floor:
                break
            hours -= dur
            chosen.append(ci)
        for ci in chosen:
            rested[ci.media_item_id] = until
            s.delete(ci)
        s.flush()
        out["rotated_out"] = len(chosen)
        if chosen:
            gone = {ci.media_item_id for ci in chosen}
            first = s.scalar(select(func.min(ScheduleEntry.start)).where(
                ScheduleEntry.channel_id == ch.id, ScheduleEntry.start >= now + GRACE,
                ScheduleEntry.media_item_id.in_(gone)))
            if first is not None:
                # The break card before that slot names the rested tape: rebuild from it.
                prev = s.scalar(select(ScheduleEntry).where(ScheduleEntry.channel_id == ch.id, ScheduleEntry.end == first,
                                                           ScheduleEntry.kind == "break").limit(1))
                cut = prev.start if prev is not None and prev.start >= now + GRACE else first
                s.refresh(ch)
                removed = truncate_from(s, ch, cut)
                added = extend(s, ch, now)
                record_version(s, ch, "rotate", cut, added, removed, actor=f"pack:{pack.key}",
                               note=f"weekly rotation: {len(chosen)} tape(s) rested")
    state["rested"] = {str(k): v for k, v in rested.items()}
    pack.state = state
    return out


async def refresh_pack(pack_id: int, force: bool = False) -> dict:
    """One weekly refresh (see above). Returns its summary."""
    from .discovery import run_query

    now = utcnow()
    with session_scope() as s:
        pack = s.get(ChannelPack, pack_id)
        if pack is None or pack.channel is None or (not pack.enabled and not force):
            return {"skipped": "disabled"}
        if not force and refresh_due_at(pack) > now:
            return {"skipped": "not due"}
        on_air, waiting = _pack_item_ids(s, pack)
        rules = [(r.id, r.provider, r.kind, r.query, dict(r.params or {}), r.max_results) for r in pack.rules if r.enabled]
        pack.last_refresh_at = now  # claimed: a crash mid-refresh waits for next week rather than looping
    summary: dict = {"at": now.isoformat() + "Z", "errors": []}
    # 1. re-check every tape against its provider
    meta = await refresh_metadata(on_air + waiting)
    summary.update({"checked": meta["checked"], "gone": meta["gone"]})
    summary["errors"] += meta["errors"]
    with session_scope() as s:
        pack = s.get(ChannelPack, pack_id)
        summary["back_to_review"] = _recheck_licences(s, pack, on_air)
    # 2. finds still waiting: validate again (new YouTube stream sources) and re-apply the policy
    summary["waiting_aired"] = await reapply_waiting(pack_id, waiting)
    # 3. new uploads, whatever the channel's size
    new = 0
    from . import ytfollow

    yt_ok = youtube_available()
    new += await ytfollow.check_feeds(pack_id, force=True)  # followed channels: free RSS
    for rid, provider, kind, query, params, maxr in rules:
        if provider == "youtube" and kind == "channel":
            continue  # covered by the RSS check above; relisted in depth by the fill job weekly
        if provider == "youtube" and not yt_ok:
            _skip_rule(rid, YT_SKIP)
            continue
        if provider == "youtube" and kind == "search" and not ytfollow.take_search():
            _skip_rule(rid, "today's YouTube search budget is spent (Settings: YouTube searches per day)")
            continue
        fresh = dict(params)
        if provider == "archive":
            fresh.update({"sort": "addeddate desc", "page": 1})
        elif provider == "youtube":
            fresh["order"] = "date"
        res = await run_query(provider, kind, query, fresh, maxr, rule_id=rid)
        new += res.new
        summary["errors"] += res.errors
    summary["new"] = new
    # 4. rotation
    from .scheduler import maintain

    with session_scope() as s:
        pack = s.get(ChannelPack, pack_id)
        summary.update(_rotate(s, pack, utcnow()))
        maintain(s, pack.channel)
        summary["errors"] = [e[:200] for e in summary["errors"]][:10]
        pack.last_refresh = summary
        pack.last_fill_at = None  # top up straight after if the rotation left it short
        audit(s, None, "pack_refresh", f"{pack.key}: {json.dumps({k: v for k, v in summary.items() if k != 'errors'})}",
              f"pack:{pack.key}")
    log.info("pack %s weekly refresh: %s", pack_id, summary)
    return summary


async def fill_tick() -> None:
    with session_scope() as s:
        ids = list(s.scalars(select(ChannelPack.id).where(ChannelPack.enabled.is_(True))).all())
    for pid in ids:
        try:
            await refresh_pack(pid)  # no-op unless the week is up
        except Exception:
            log.exception("pack %s refresh failed", pid)
        try:
            r = await fill_pack(pid)
            if r.get("new"):
                log.info("pack %s: %s new item(s)", pid, r["new"])
        except Exception:
            log.exception("pack %s fill failed", pid)
