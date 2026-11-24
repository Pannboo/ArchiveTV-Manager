"""Curated YouTube channels for channel packs.

Keyword searches are the expensive part of YouTube discovery (100 quota
units each, and YouTube caps them per day), so packs use them to *discover
channels* rather than to fill up:

1. A pack's YouTube search turns up videos; their channels become
   **suggestions** (with how many finds they produced, example titles and,
   when an AI backend is set, its verdict on whether the channel fits).
2. An admin **follows** or **dismisses** each suggestion, or adds a channel
   directly (``@handle`` or channel URL). Nothing is followed automatically.
3. A followed channel becomes a channel rule in the pack: its uploads are
   harvested once in depth (1 unit per 50 videos), then checked for new
   uploads through the channel's public RSS feed (free, no quota).
4. Searches draw on a shared daily budget (``youtube_search_budget``),
   and back off once a pack has curated channels to draw on.

Finds from followed channels go through the same pipeline as any other:
validation, AI clean-up, the pack's policy and the review queue.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from .config import settings
from .db import session_scope, utcnow
from .enums import RuleKind
from .models import AppSetting, ChannelPack, DiscoveryRule, FollowedChannel, MediaItem, MediaSource

log = logging.getLogger(__name__)

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
_UC = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
SAMPLE_KEEP = 12
DEEP_HARVEST = 200  # videos listed from a newly followed channel's uploads (about 8 quota units)
REHARVEST = timedelta(days=7)  # in-depth relist of a followed channel's uploads
RSS_EVERY = timedelta(hours=1)
SEARCH_EVERY = timedelta(hours=12)  # a pack's search rule runs at most this often
CURATED_ENOUGH = 3  # with this many followed channels, a pack searches only weekly
_PACIFIC = ZoneInfo("America/Los_Angeles")  # YouTube quotas reset at midnight Pacific
_BUDGET_KEY = "_yt_search_budget"
# Filters carried from a pack's search rule to the channels it follows.
CARRY = ("min_duration", "max_duration", "excluded_keywords", "embeddable_only", "published_after", "published_before")


# ------------------------------------------------------------- search budget


def _today() -> str:
    return datetime.now(_PACIFIC).strftime("%Y-%m-%d")


def searches_used() -> int:
    with session_scope() as s:
        row = s.get(AppSetting, _BUDGET_KEY)
        day, _, n = (row.value if row else "").partition(":")
        return int(n) if day == _today() and n.isdigit() else 0


def searches_left() -> int:
    return max(0, int(settings.youtube_search_budget or 0) - searches_used())


def take_search() -> bool:
    """Spend one search from today's shared budget, if any is left."""
    with session_scope() as s:
        row = s.get(AppSetting, _BUDGET_KEY) or AppSetting(key=_BUDGET_KEY, value="")
        day, _, n = row.value.partition(":")
        used = int(n) if day == _today() and n.isdigit() else 0
        if used >= int(settings.youtube_search_budget or 0):
            return False
        row.value = f"{_today()}:{used + 1}"
        s.add(row)
        return True


def search_due(rule: DiscoveryRule, followed: int, now: datetime | None = None) -> bool:
    """A pack's YouTube search runs at most every 12 hours, and only weekly
    once the pack has enough curated channels to draw on."""
    now = now or utcnow()
    if rule.last_run_at is None:
        return True
    wait = REHARVEST if followed >= CURATED_ENOUGH else SEARCH_EVERY
    return now - rule.last_run_at >= wait


def is_channel_rule(rule) -> bool:
    return rule.provider == "youtube" and rule.kind == RuleKind.CHANNEL


def is_search_rule(rule) -> bool:
    return rule.provider == "youtube" and rule.kind == RuleKind.SEARCH


def followed_count(s, pack_id: int) -> int:
    return len(s.scalars(select(FollowedChannel.id).where(FollowedChannel.pack_id == pack_id,
                                                          FollowedChannel.status == "followed")).all())


# --------------------------------------------------------------- suggestions


async def note_search_finds(rule_id: int, item_ids: list[int]) -> list[int]:
    """Record the channels a pack's YouTube search found videos on, as
    suggestions for the admin. Returns new suggestion ids."""
    new: list[int] = []
    with session_scope() as s:
        rule = s.get(DiscoveryRule, rule_id)
        if rule is None or rule.pack_id is None or not is_search_rule(rule):
            return []
        by_channel: dict[str, list[MediaItem]] = {}
        for iid in item_ids:
            it = s.get(MediaItem, iid)
            cid = it.source.creator_id if it is not None and it.source is not None else None
            if cid and _UC.match(cid):
                by_channel.setdefault(cid, []).append(it)
        for cid, items in by_channel.items():
            fc = s.scalar(select(FollowedChannel).where(FollowedChannel.pack_id == rule.pack_id, FollowedChannel.channel_id == cid))
            if fc is None:
                fc = FollowedChannel(pack_id=rule.pack_id, channel_id=cid, title=items[0].source.creator, status="suggested",
                                     hits=0, samples=[], ai_verdict={}, added_by="search")
                s.add(fc)
                s.flush()
                new.append(fc.id)
            fc.hits += len(items)
            fc.samples = list(dict.fromkeys(list(fc.samples or []) + [i.title for i in items]))[-SAMPLE_KEEP:]
    for fid in new:
        await vet(fid)
    return new


async def vet(follow_id: int) -> dict | None:
    """Ask the AI backend whether a suggested channel fits its pack."""
    from . import ai

    if not ai.available():
        return None
    with session_scope() as s:
        fc = s.get(FollowedChannel, follow_id)
        if fc is None:
            return None
        pack = s.get(ChannelPack, fc.pack_id)
        theme = (pack.spec or {}).get("theme") or pack.name
        args = (theme, pack.name, fc.title or fc.channel_id, list(fc.samples or []))
    try:
        verdict = await ai.vet_channel(*args)
    except ai.AIError as e:
        verdict = {"fits": None, "reason": f"not checked: {e}"}
    with session_scope() as s:
        fc = s.get(FollowedChannel, follow_id)
        if fc is not None:
            fc.ai_verdict = verdict
    return verdict


# ------------------------------------------------------------- follow / add


def follow(s, fc: FollowedChannel) -> DiscoveryRule:
    """Follow a channel: a pack-managed channel rule harvests its uploads."""
    pack = s.get(ChannelPack, fc.pack_id)
    if fc.rule_id and (rule := s.get(DiscoveryRule, fc.rule_id)) is not None:
        rule.enabled = True
    else:
        source = next((r for r in pack.rules if is_search_rule(r)), None)
        params = {k: v for k, v in ((source.params or {}) if source else {}).items() if k in CARRY}
        params["follow_id"] = fc.id
        rule = DiscoveryRule(name=f"{pack.name}: {fc.title or fc.channel_id} (followed)"[:256], provider="youtube",
                             kind=RuleKind.CHANNEL, query=fc.channel_id, params=params, max_results=DEEP_HARVEST,
                             scan_interval_minutes=24 * 60, enabled=True)
        pack.rules.append(rule)
        s.add(rule)
        if pack.channel is not None:
            rule.channels = [pack.channel]
        s.flush()
        fc.rule_id = rule.id
    fc.status = "followed"
    fc.followed_at = utcnow()
    pack.last_fill_at = None  # harvest it on the next fill round
    return rule


def unfollow(s, fc: FollowedChannel, dismiss: bool = True) -> None:
    """Stop following (its finds already on air stay). Dismissed channels
    aren't suggested again."""
    if fc.rule_id and (rule := s.get(DiscoveryRule, fc.rule_id)) is not None:
        s.delete(rule)
    fc.rule_id = None
    fc.status = "dismissed" if dismiss else "suggested"


async def resolve(ref: str) -> tuple[str, str]:
    """(channel id, title) for an @handle, channel URL or UC id."""
    from .providers import ProviderError, get_provider
    from .providers.youtube import parse_youtube_url

    ref = (ref or "").strip()
    if _UC.match(ref):
        title = await _rss_title(ref)
        return ref, title or ref
    parsed = parse_youtube_url(ref)
    if not parsed or parsed[0] not in ("handle", "channel"):
        raise ValueError("paste a YouTube channel: @handle, youtube.com/@handle or youtube.com/channel/UC…")
    if parsed[0] == "channel":
        title = await _rss_title(parsed[1])
        return parsed[1], title or parsed[1]
    yt = get_provider("youtube")
    try:
        if getattr(yt, "unofficial", False):
            cid = await yt._unofficial_channel_id(parsed)
            return cid, (await _rss_title(cid)) or parsed[1]
        cid, title, _ = await yt.resolve_channel(parsed[1])  # 1 quota unit
        return cid, title
    except ProviderError as e:
        raise ValueError(str(e))


async def add(pack_id: int, ref: str) -> int:
    cid, title = await resolve(ref)
    with session_scope() as s:
        fc = s.scalar(select(FollowedChannel).where(FollowedChannel.pack_id == pack_id, FollowedChannel.channel_id == cid))
        if fc is None:
            fc = FollowedChannel(pack_id=pack_id, channel_id=cid, title=title, status="suggested", hits=0, samples=[],
                                 ai_verdict={}, added_by="admin")
            s.add(fc)
            s.flush()
        follow(s, fc)
        return fc.id


# ---------------------------------------------------------------------- RSS

_ATOM = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


async def _rss(channel_id: str) -> ET.Element | None:
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": settings.http_user_agent}) as c:
            r = await c.get(RSS_URL.format(channel_id))
        if r.status_code != 200 or len(r.content) > 2_000_000:
            return None
        return ET.fromstring(r.content)
    except (httpx.HTTPError, ET.ParseError):
        return None


async def _rss_title(channel_id: str) -> str | None:
    root = await _rss(channel_id)
    t = root.find("a:title", _ATOM) if root is not None else None
    return t.text if t is not None else None


async def rss_video_ids(channel_id: str) -> list[str]:
    """The channel's latest uploads (about 15), from its public feed: no key, no quota."""
    root = await _rss(channel_id)
    if root is None:
        return []
    return [v.text for v in root.findall("a:entry/yt:videoId", _ATOM) if v.text]


async def check_feeds(pack_id: int, force: bool = False) -> int:
    """New uploads from a pack's followed channels, via RSS. Returns new finds."""
    from .catalogue import is_known
    from .discovery import run_ids

    now = utcnow()
    with session_scope() as s:
        rows = s.scalars(select(FollowedChannel).where(FollowedChannel.pack_id == pack_id, FollowedChannel.status == "followed",
                                                       FollowedChannel.rule_id.is_not(None))).all()
        feeds = [(fc.id, fc.channel_id, fc.rule_id) for fc in rows
                 if force or fc.last_rss_at is None or now - fc.last_rss_at >= RSS_EVERY]
    new = 0
    for fid, cid, rid in feeds:
        ids = await rss_video_ids(cid)
        with session_scope() as s:
            fresh = [v for v in ids if not is_known(s, "youtube", v)]
            fc = s.get(FollowedChannel, fid)
            if fc is not None:
                fc.last_rss_at = now
            rule = s.get(DiscoveryRule, rid)
            params = dict(rule.params or {}) if rule else {}
        if fresh and rule is not None:
            res = await run_ids("youtube", fresh, params, rule_id=rid)
            new += res.new
    return new


def summary(s, pack_id: int) -> dict:
    rows = s.scalars(select(FollowedChannel).where(FollowedChannel.pack_id == pack_id)
                     .order_by(FollowedChannel.status, FollowedChannel.hits.desc())).all()
    harvested: dict[int, int] = {}
    rule_ids = [fc.rule_id for fc in rows if fc.rule_id]
    if rule_ids:
        from sqlalchemy import func

        harvested = dict(s.execute(select(MediaSource.discovered_by_rule_id, func.count())
                                   .where(MediaSource.discovered_by_rule_id.in_(rule_ids))
                                   .group_by(MediaSource.discovered_by_rule_id)).all())
    return {
        "channels": [{
            "id": fc.id, "channel_id": fc.channel_id, "title": fc.title, "status": fc.status, "hits": fc.hits,
            "samples": fc.samples or [], "ai_verdict": fc.ai_verdict or {}, "added_by": fc.added_by,
            "url": f"https://www.youtube.com/channel/{fc.channel_id}", "harvested": harvested.get(fc.rule_id, 0),
            "followed_at": fc.followed_at.isoformat() + "Z" if fc.followed_at else None,
        } for fc in rows],
        "searches_left_today": searches_left(), "search_budget": settings.youtube_search_budget,
    }
