"""Deterministic 24/7 schedule generation.

Schedules are generated ahead of time (default 7 days) and persisted, so the
guide and the stream agree and survive restarts. Generation is a pure function
of (channel config, eligible pool, previous entry's state): every entry stores
the generator state after it, so the schedule can be extended or rebuilt from
any point and produces the same result.

Invariants:
* entries are contiguous: entry[n].end == entry[n+1].start (no gaps/overlaps)
* only schedulable media (approved + authorised, compatible, healthy source)
  enters a schedule; when nothing is eligible a SLATE entry fills the time
* published entries are never rewritten unless they become ineligible or an
  administrator overrides/regenerates them
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .catalogue import best_iptv_source, is_schedulable, programme_duration, source_is_iptv_ok
from .config import settings
from .db import utcnow
from .enums import ChannelRole, EntryKind, ScheduleMode
from .models import Channel, ChannelBlock, MediaItem, PlaybackSource, ScheduleEntry, ScheduleVersion

SLATE_MAX = timedelta(hours=1)
PAD_MAX = timedelta(minutes=20)  # largest gap before a block boundary we try to pad with filler
GRACE = timedelta(seconds=90)  # entries starting sooner than this are left alone on rebuilds
MIN_DURATION = 1.0
RECENT_KEEP = 200


@dataclass
class Candidate:
    item: MediaItem
    source: PlaybackSource
    duration: float
    position: int

    @property
    def live(self) -> bool:
        return bool(self.source.live)


def _to_utc_naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def to_local(dt_utc: datetime) -> datetime:
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(settings.timezone)


def floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def block_intervals(blocks: list[ChannelBlock], around: datetime) -> list[tuple[datetime, datetime, ChannelBlock]]:
    """UTC intervals for every block occurrence from 1 day before to 2 days after ``around``."""
    tz = settings.timezone
    base = to_local(around).date()
    out = []
    for day_off in range(-1, 3):
        d = base + timedelta(days=day_off)
        for b in blocks:
            if str(d.weekday()) not in (b.days or "0123456"):
                continue
            st, en = _hm(b.start_time), _hm(b.end_time)
            start = datetime.combine(d, st, tzinfo=tz)
            end = datetime.combine(d if en > st else d + timedelta(days=1), en, tzinfo=tz)
            out.append((_to_utc_naive(start), _to_utc_naive(end), b))
    out.sort(key=lambda x: (x[0], x[2].id or 0))
    return out


def active_block(blocks: list[ChannelBlock], t: datetime) -> tuple[ChannelBlock | None, datetime | None]:
    """(block active at t, next time the active block changes)."""
    if not blocks:
        return None, None
    ivs = block_intervals(blocks, t)
    active = next((iv for iv in ivs if iv[0] <= t < iv[1]), None)
    boundaries = sorted({x for iv in ivs for x in (iv[0], iv[1]) if x > t})
    nxt = boundaries[0] if boundaries else None
    return (active[2] if active else None), nxt


def _matches(item: MediaItem, tag_filter: str | None) -> bool:
    if not tag_filter:
        return True
    wanted = {t.strip().lower() for t in tag_filter.split(",") if t.strip()}
    have = {str(t).lower() for t in (item.tags or [])} | {str(c).lower() for c in (item.categories or [])}
    if item.series:
        have.add(item.series.lower())
    return bool(wanted & have)


class Generator:
    def __init__(self, s: Session, channel: Channel):
        self.s = s
        self.ch = channel
        self.pools: dict[str, list[Candidate]] = {r: [] for r in ChannelRole}
        for ci in channel.items:
            item = ci.media_item
            if item is None or not is_schedulable(item):
                continue
            ps = best_iptv_source(item)
            dur = programme_duration(item, ps)
            if ps is None or dur is None or dur < MIN_DURATION:
                continue
            self.pools[ci.role].append(Candidate(item, ps, dur, ci.position))
        for pool in self.pools.values():
            pool.sort(key=lambda c: (c.position, c.item.id))
        self.fallback: Candidate | None = None
        fb = channel.fallback_item
        if fb is not None and is_schedulable(fb):
            ps = best_iptv_source(fb)
            self.fallback = Candidate(fb, ps, programme_duration(fb, ps), 0)  # type: ignore[arg-type]
        self.blocks = list(channel.blocks)

    @property
    def has_programmes(self) -> bool:
        return bool(self.pools[ChannelRole.PROGRAMME]) or self.fallback is not None

    # -- selection -----------------------------------------------------------

    def _sequential(self, pool: list[Candidate], state: dict, key: str) -> Candidate:
        seq = state.setdefault("seq", {})
        last = seq.get(key)
        pick = pool[0]
        if last:
            ids = [c.item.id for c in pool]
            if last.get("id") in ids:
                pick = pool[(ids.index(last["id"]) + 1) % len(pool)]
            else:
                pick = next((c for c in pool if c.position > last.get("pos", -1)), pool[0])
        seq[key] = {"id": pick.item.id, "pos": pick.position}
        return pick

    def _shuffle(self, pool: list[Candidate], state: dict) -> Candidate:
        state["draws"] = int(state.get("draws", 0)) + 1
        rng = random.Random(f"{self.ch.seed}:{self.ch.id}:{state['draws']}")
        recent = list(state.get("recent", []))
        k = max(0, min(self.ch.repeat_avoid_count, len(pool) - 1))
        avoid = set(recent[-k:]) if k else set()
        candidates = [c for c in pool if c.item.id not in avoid] or pool
        return rng.choice(candidates)

    def _cycle(self, pool: list[Candidate], state: dict, key: str) -> Candidate:
        idx = int(state.get(key, 0))
        state[key] = idx + 1
        return pool[idx % len(pool)]

    # -- generation ------------------------------------------------------------

    def next_entries(self, t: datetime, state: dict) -> list[ScheduleEntry]:
        block, boundary = active_block(self.blocks, t)
        programmes = self.pools[ChannelRole.PROGRAMME]
        if block is not None:
            pool = [c for c in programmes if _matches(c.item, block.tag_filter)] or programmes
            mode = block.mode
            key = f"block:{block.id}"
        else:
            pool = programmes
            mode = self.ch.mode if self.ch.mode != ScheduleMode.BLOCK else ScheduleMode.SHUFFLE
            key = "main"

        # Pad to an approaching block boundary with filler clips that fit.
        if boundary is not None and self.ch.filler_pad and self.pools[ChannelRole.FILLER]:
            gap = (boundary - t).total_seconds()
            if 0 < gap <= PAD_MAX.total_seconds():
                fits = [c for c in self.pools[ChannelRole.FILLER] if c.duration <= gap]
                if fits:
                    c = max(fits, key=lambda c: (c.duration, -c.item.id))
                    return [self._entry(t, c, EntryKind.FILLER, block, state)]

        if not pool:
            if self.fallback is not None:
                return [self._entry(t, self.fallback, EntryKind.FALLBACK, block, state)]
            end = t + SLATE_MAX
            if boundary is not None and boundary < end:
                end = boundary
            return [
                ScheduleEntry(
                    channel_id=self.ch.id,
                    start=t,
                    end=end,
                    kind=EntryKind.SLATE,
                    title=f"{self.ch.name} — off air",
                    description="No approved, IPTV-eligible programmes are available for this channel.",
                    block_name=block.name if block else None,
                    state=dict(state),
                )
            ]

        pick = self._sequential(pool, state, key) if mode == ScheduleMode.SEQUENTIAL else self._shuffle(pool, state)
        recent = list(state.get("recent", [])) + [pick.item.id]
        state["recent"] = recent[-RECENT_KEEP:]
        out = [self._entry(t, pick, EntryKind.PROGRAMME, block, state, boundary)]
        if self.ch.idents_between and self.pools[ChannelRole.IDENT]:
            ident = self._cycle(self.pools[ChannelRole.IDENT], state, "ident_idx")
            out.append(self._entry(out[-1].end, ident, EntryKind.IDENT, block, state))
        return out

    def _entry(self, t: datetime, c: Candidate, kind: EntryKind, block: ChannelBlock | None, state: dict,
               boundary: datetime | None = None) -> ScheduleEntry:
        end = t + timedelta(seconds=c.duration)
        if c.live and boundary is not None and t < boundary < end:
            end = boundary  # a live slot runs to the end of its block (or the next block start)
        return ScheduleEntry(
            channel_id=self.ch.id,
            start=t,
            end=end,
            kind=kind,
            media_item_id=c.item.id,
            playback_source_id=c.source.id,
            title=c.item.title,
            description=c.item.description,
            block_name=block.name if block else None,
            state=_copy_state(state),
        )


def _copy_state(state: dict) -> dict:
    return {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in state.items()}


def last_entry(s: Session, channel_id: int) -> ScheduleEntry | None:
    return s.scalar(select(ScheduleEntry).where(ScheduleEntry.channel_id == channel_id).order_by(ScheduleEntry.start.desc()).limit(1))


RETENTION_CHOICES = (1, 3, 7, 14, 30)


def history_days(channel: Channel) -> int:
    """Days of programme history kept for a channel (never less than one)."""
    return max(1, min(channel.catchup_days or settings.catchup_days, max(RETENTION_CHOICES)))


def current_entry(s: Session, channel_id: int, now: datetime | None = None) -> ScheduleEntry | None:
    now = now or utcnow()
    return s.scalar(
        select(ScheduleEntry)
        .where(ScheduleEntry.channel_id == channel_id, ScheduleEntry.start <= now, ScheduleEntry.end > now)
        .order_by(ScheduleEntry.start.desc())
        .limit(1)
    )


def extend(s: Session, channel: Channel, now: datetime | None = None, days: int | None = None, gen: Generator | None = None) -> int:
    """Append entries until the schedule reaches now + days. Returns entries added."""
    now = now or utcnow()
    horizon = now + timedelta(days=settings.schedule_days if days is None else days)
    last = last_entry(s, channel.id)
    if last is None:
        t, state = floor_hour(now), {}
    else:
        t, state = max(last.end, floor_hour(now)), _copy_state(last.state or {})
    if t >= horizon:
        return 0
    gen = gen or Generator(s, channel)
    added = 0
    while t < horizon:
        for e in gen.next_entries(t, state):
            s.add(e)
            t = e.end
            added += 1
    s.flush()
    return added


def truncate_from(s: Session, channel: Channel, cut: datetime) -> int:
    """Delete entries starting at or after ``cut``. Returns how many."""
    n = s.execute(delete(ScheduleEntry).where(ScheduleEntry.channel_id == channel.id, ScheduleEntry.start >= cut)).rowcount or 0
    s.flush()
    return n


def record_version(s: Session, channel: Channel, reason: str, changed_from: datetime | None, added: int = 0,
                   removed: int = 0, actor: str = "system", note: str | None = None) -> ScheduleVersion | None:
    """Append to the channel's publication history. Routine extensions are
    merged into the day's existing "extend" row to keep the table small."""
    if not added and not removed and reason == "extend":
        return None
    now = utcnow()
    horizon = s.scalar(select(func.max(ScheduleEntry.end)).where(ScheduleEntry.channel_id == channel.id))
    if reason == "extend":
        last = s.scalar(select(ScheduleVersion).where(ScheduleVersion.channel_id == channel.id)
                        .order_by(ScheduleVersion.id.desc()).limit(1))
        if last is not None and last.reason == "extend" and last.created_at.date() == now.date():
            last.entries_added += added
            last.horizon, last.updated_at = horizon, now
            return last
    v = ScheduleVersion(channel_id=channel.id, created_at=now, updated_at=now, reason=reason, actor=actor,
                        changed_from=changed_from, horizon=horizon, entries_added=added, entries_removed=removed, note=note)
    s.add(v)
    return v


def entry_still_valid(e: ScheduleEntry, s: Session) -> bool:
    if e.kind == EntryKind.SLATE:
        return True
    if e.media_item_id is None:
        return False
    item = s.get(MediaItem, e.media_item_id)
    if item is None or not is_schedulable(item):
        return False
    ps = s.get(PlaybackSource, e.playback_source_id) if e.playback_source_id else None
    if ps is not None and source_is_iptv_ok(ps):
        return True
    # Another eligible source with the same running time can stand in.
    alt = best_iptv_source(item)
    dur = (e.end - e.start).total_seconds()
    if alt is not None and abs((programme_duration(item, alt) or 0) - dur) <= 1.0:
        e.playback_source_id = alt.id
        return True
    return False


def maintain(s: Session, channel: Channel, now: datetime | None = None) -> dict:
    """Validate future entries, rebuild from the first invalid one, extend to horizon."""
    now = now or utcnow()
    if not channel.enabled:
        return {"rebuilt_from": None, "added": 0}
    gen = Generator(s, channel)
    cur = current_entry(s, channel.id, now)
    if cur is not None and cur.kind == EntryKind.SLATE and gen.has_programmes:
        # Content became available while the channel is "off air": end the
        # slate now rather than at its scheduled end.
        cur.end = now
        s.flush()
        n = truncate_from(s, channel, now)
        record_version(s, channel, "off_air_ended", now, removed=n, note="eligible content became available")
    cutoff = now + GRACE
    future = s.scalars(
        select(ScheduleEntry).where(ScheduleEntry.channel_id == channel.id, ScheduleEntry.start >= cutoff).order_by(ScheduleEntry.start)
    ).all()
    rebuild_at: datetime | None = None
    for e in future:
        if e.kind == EntryKind.SLATE and gen.has_programmes:
            rebuild_at = e.start  # content became available
            break
        if not entry_still_valid(e, s):
            rebuild_at = e.start
            break
    removed = truncate_from(s, channel, rebuild_at) if rebuild_at is not None else 0
    added = extend(s, channel, now, gen=gen)
    if rebuild_at is not None:
        record_version(s, channel, "rebuild", rebuild_at, added, removed, note="upcoming entry no longer eligible")
    else:
        record_version(s, channel, "extend", None, added)
    # Historical entries are the catch-up archive: keep them for the retention period.
    prune_before = now - timedelta(days=history_days(channel))
    s.execute(delete(ScheduleEntry).where(ScheduleEntry.channel_id == channel.id, ScheduleEntry.end < prune_before))
    return {"rebuilt_from": rebuild_at, "added": added}


def regenerate(s: Session, channel: Channel, now: datetime | None = None, actor: str = "admin") -> int:
    """Discard upcoming entries (keeping what is on air now) and rebuild."""
    now = now or utcnow()
    cur = current_entry(s, channel.id, now)
    cut = cur.end if cur else now
    removed = truncate_from(s, channel, cut)
    added = extend(s, channel, now)
    record_version(s, channel, "regenerate", cut, added, removed, actor=actor)
    return added


def override(s: Session, channel: Channel, entry_id: int, item_id: int, now: datetime | None = None,
             actor: str = "admin") -> ScheduleEntry:
    """Replace an upcoming entry with a specific item; later entries are rebuilt."""
    now = now or utcnow()
    e = s.get(ScheduleEntry, entry_id)
    if e is None or e.channel_id != channel.id:
        raise ValueError("schedule entry not found")
    if e.start < now + GRACE:
        raise ValueError("cannot override a programme that is on air or about to start")
    item = s.get(MediaItem, item_id)
    if item is None or not is_schedulable(item):
        raise ValueError("item is not approved with an authorised, eligible playback source")
    ps = best_iptv_source(item)
    start = e.start
    prev = s.scalar(
        select(ScheduleEntry).where(ScheduleEntry.channel_id == channel.id, ScheduleEntry.start < start).order_by(ScheduleEntry.start.desc()).limit(1)
    )
    state = _copy_state(prev.state) if prev and prev.state else {}
    replaced = e.title
    removed = truncate_from(s, channel, start)
    new = ScheduleEntry(
        channel_id=channel.id,
        start=start,
        end=start + timedelta(seconds=programme_duration(item, ps)),  # type: ignore[arg-type]
        kind=EntryKind.PROGRAMME,
        media_item_id=item.id,
        playback_source_id=ps.id,  # type: ignore[union-attr]
        title=item.title,
        description=item.description,
        overridden=True,
        state=state,
    )
    s.add(new)
    s.flush()
    added = extend(s, channel, now) + 1
    record_version(s, channel, "override", start, added, removed, actor=actor, note=f"'{replaced}' replaced by '{item.title}'")
    return new


def check_continuity(entries: list[ScheduleEntry]) -> list[str]:
    problems = []
    for a, b in zip(entries, entries[1:]):
        if b.start > a.end:
            problems.append(f"gap {a.end} -> {b.start}")
        elif b.start < a.end:
            problems.append(f"overlap {b.start} < {a.end}")
    return problems
