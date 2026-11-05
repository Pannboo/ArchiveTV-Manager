"""Schedule-based catch-up, Start Over and historical seek.

Nothing is recorded. The published schedule is the archive: a request for a
past timestamp is resolved to the programme that aired then, and that
programme's original (still eligible) source is replayed from the matching
offset. Each request gets its own FFmpeg pipeline and playhead, so catch-up
viewers never disturb the shared live stream, and playback continues into the
following programmes for as long as the client asked for.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .catalogue import best_iptv_source, source_is_iptv_ok
from .config import settings
from .db import session_scope, utcnow
from .enums import EntryKind, ReviewStatus
from .models import CatchupSession, Channel, PlaybackEvent, PlaybackSource, ScheduleEntry
from .scheduler import current_entry, history_days
from .stream import SLATE_SEGMENT, ChannelStream, SegmentPlan, StreamLimitError, StreamManager
from .validator import playable_target

log = logging.getLogger(__name__)

MIN_DURATION = 60.0
MAX_DURATION = 12 * 3600.0
# Requests closer to "now" than this are treated as live rather than catch-up.
LIVE_EDGE = timedelta(seconds=30)
_SPLIT = re.compile(r"[-:_ T]")
# YYYY-MM-DD then ":", "_" or " ", then HH-MM / HH:MM with optional seconds.
_XC_LAYOUT = re.compile(r"\d{4}-\d{2}-\d{2}[:_ ]\d{2}([-:])\d{2}(\1\d{2})?")


class CatchupError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def parse_timestamp(value: str | int | float, tz: ZoneInfo | None = None) -> datetime:
    """Parse a catch-up start time into naive UTC.

    Accepts Unix epoch seconds, ISO 8601 (with or without offset) and the
    Xtream Codes layouts ``YYYY-MM-DD:HH-MM``, ``YYYY-MM-DD_HH-MM``,
    ``YYYY-MM-DD:HH-MM-SS`` and ``YYYY-MM-DD HH:MM:SS``. Times without an
    offset are interpreted in ``tz`` (default: the display timezone).
    """
    tz = tz or settings.timezone
    raw = str(value).strip()
    if not raw:
        raise CatchupError(400, "missing start time")
    if re.fullmatch(r"\d{9,11}(\.\d+)?", raw):
        return datetime.fromtimestamp(float(raw), timezone.utc).replace(tzinfo=None)
    # XC layouts are matched first: fromisoformat() would read the "-MM" of
    # "2026-11-05:05-06" as a UTC-06:00 offset.
    if _XC_LAYOUT.fullmatch(raw):
        try:
            dt = datetime(*(int(p) for p in _SPLIT.split(raw) if p))  # type: ignore[misc]
        except ValueError:
            raise CatchupError(400, f"invalid timestamp {raw!r}")
    else:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise CatchupError(400, f"unrecognised timestamp {raw!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass
class CatchupRequest:
    channel_id: int
    slug: str
    entry_id: int
    title: str
    start: datetime  # UTC schedule time playback begins at
    duration: float  # seconds of schedule time to play
    programme_start: datetime
    programme_end: datetime
    offset: float  # seconds into the first programme


def _entry_source(e: ScheduleEntry) -> PlaybackSource | None:
    """The source to replay an entry from, re-checked at playback time:
    the item must still be approved and the source still authorised,
    eligible and healthy (falling back to another eligible source)."""
    item = e.media_item
    if item is None or item.review_status != ReviewStatus.APPROVED:
        return None
    ps = e.playback_source
    if ps is not None and ps.live:
        return None  # live broadcasts are not recorded, so they can't be replayed
    if ps is not None and source_is_iptv_ok(ps):
        return ps
    alt = best_iptv_source(item)
    return None if alt is not None and alt.live else alt


def resolve_request(s, channel: Channel, start: datetime, duration: float | None = None,
                    now: datetime | None = None, snap_seconds: float = 0.0) -> CatchupRequest:
    """Validate a historical playback request before any stream is created.

    ``snap_seconds``: Dispatcharr truncates catch-up times to the minute, so a
    request a few seconds before a programme starts really means that
    programme; snap forward to it when one begins within this window.
    """
    now = now or utcnow()
    if snap_seconds > 0:
        here = current_entry(s, channel.id, start)
        if here is None or here.start != start:
            cands = s.scalars(select(ScheduleEntry).where(
                ScheduleEntry.channel_id == channel.id, ScheduleEntry.start > start,
                ScheduleEntry.start <= start + timedelta(seconds=snap_seconds), ScheduleEntry.kind != EntryKind.SLATE,
                ScheduleEntry.kind != EntryKind.IDENT).order_by(ScheduleEntry.start)).all()
            if cands:
                # Several programmes can start inside one minute (short clips).
                # Dispatcharr's duration is the guide length + 5 min, so prefer
                # the programme whose length matches it; otherwise the longest.
                def length(x):
                    return (x.end - x.start).total_seconds()
                if duration:
                    pick = min(cands, key=lambda x: abs(length(x) - max(duration - 300, 60)))
                else:
                    pick = max(cands, key=length)
                start = pick.start
    if not channel.enabled:
        raise CatchupError(404, "unknown channel")
    if not channel.catchup_enabled:
        raise CatchupError(403, "catch-up is disabled for this channel")
    earliest = now - timedelta(days=history_days(channel))
    if start < earliest:
        raise CatchupError(410, f"outside the {history_days(channel)}-day catch-up window")
    if start > now - LIVE_EDGE:
        raise CatchupError(416, "requested time is live or in the future")
    e = current_entry(s, channel.id, start)
    if e is None:
        raise CatchupError(404, "no programme was scheduled at that time")
    if e.kind == EntryKind.SLATE:
        raise CatchupError(404, "the channel was off air at that time")
    if e.playback_source is not None and e.playback_source.live:
        raise CatchupError(404, f"'{e.title}' was a live broadcast and cannot be replayed")
    ps = _entry_source(e)
    if ps is None:
        raise CatchupError(410, f"'{e.title}' is no longer available for playback")
    offset = (start - e.start).total_seconds()
    if offset > 1.0 and ps.seekable is False:
        raise CatchupError(409, f"the source of '{e.title}' cannot seek; request the programme start instead")
    if duration is None or duration <= 0:
        duration = (e.end - start).total_seconds()
    duration = min(max(duration, MIN_DURATION), MAX_DURATION)
    return CatchupRequest(channel.id, channel.slug, e.id, e.title, start, duration, e.start, e.end, max(0.0, offset))


def start_over_time(s, channel: Channel, now: datetime | None = None) -> datetime:
    """Start of the programme currently on air (for Start Over)."""
    now = now or utcnow()
    e = current_entry(s, channel.id, now)
    if e is None or e.kind == EntryKind.SLATE:
        raise CatchupError(404, "nothing is on air to start over")
    return e.start


class CatchupStream(ChannelStream):
    """A single-viewer stream whose clock runs from a past schedule time."""

    def __init__(self, manager: StreamManager, req: CatchupRequest, protocol: str = "direct", client: str | None = None):
        super().__init__(manager, req.channel_id)
        self.sid = uuid.uuid4().hex
        self.req = req
        self.protocol = protocol
        self.client = client
        self.v_end = req.start + timedelta(seconds=req.duration)
        self.real_start = utcnow()
        # The schedule clock is held at the requested time until the first
        # segment starts, so source resolution never eats into the programme.
        self._clock_running = False
        self.failed_entries: set[int] = set()
        self.error: str | None = None
        self.slug = f"{req.slug} catch-up {self.sid[:8]}"  # log label

    def _now(self) -> datetime:
        if not self._clock_running:
            return self.req.start
        return self.req.start + (utcnow() - self.real_start)

    def _before_play(self, plan: SegmentPlan) -> None:
        if not self._clock_running:
            self.real_start = utcnow()
            self._clock_running = True

    def remove_viewer(self, q: asyncio.Queue) -> None:
        # One viewer per session: stop as soon as the client goes away.
        self.viewers.discard(q)
        if not self.viewers and not self.finished:
            asyncio.ensure_future(self.stop())

    async def _plan(self) -> SegmentPlan | None:
        now = self._now()
        pos = self.emitted_until if self.emitted_until and self.emitted_until > now else now
        limit = (self.v_end - pos).total_seconds()
        if limit < 0.5:
            self._stopping = True
            return None
        with session_scope() as s:
            ch = s.get(Channel, self.channel_id)
            if ch is None:
                self._stopping = True
                return None
            self.channel_name = ch.name
            e = current_entry(s, ch.id, pos)
            if e is None:  # history gap or pruned: fill rather than end abruptly
                return SegmentPlan(None, EntryKind.SLATE, ch.name, pos, min(limit, SLATE_SEGMENT), 0.0)
            remaining = min((e.end - pos).total_seconds(), limit)
            if remaining < 0.5:
                self.emitted_until = e.end
                return None
            unavailable = e.kind != EntryKind.SLATE and (e.id in self.failed_entries or _entry_source(e) is None)
            if e.kind == EntryKind.SLATE or unavailable:
                label = f"{e.title} - unavailable" if unavailable else None
                return SegmentPlan(e.id, EntryKind.SLATE, e.title, pos, min(remaining, SLATE_SEGMENT), 0.0, label=label)
            offset = max(0.0, (pos - e.start).total_seconds())
            plan, snapshot = self._source_plan(ch, e, pos, remaining, offset)
        return await self._resolve(plan, snapshot)

    async def _handle_failure(self, plan: SegmentPlan, msg: str | None, mark_source: bool = True) -> None:
        """Log and skip the rest of the failed programme. History is never edited."""
        log.warning("%s: %s (%s)", self.slug, msg, plan.title)
        self.failures += 1
        self.error = msg
        with session_scope() as s:
            s.add(PlaybackEvent(channel_id=self.channel_id, playback_source_id=plan.source_id, level="warning",
                                message=f"catch-up '{plan.title}': {msg}"))
            if mark_source and plan.source_id:
                ps = s.get(PlaybackSource, plan.source_id)
                if ps:
                    ps.health_ok = False
                    ps.failure_count += 1
                    ps.last_error = f"catch-up playback: {msg}"[:500]
        if plan.entry_id:
            self.failed_entries.add(plan.entry_id)
        self._retry_now = True

    def _on_start(self) -> None:
        with session_scope() as s:
            s.add(CatchupSession(
                id=self.sid, channel_id=self.channel_id, entry_id=self.req.entry_id, title=self.req.title[:512],
                requested_start=self.req.start, duration=self.req.duration, protocol=self.protocol,
                client=(self.client or "")[:256] or None, started_at=self.real_start,
            ))

    def _on_finish(self) -> None:
        try:
            with session_scope() as s:
                row = s.get(CatchupSession, self.sid)
                if row:
                    row.ended_at = utcnow()
                    row.position = self.emitted_until
                    row.bytes_out = self.bytes_out
                    row.error = self.error
                    done = self.emitted_until is not None and self.emitted_until >= self.v_end - timedelta(seconds=2)
                    row.status = "completed" if done else ("failed" if self.error and not self.bytes_out else "stopped")
        except Exception:  # noqa: BLE001 - session history is best effort
            log.exception("could not record catch-up session end")

    def status(self) -> dict:
        d = super().status()
        d.update({
            "slug": self.req.slug, "session_id": self.sid, "protocol": self.protocol, "client": self.client,
            "requested_start": self.req.start.isoformat() + "Z", "duration": self.req.duration,
            "position": (self.emitted_until or self.req.start).isoformat() + "Z", "programme": self.req.title,
        })
        return d


async def preflight(req: CatchupRequest) -> None:
    """Confirm the first programme's source answers before streaming starts,
    so an unavailable programme fails with an HTTP error, not a slate."""
    with session_scope() as s:
        e = s.get(ScheduleEntry, req.entry_id)
        ps = _entry_source(e) if e else None
        snap = PlaybackSource(id=ps.id, playback_type=ps.playback_type, url=ps.url, local_path=ps.local_path,
                              allow_cache=ps.allow_cache) if ps else None
    if snap is None:
        raise CatchupError(410, f"'{req.title}' is no longer available for playback")
    try:
        await playable_target(snap, use_cache=True)
    except Exception as e:  # noqa: BLE001
        raise CatchupError(502, f"source for '{req.title}' is unavailable: {e}")


async def start_session(m: StreamManager, req: CatchupRequest, protocol: str = "direct",
                        client: str | None = None) -> tuple[CatchupStream, asyncio.Queue]:
    await preflight(req)
    async with m._lock:
        for sid in [k for k, v in m.catchups.items() if v.finished]:
            del m.catchups[sid]
        if len(m.catchups) >= settings.max_catchup_sessions:
            raise StreamLimitError(f"catch-up limit reached ({settings.max_catchup_sessions} sessions active)")
        st = CatchupStream(m, req, protocol, client)
        m.catchups[st.sid] = st
        q = st.add_viewer()
        st.start()
    log.info("catch-up %s: %s from %s for %.0fs (%s)", st.sid[:8], req.slug, req.start.isoformat(), req.duration, protocol)
    return st, q


async def stop_session(m: StreamManager, sid: str) -> bool:
    st = m.catchups.get(sid)
    if st is None or st.finished:
        return False
    await st.stop()
    return True
