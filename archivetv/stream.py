"""On-demand IPTV stream engine.

One ChannelStream per channel is shared by every viewer of that channel. It
walks the persisted schedule, running one FFmpeg process per programme
segment that writes MPEG-TS to a pipe. Timestamps stay continuous across
programme transitions via ``-output_ts_offset`` (running total of media
emitted), and PIDs/service ids are fixed so the output looks like a single
continuous transport stream to Dispatcharr.

Position is derived from the wall clock and the persisted schedule, so after
a restart a channel resumes exactly where the guide says it should be.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import resource
import signal
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .catalogue import best_iptv_source, source_is_iptv_ok
from sqlalchemy.exc import OperationalError

from .config import settings
from .db import session_scope, utcnow
from .enums import EntryKind, Eligibility, PlaybackFormat, PlaybackType
from .models import Channel, PlaybackEvent, PlaybackSource, ScheduleEntry, StreamSession
from .scheduler import current_entry, extend, maintain, record_version, truncate_from
from .security import FFMPEG_PROTOCOLS
from . import ytstream
from .validator import forget_target, playable_target

log = logging.getLogger(__name__)

TS_PACKET = 188
READ_SIZE = TS_PACKET * 348  # ~64 KiB
QUEUE_CHUNKS = 512  # per-viewer backlog (~32 MiB) before a slow viewer is dropped
GOP_CACHE_BYTES = 24 * 1024 * 1024  # cap on output kept since the last keyframe
VIDEO_PID, AUDIO_PID, PMT_PID = 256, 257, 4096
# MPEG-TS muxer start offset (-muxdelay + -muxpreload, set explicitly below).
MUX_START = 1.4
PTS_WRAP = 2**33 / 90000
# Gap left after the last emitted PTS so a new segment's first packets never
# land before the previous segment's last ones (covers a multi-frame audio PES).
SEGMENT_TS_MARGIN = 0.2
RESOLVE_ATTEMPTS = 3
TARGET_LEAD = 3.0  # seconds of output kept ahead of real time
SHORTFALL_TOLERANCE = 3.0
SLATE_SEGMENT = 30.0
REPLACEMENT_HORIZON_DAYS = 0.25
MAX_PREROLL = 30.0  # cap on keyframe pre-roll compensation (very long GOPs)
# FFmpeg failures worth retrying before failing over (busy mirrors, throttling, blips).
TRANSIENT = re.compile(r"5XX|Server Error|429|Too Many Requests|timed out|Connection reset|Connection timed|Broken pipe", re.I)
TRANSIENT_RETRIES = 2
PID_FILE = "ffmpeg.pids"


@dataclass
class SegmentPlan:
    entry_id: int | None
    kind: str
    title: str
    sched_start: datetime  # schedule time this segment begins at
    duration: float  # seconds of schedule time it covers
    offset: float  # seek offset into the media
    source_id: int | None = None
    target: str | None = None
    copy: bool = False  # remux video
    copy_audio: bool = False  # remux audio too (only in explicit "copy" mode)
    has_audio: bool = True
    label: str | None = None  # slate caption override
    preroll: float = 0.0  # seconds a remux starts before ``offset`` (previous keyframe)
    keyframe_aligned: bool = False  # remux starts the output clock at that keyframe
    audio_target: str | None = None  # separate audio input (YouTube serves video and audio apart)
    user_agent: str | None = None  # per-source HTTP user agent override
    source_url: str | None = None
    live: bool = False  # live source: join at the live edge, no seek, no pacing


def _sanitize_text(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9 .,!?&()-]", "", s)[:60]
    return s.replace(":", " ")


class StreamManager:
    def __init__(self) -> None:
        self.streams: dict[int, ChannelStream] = {}
        self.catchups: dict[str, ChannelStream] = {}  # catch-up sessions by id (see catchup.py)
        self._lock = asyncio.Lock()
        self._drawtext: bool | None = None
        self._pids: set[int] = set()

    # -- process bookkeeping ---------------------------------------------------

    @property
    def pid_file(self) -> Path:
        return settings.data_dir / "run" / PID_FILE

    def _write_pids(self) -> None:
        try:
            self.pid_file.parent.mkdir(parents=True, exist_ok=True)
            self.pid_file.write_text("\n".join(str(p) for p in sorted(self._pids)))
        except OSError:
            pass

    def register_pid(self, pid: int) -> None:
        self._pids.add(pid)
        self._write_pids()

    def unregister_pid(self, pid: int) -> None:
        self._pids.discard(pid)
        self._write_pids()

    def cleanup_orphans(self) -> int:
        """Kill FFmpeg processes left behind by a previous crash."""
        killed = 0
        try:
            pids = [int(x) for x in self.pid_file.read_text().split() if x.strip().isdigit()]
        except OSError:
            return 0
        for pid in pids:
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                continue
            if b"ffmpeg" in cmdline and b"archivetv" in cmdline:
                try:
                    os.killpg(pid, signal.SIGKILL)
                    killed += 1
                except OSError:
                    pass
        self.pid_file.unlink(missing_ok=True)
        return killed

    async def drawtext_available(self) -> bool:
        if self._drawtext is None:
            try:
                p = await asyncio.create_subprocess_exec(
                    settings.ffmpeg_path, "-hide_banner", "-filters", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
                )
                out, _ = await p.communicate()
                self._drawtext = b"drawtext" in out
            except OSError:
                self._drawtext = False
        return self._drawtext

    # -- streams ---------------------------------------------------------------

    async def subscribe(self, channel_id: int) -> tuple["ChannelStream", asyncio.Queue]:
        async with self._lock:
            st = self.streams.get(channel_id)
            if st is None or st.finished:
                active = [s for s in self.streams.values() if not s.finished]
                if len(active) >= settings.max_streams:
                    raise StreamLimitError(f"stream limit reached ({settings.max_streams} channels active)")
                st = ChannelStream(self, channel_id)
                self.streams[channel_id] = st
                st.start()
            q = st.add_viewer()
            return st, q

    def status(self) -> list[dict]:
        return [s.status() for s in self.streams.values() if not s.finished]

    def catchup_status(self) -> list[dict]:
        return [s.status() for s in self.catchups.values() if not s.finished]

    def ffmpeg_processes(self) -> int:
        return len(self._pids)

    async def stop_all(self) -> None:
        for st in list(self.streams.values()) + list(self.catchups.values()):
            await st.stop()
        self.streams.clear()
        self.catchups.clear()


class StreamLimitError(Exception):
    pass


class ChannelStream:
    def __init__(self, manager: StreamManager, channel_id: int):
        self.m = manager
        self.channel_id = channel_id
        self.viewers: set[asyncio.Queue] = set()
        # Output since the most recent video keyframe, replayed (after a fresh
        # PAT/PMT) to viewers who join mid-stream so they can decode at once.
        self.gop: list[bytes] = []
        self.gop_bytes = 0
        self.psi: dict[int, bytes] = {}
        self.task: asyncio.Task | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.finished = False
        self.started_at = utcnow()
        self.ts_offset = 0.0
        self.seg_max_pts: float | None = None
        self.emitted_until: datetime | None = None
        self.current: SegmentPlan | None = None
        self.bytes_out = 0
        self._idle_handle: asyncio.TimerHandle | None = None
        self._stopping = False
        self._retry_now = False
        self.channel_name = ""
        self.slug = ""
        self.peak_viewers = 0
        self.failures = 0
        self._db_down = False
        self._transient: dict[int | None, int] = {}  # entry id -> consecutive transient failures
        self.session_id: int | None = None

    def _now(self) -> datetime:
        """Schedule time the output should currently be at (wall clock for live)."""
        return utcnow()

    # -- viewers ---------------------------------------------------------------

    def add_viewer(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_CHUNKS)
        if self.gop and 0 in self.psi and PMT_PID in self.psi:
            q.put_nowait(self.psi[0] + self.psi[PMT_PID])
            for chunk in self.gop[: QUEUE_CHUNKS - 8]:
                q.put_nowait(chunk)
        self.viewers.add(q)
        self.peak_viewers = max(self.peak_viewers, len(self.viewers))
        if self._idle_handle:
            self._idle_handle.cancel()
            self._idle_handle = None
        return q

    def remove_viewer(self, q: asyncio.Queue) -> None:
        self.viewers.discard(q)
        if not self.viewers and not self.finished:
            loop = asyncio.get_running_loop()
            if self._idle_handle:
                self._idle_handle.cancel()
            self._idle_handle = loop.call_later(settings.stream_idle_seconds, lambda: asyncio.ensure_future(self._idle_stop()))

    async def _idle_stop(self) -> None:
        if not self.viewers:
            log.info("channel %s idle, stopping stream", self.slug)
            await self.stop()

    def _track_gop(self, chunk: bytes) -> None:
        key_at = None
        for off in range(0, len(chunk), TS_PACKET):
            b1, b2, b3 = chunk[off + 1], chunk[off + 2], chunk[off + 3]
            pid = ((b1 & 0x1F) << 8) | b2
            if pid in (0, PMT_PID) and b1 & 0x40:
                self.psi[pid] = chunk[off : off + TS_PACKET]
            elif pid in (VIDEO_PID, AUDIO_PID):
                if pid == VIDEO_PID and (b3 & 0x20) and chunk[off + 4] > 0 and chunk[off + 5] & 0x40:
                    key_at = off  # adaptation field random_access_indicator
                if b1 & 0x40:
                    self._note_pts(chunk, off, b3)
        if key_at is not None:
            tail = chunk[key_at:]
            self.gop, self.gop_bytes = [tail], len(tail)
        elif self.gop:
            self.gop.append(chunk)
            self.gop_bytes += len(chunk)
            if self.gop_bytes > GOP_CACHE_BYTES:
                self.gop, self.gop_bytes = [], 0

    def _note_pts(self, chunk: bytes, off: int, b3: int) -> None:
        """Track the highest PES PTS emitted in the current segment."""
        start = off + 4 + ((1 + chunk[off + 4]) if b3 & 0x20 else 0)
        h = chunk[start : start + 14]
        if len(h) < 14 or h[0:3] != b"\x00\x00\x01" or not (h[7] & 0x80):
            return
        pts = ((h[9] >> 1) & 7) << 30 | h[10] << 22 | (h[11] >> 1) << 15 | h[12] << 7 | h[13] >> 1
        secs = pts / 90000
        base = self.ts_offset + MUX_START
        secs += round((base - secs) / PTS_WRAP) * PTS_WRAP  # unwrap 33-bit PTS
        if self.seg_max_pts is None or secs > self.seg_max_pts:
            self.seg_max_pts = secs

    def _broadcast(self, chunk: bytes) -> None:
        self._track_gop(chunk)
        self.bytes_out += len(chunk)
        for q in list(self.viewers):
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                # Slow viewer: drop it rather than stall everyone else.
                self.viewers.discard(q)
                _drain_and_close(q)

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        self._on_start()
        self.task = asyncio.create_task(self._run(), name=f"stream-{self.channel_id}")

    def _before_play(self, plan: SegmentPlan) -> None:
        """Hook run just before each segment's FFmpeg process starts."""

    def _on_start(self) -> None:
        try:
            with session_scope() as s:
                row = StreamSession(channel_id=self.channel_id, started_at=self.started_at)
                s.add(row)
                s.flush()
                self.session_id = row.id
        except Exception:  # noqa: BLE001 - session history is best effort
            log.exception("could not record stream session")

    def _on_finish(self) -> None:
        if self.session_id is None:
            return
        try:
            with session_scope() as s:
                row = s.get(StreamSession, self.session_id)
                if row:
                    row.ended_at = utcnow()
                    row.peak_viewers, row.bytes_out, row.failures = self.peak_viewers, self.bytes_out, self.failures
        except Exception:  # noqa: BLE001
            log.exception("could not record stream session end")

    async def stop(self) -> None:
        self._stopping = True
        await self._kill_proc()
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        self._finish()

    def _finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        self._on_finish()
        for q in list(self.viewers):
            _drain_and_close(q)
        self.viewers.clear()

    async def _kill_proc(self) -> None:
        p = self.proc
        if p and p.returncode is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                await asyncio.wait_for(p.wait(), 3)
            except asyncio.TimeoutError:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                await p.wait()

    def status(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "slug": self.slug,
            "name": self.channel_name,
            "viewers": len(self.viewers),
            "started_at": self.started_at.isoformat() + "Z",
            "now_playing": self.current.title if self.current else None,
            "segment_kind": self.current.kind if self.current else None,
            "mode": ("copy" if self.current.copy else "transcode") if self.current else None,
            "bytes_out": self.bytes_out,
            "pid": self.proc.pid if self.proc and self.proc.returncode is None else None,
        }

    async def _run(self) -> None:
        try:
            while not self._stopping:
                try:
                    plan = await self._plan()
                except OperationalError as e:
                    # Database unavailable: keep viewers on a valid stream
                    # (an "off air" card) and retry instead of dropping them.
                    if not self._db_down:
                        log.error("channel %s: database unavailable, holding on slate (%s)", self.slug, e.orig)
                    self._db_down = True
                    now = self._now()
                    pos = self.emitted_until if self.emitted_until and self.emitted_until > now else now
                    plan = SegmentPlan(None, EntryKind.SLATE, self.channel_name, pos, 5.0, 0.0,
                                       label=f"{self.channel_name} - back soon")
                else:
                    if self._db_down:
                        log.info("channel %s: database available again", self.slug)
                    self._db_down = False
                if plan is None:
                    if self._retry_now:
                        self._retry_now = False
                    else:
                        await asyncio.sleep(0.5)
                    continue
                self.current = plan
                self.seg_max_pts = None
                self._before_play(plan)
                produced, ok, err = await self._play(plan)
                if self.seg_max_pts is not None:
                    # Continue timestamps from what was actually emitted, which
                    # also absorbs keyframe pre-roll when remuxing mid-programme.
                    self.ts_offset = max(self.ts_offset, self.seg_max_pts - MUX_START + SEGMENT_TS_MARGIN)
                else:
                    self.ts_offset += produced
                self.emitted_until = plan.sched_start + timedelta(seconds=produced)
                if self._stopping:
                    break
                if not ok and produced < 0.5 and err and TRANSIENT.search(err) and plan.source_id:
                    n = self._transient.get(plan.entry_id, 0) + 1
                    self._transient[plan.entry_id] = n
                    if n <= TRANSIENT_RETRIES:
                        log.info("channel %s: transient source error, retry %d/%d (%s)", self.slug, n, TRANSIENT_RETRIES, err[:120])
                        forget_target(plan.source_id)  # re-resolve: the redirect may pick another mirror
                        await asyncio.sleep(1.0 * n)
                        continue
                elif ok:
                    self._transient.pop(plan.entry_id, None)
                try:
                    if not ok:
                        await self._handle_failure(plan, err)
                    elif plan.kind != EntryKind.SLATE and produced + SHORTFALL_TOLERANCE < plan.duration:
                        await self._handle_failure(plan, f"source ended {plan.duration - produced:.0f}s early", mark_source=False)
                except OperationalError as e:
                    log.error("channel %s: could not record failure, database unavailable (%s)", self.slug, e.orig)
                    self._retry_now = True
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("stream loop for channel %s crashed", self.channel_id)
        finally:
            await self._kill_proc()
            self._finish()

    async def _plan(self) -> SegmentPlan | None:
        now = utcnow()
        # Continue from where output got to, unless we fell behind real time.
        pos = self.emitted_until if self.emitted_until and self.emitted_until > now else now
        with session_scope() as s:
            ch = s.get(Channel, self.channel_id)
            if ch is None or not ch.enabled:
                self._stopping = True
                return None
            self.channel_name, self.slug = ch.name, ch.slug
            e = current_entry(s, ch.id, pos)
            if e is None or e.kind == EntryKind.SLATE:
                # Also re-check while off air, so newly eligible content airs promptly.
                maintain(s, ch, now)
                e = current_entry(s, ch.id, pos)
            if e is None:
                return SegmentPlan(None, EntryKind.SLATE, f"{ch.name}", pos, 10.0, 0.0)
            offset = max(0.0, (pos - e.start).total_seconds())
            remaining = (e.end - pos).total_seconds()
            if remaining < 0.5:
                self.emitted_until = e.end
                return None
            if e.kind == EntryKind.SLATE:
                # Short slate segments so newly eligible content is picked up quickly.
                return SegmentPlan(e.id, e.kind, e.title, pos, min(remaining, SLATE_SEGMENT), 0.0)
            plan, snapshot = self._source_plan(ch, e, pos, remaining, offset)
        return await self._resolve(plan, snapshot)

    def _source_plan(self, ch: Channel, e: ScheduleEntry, pos: datetime, remaining: float, offset: float):
        """Pick the playback source for an entry. Must run inside a DB session."""
        ps = e.playback_source
        if ps is None or not source_is_iptv_ok(ps):
            ps = best_iptv_source(e.media_item) if e.media_item else None
        if ps is None:
            plan = SegmentPlan(e.id, e.kind, e.title, pos, remaining, offset)
            plan.kind = "missing"
            return plan, None
        fmt = ch.playback_format
        copy = fmt == PlaybackFormat.COPY or (fmt == PlaybackFormat.AUTO and ps.eligibility == Eligibility.IPTV_READY)
        plan = SegmentPlan(
            e.id, e.kind, e.title, pos, remaining, offset, source_id=ps.id, copy=copy,
            copy_audio=copy and fmt == PlaybackFormat.COPY, has_audio=ps.audio_codec is not None,
        )
        snapshot = PlaybackSource(
            id=ps.id, playback_type=ps.playback_type, url=ps.url, local_path=ps.local_path, allow_cache=ps.allow_cache
        )
        plan.source_url = ps.url
        plan.live = bool(ps.live)
        if plan.live:
            plan.offset = 0.0
        return plan, snapshot

    async def _resolve(self, plan: SegmentPlan, ps_snapshot: PlaybackSource | None) -> SegmentPlan | None:
        if plan.kind == "missing" or ps_snapshot is None:
            return await self._fail_plan(plan, "no eligible playback source")
        for attempt in range(RESOLVE_ATTEMPTS):
            try:
                plan.target = await playable_target(ps_snapshot, use_cache=True)
                if ps_snapshot.playback_type == PlaybackType.YOUTUBE_STREAM:
                    pair = ytstream.companion(plan.target)
                    if pair:
                        plan.audio_target, plan.user_agent = pair.audio, pair.user_agent
                break
            except Exception as e:  # noqa: BLE001 - any resolution failure is a source failure
                if attempt == RESOLVE_ATTEMPTS - 1 or ps_snapshot.playback_type == "local_file":
                    return await self._fail_plan(plan, f"source unavailable: {e}")
                log.info("channel %s: retrying source (%s)", self.slug, e)
                await asyncio.sleep(1.5)
        # Retries consumed wall-clock time; keep position aligned with the guide.
        late = (self._now() - plan.sched_start).total_seconds()
        if late > 1.0 and plan.kind != EntryKind.SLATE:
            plan.offset += late
            plan.duration -= late
            plan.sched_start = self._now()
            if plan.duration < 0.5:
                return None
        # A remux joined mid-programme starts on the previous keyframe. Once
        # earlier output exists those pre-roll packets would land behind it,
        # so find the keyframe and shift the segment's clock to compensate.
        # On a stream's first segment nothing precedes them; only cheap
        # (local) sources are probed then, so remote joins stay fast.
        continuing = self.emitted_until is not None
        if plan.copy and not plan.live and plan.offset > 0.5 and plan.target and (continuing or not plan.target.startswith("http")):
            kf = await keyframe_before(plan.target, plan.offset)
            if kf is not None:
                plan.preroll = min(max(plan.offset - kf, 0.0), MAX_PREROLL)
                plan.keyframe_aligned = True
        return plan

    async def _fail_plan(self, plan: SegmentPlan, msg: str) -> SegmentPlan | None:
        await self._handle_failure(plan, msg, mark_source=plan.source_id is not None)
        return None

    async def _handle_failure(self, plan: SegmentPlan, msg: str | None, mark_source: bool = True) -> None:
        """Record the failure, take the source out of rotation and replace the
        rest of the failed programme with freshly generated entries."""
        log.warning("channel %s: %s (%s)", self.slug, msg, plan.title)
        self.failures += 1
        forget_target(plan.source_id)
        if plan.source_url and ytstream.cache_key(plan.source_url):
            ytstream.forget(plan.source_url)  # stream addresses may have expired: re-resolve next time
        cut = max(self.emitted_until or utcnow(), plan.sched_start)
        with session_scope() as s:
            s.add(PlaybackEvent(channel_id=self.channel_id, playback_source_id=plan.source_id, message=f"{plan.title}: {msg}"))
            if mark_source and plan.source_id:
                ps = s.get(PlaybackSource, plan.source_id)
                if ps:
                    ps.health_ok = False
                    ps.failure_count += 1
                    ps.last_error = f"playback: {msg}"[:500]
            ch = s.get(Channel, self.channel_id)
            e = s.get(ScheduleEntry, plan.entry_id) if plan.entry_id else None
            if ch is not None and e is not None and e.kind != EntryKind.SLATE:
                title = e.title
                if e.start < cut:
                    # Keep what already aired; the rest of the slot is regenerated.
                    e.end = cut
                    if not e.title.endswith("(interrupted)"):
                        e.title = f"{e.title} (interrupted)"
                    s.flush()
                    changed, removed = cut, truncate_from(s, ch, cut)
                else:
                    changed, removed = e.start, truncate_from(s, ch, e.start)
                # Short horizon keeps recovery fast; the schedule job extends the rest.
                added = extend(s, ch, utcnow(), days=REPLACEMENT_HORIZON_DAYS)
                record_version(s, ch, "failover", changed, added, removed, note=f"'{title}': {msg}"[:500])
        self._retry_now = True

    def _cmd(self, plan: SegmentPlan, drawtext: bool) -> list[str]:
        st = settings
        cmd = [st.ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats", "-progress", "pipe:2"]
        now = self._now()
        lead = ((self.emitted_until or now) - now).total_seconds()
        burst = max(0.0, min(TARGET_LEAD, TARGET_LEAD - lead))
        rate = ["-readrate", "1"] + (["-readrate_initial_burst", f"{burst:.2f}"] if burst > 0.05 else [])
        out_v: list[str]
        if plan.kind == EntryKind.SLATE or plan.target is None:
            label = _sanitize_text(
                plan.label or (f"{self.channel_name} - back soon" if plan.kind == EntryKind.SLATE else self.channel_name)
            )
            cmd += rate + ["-f", "lavfi", "-i", "color=c=0x15151f:s=1280x720:r=25"]
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            vf = f"drawtext=text='{label}':fontcolor=white:fontsize=44:x=(w-tw)/2:y=(h-th)/2" if drawtext else "null"
            cmd += ["-t", f"{plan.duration:.3f}", "-map", "0:v", "-map", "1:a", "-vf", vf]
            out_v = ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage", "-pix_fmt", "yuv420p", "-g", "50", "-b:v", "400k"]
            cmd += out_v + ["-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "2"]
        else:
            http_opts = [
                "-protocol_whitelist", FFMPEG_PROTOCOLS, "-reconnect", "1", "-reconnect_on_network_error", "1",
                "-reconnect_delay_max", "10", "-rw_timeout", "20000000", "-user_agent", plan.user_agent or st.http_user_agent,
            ]
            cmd += http_opts if plan.target.startswith("http") else ["-protocol_whitelist", FFMPEG_PROTOCOLS]
            if plan.offset > 0.5:
                if plan.keyframe_aligned:
                    # Remuxing can only start on a keyframe. Without this,
                    # FFmpeg keeps the pre-roll packets' earlier timestamps,
                    # which land before the previous segment's (a backwards
                    # jump after a mid-programme failover). Starting the output
                    # clock at the keyframe keeps timestamps moving forward
                    # and video and audio aligned.
                    cmd += ["-noaccurate_seek"]
                cmd += ["-ss", f"{plan.offset:.3f}"]
            # A live feed paces itself; -readrate would only add delay.
            src_rate = [] if plan.live else rate
            cmd += src_rate + ["-i", plan.target]
            if plan.audio_target:
                # Separate audio stream: same seek, same pacing, mapped as input 1.
                cmd += http_opts + (["-ss", f"{plan.offset:.3f}"] if plan.offset > 0.5 else []) + src_rate + ["-i", plan.audio_target]
            elif not plan.has_audio:
                cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            # With -noaccurate_seek the output starts at the keyframe, so the
            # pre-roll counts towards -t; add it so audio runs to the same end.
            audio_map = "1:a:0" if plan.audio_target or not plan.has_audio else "0:a:0"
            cmd += ["-t", f"{plan.duration + plan.preroll:.3f}", "-map", "0:v:0", "-map", audio_map, "-sn", "-dn"]
            audio_norm = ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
            if plan.copy:
                # Remux video; normalise audio (cheap) so sample rate/layout
                # never changes mid-stream unless the channel is pure "copy".
                cmd += ["-c:v", "copy"] + (["-c:a", "copy"] if plan.copy_audio and plan.has_audio and not plan.audio_target else audio_norm)
            else:
                h = st.transcode_height
                w = (h * 16 // 9) // 2 * 2
                vf = (
                    f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                    "setsar=1,fps=25,format=yuv420p"
                )
                cmd += [
                    "-vf", vf, "-c:v", "libx264", "-preset", st.transcode_preset, "-profile:v", "high",
                    "-b:v", st.transcode_video_bitrate, "-maxrate", st.transcode_video_bitrate, "-bufsize", "5M",
                    "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
                ] + audio_norm
        cmd += [
            "-threads", str(st.ffmpeg_threads),
            "-muxdelay", "0.7", "-muxpreload", "0.7",
            # A remux that starts on an earlier keyframe emits ``preroll``
            # seconds before the seek point; start the clock that much later
            # so those packets continue on from the previous segment.
            "-output_ts_offset", f"{self.ts_offset + plan.preroll:.3f}",
            "-mpegts_flags", "+initial_discontinuity+resend_headers",
            "-mpegts_service_id", "1", "-mpegts_pmt_start_pid", "4096", "-mpegts_start_pid", "256",
            "-metadata", f"service_name={self.channel_name}", "-metadata", "service_provider=ArchiveTV",
            # Marker so orphan cleanup only ever touches our own processes.
            "-metadata", "comment=archivetv",
            "-f", "mpegts", "pipe:1",
        ]
        return cmd

    async def _play(self, plan: SegmentPlan) -> tuple[float, bool, str | None]:
        cmd = self._cmd(plan, await self.m.drawtext_available())
        log.info("channel %s: %s '%s' offset=%.1fs dur=%.1fs %s", self.slug, plan.kind, plan.title, plan.offset, plan.duration,
                 "copy" if plan.copy else "encode")
        self.proc = proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=_limit_child,
        )
        self.m.register_pid(proc.pid)
        out_time = 0.0
        errors: list[str] = []

        async def read_stderr() -> None:
            nonlocal out_time
            assert proc.stderr
            async for raw in proc.stderr:
                line = raw.decode(errors="replace").strip()
                if line.startswith("out_time_us="):
                    try:
                        out_time = max(out_time, int(line.split("=", 1)[1]) / 1e6)
                    except ValueError:
                        pass
                elif line and "=" not in line[:24]:
                    errors.append(line)
                    del errors[:-10]

        err_task = asyncio.create_task(read_stderr())
        pending = b""
        assert proc.stdout
        try:
            while True:
                data = await proc.stdout.read(READ_SIZE)
                if not data:
                    break
                pending += data
                cut = len(pending) - (len(pending) % TS_PACKET)
                if cut:
                    self._broadcast(pending[:cut])
                    pending = pending[cut:]
            rc = await proc.wait()
            await err_task
        finally:
            if proc.returncode is None:
                await self._kill_proc()
            self.m.unregister_pid(proc.pid)
            self.proc = None
        produced = min(out_time, plan.duration) if out_time > 0 else 0.0
        if self._stopping:
            return produced, True, None
        if rc != 0:
            return produced, False, "; ".join(errors[-3:]) or f"ffmpeg exited with {rc}"
        if produced == 0.0 and plan.duration > 1:
            return 0.0, False, "; ".join(errors[-3:]) or "no output produced"
        return produced, True, None


async def keyframe_before(target: str, offset: float) -> float | None:
    """Timestamp of the keyframe a remux seeking to ``offset`` will start on."""
    cmd = [settings.ffprobe_path, "-v", "error", "-protocol_whitelist", FFMPEG_PROTOCOLS, "-user_agent", settings.http_user_agent,
           "-read_intervals", f"{offset:.3f}%+#1", "-select_streams", "v:0", "-show_entries", "packet=pts_time", "-of", "csv=p=0", target]
    try:
        p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                                 stdin=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 15)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        return float(out.decode().strip().splitlines()[0].split(",")[0])
    except (ValueError, IndexError):
        return None


def _drain_and_close(q: asyncio.Queue) -> None:
    try:
        while True:
            q.get_nowait()
    except asyncio.QueueEmpty:
        pass
    try:
        q.put_nowait(None)
    except asyncio.QueueFull:
        pass


def _limit_child() -> None:  # pragma: no cover - runs in the child
    try:
        os.nice(5)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (OSError, ValueError):
        pass


manager = StreamManager()
