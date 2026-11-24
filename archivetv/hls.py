"""HLS output for live channels, segmented in RAM.

An HlsChannel is one more viewer of a channel's shared MPEG-TS stream
(stream.ChannelStream): it cuts that output into short segments at video
keyframes and keeps the last few in memory. Nothing extra is fetched from the
origin and nothing is written to disk, so minimal-disk mode still holds.

Each FFmpeg process the channel starts (a new programme, a break card, a
failover) begins with MPEG-TS discontinuity indicators. A segment is always
cut there and marked #EXT-X-DISCONTINUITY, so players reset their decoders
when the picture size or codec settings change between programmes.

Segments start with a fresh PAT/PMT so each one decodes on its own. Once
nobody has fetched a playlist or segment for a while, the HLS viewer leaves,
and the channel stops as usual when its last viewer has gone.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass

from .config import settings
from .stream import PMT_PID, PTS_WRAP, TS_PACKET, VIDEO_PID, ChannelStream, StreamManager, raw_pes_pts

log = logging.getLogger(__name__)

MAX_SEGMENT_FACTOR = 3.0  # cut without a keyframe past target × this (very long GOPs)
MAX_SEGMENT_BYTES = 24 * 1024 * 1024
EXTRA_KEPT = 4  # segments kept beyond the playlist window, for slightly late fetches
READY_TIMEOUT = 8.0  # how long a first playlist request waits for a segment (hls.js gives up at 10 s)
FRAME = 0.04  # nominal frame length added to a segment's last timestamp


@dataclass
class Segment:
    seq: int
    data: bytes
    duration: float
    discontinuity: bool
    disc_index: int  # discontinuities up to and including this segment


class HlsChannel:
    def __init__(self, hls: "HlsManager", slug: str, channel_id: int):
        self.hls = hls
        self.slug = slug
        self.channel_id = channel_id
        self.stream: ChannelStream | None = None
        self.queue: asyncio.Queue | None = None
        self.segments: deque[Segment] = deque()
        self.next_seq = 0
        self.disc_count = 0
        self.max_duration = 0.0
        self.finished = False
        self.ready = asyncio.Event()
        self.last_access = time.monotonic()
        self._task: asyncio.Task | None = None
        # segment being built
        self._buf = bytearray()
        self._start_pts: float | None = None
        self._last_pts: float | None = None
        self._pending_disc = False
        self._prev_pts: float | None = None  # for unwrapping 33-bit timestamps
        self._psi: dict[int, bytes] = {}

    @property
    def target(self) -> float:
        return max(1.0, float(settings.hls_segment_seconds))

    @property
    def max_segment(self) -> float:
        """Longest a segment may run: a remuxed programme with long keyframe
        gaps is cut here without waiting for a keyframe."""
        return self.target * MAX_SEGMENT_FACTOR

    @property
    def window(self) -> int:
        return max(3, int(settings.hls_window))

    async def start(self, manager: StreamManager) -> None:
        self.stream, self.queue = await manager.subscribe(self.channel_id)
        self._task = asyncio.create_task(self._consume(), name=f"hls-{self.slug}")

    def touch(self) -> None:
        self.last_access = time.monotonic()

    def idle_for(self) -> float:
        return time.monotonic() - self.last_access

    async def stop(self) -> None:
        if self.finished:
            return
        self.finished = True
        if self._task and not self._task.done():
            self._task.cancel()
        if self.stream is not None and self.queue is not None:
            self.stream.remove_viewer(self.queue)
        self.ready.set()

    # -- segmenting ------------------------------------------------------------

    async def _consume(self) -> None:
        assert self.queue is not None
        try:
            while True:
                chunk = await self.queue.get()
                if chunk is None:  # the channel stream ended (or dropped us as too slow)
                    break
                self.feed(chunk)
        except asyncio.CancelledError:
            pass
        finally:
            self.finished = True
            self.ready.set()

    def _unwrap(self, pts: float) -> float:
        if self._prev_pts is not None:
            pts += round((self._prev_pts - pts) / PTS_WRAP) * PTS_WRAP
        self._prev_pts = pts
        return pts

    def feed(self, chunk: bytes) -> None:
        """Process a run of whole TS packets from the channel stream."""
        for off in range(0, len(chunk) - TS_PACKET + 1, TS_PACKET):
            pkt = chunk[off: off + TS_PACKET]
            if pkt[0] != 0x47:
                continue
            b1, b2, b3 = pkt[1], pkt[2], pkt[3]
            pid = ((b1 & 0x1F) << 8) | b2
            if pid in (0, PMT_PID):
                if b1 & 0x40:
                    self._psi[pid] = pkt
                continue  # re-sent at the start of every segment
            pusi = bool(b1 & 0x40)
            if pid == VIDEO_PID and pusi:
                af = (b3 & 0x20) and pkt[4] > 0
                flags = pkt[5] if af else 0
                disc, key = bool(flags & 0x80), bool(flags & 0x40)
                raw = raw_pes_pts(pkt, 0, b3)
                pts = self._unwrap(raw) if raw is not None else None
                if self._start_pts is None:
                    if not key and not disc:
                        continue  # wait for a keyframe to start on
                    self._open(pts, disc)
                elif disc:
                    self._close()
                    self._open(pts, True)
                elif pts is not None and self._start_pts is not None:
                    long = pts - self._start_pts >= self.max_segment - 0.25 or len(self._buf) >= MAX_SEGMENT_BYTES
                    if (key and pts - self._start_pts >= self.target) or long:
                        self._close()
                        self._open(pts, False)
                if pts is not None and (self._last_pts is None or pts > self._last_pts):
                    self._last_pts = pts
            elif self._start_pts is None:
                continue  # nothing before the first keyframe
            self._buf += pkt

    def _open(self, pts: float | None, disc: bool) -> None:
        self._buf = bytearray(self._psi.get(0, b"") + self._psi.get(PMT_PID, b""))
        self._start_pts = pts
        self._last_pts = pts
        self._pending_disc = disc and self.next_seq > 0

    def _close(self) -> None:
        if self._start_pts is None or self._last_pts is None:
            return
        duration = max(FRAME, self._last_pts - self._start_pts + FRAME)
        if self._pending_disc:
            self.disc_count += 1
        seg = Segment(self.next_seq, bytes(self._buf), duration, self._pending_disc, self.disc_count)
        self.next_seq += 1
        self.segments.append(seg)
        self.max_duration = max(self.max_duration, duration)
        while len(self.segments) > self.window + EXTRA_KEPT:
            self.segments.popleft()
        self._start_pts = None
        self._buf = bytearray()
        self.ready.set()

    # -- output ----------------------------------------------------------------

    def playlist(self, uri) -> str:
        """Live media playlist; ``uri(seq)`` gives each segment's address."""
        segs = list(self.segments)[-self.window:]
        # Fixed for the playlist's life, as HLS requires: no segment runs
        # past max_segment, so the target never needs to grow.
        target = max(math.ceil(self.max_segment), math.ceil(self.max_duration))
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{target}"]
        first = segs[0] if segs else None
        lines.append(f"#EXT-X-MEDIA-SEQUENCE:{first.seq if first else self.next_seq}")
        ds = (first.disc_index - (1 if first.discontinuity else 0)) if first else self.disc_count
        lines.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{ds}")
        for sg in segs:
            if sg.discontinuity:
                lines.append("#EXT-X-DISCONTINUITY")
            lines.append(f"#EXTINF:{sg.duration:.3f},")
            lines.append(uri(sg.seq))
        if self.finished:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    def segment(self, seq: int) -> Segment | None:
        return next((sg for sg in self.segments if sg.seq == seq), None)


class HlsManager:
    def __init__(self) -> None:
        self.channels: dict[str, HlsChannel] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None

    def idle_limit(self) -> float:
        # A player refreshes the playlist every segment or so: allow for a
        # few missed refreshes before treating the viewer as gone.
        return max(float(settings.stream_idle_seconds), settings.hls_segment_seconds * 6, 20.0)

    async def get(self, manager: StreamManager, slug: str, channel_id: int) -> HlsChannel:
        async with self._lock:
            hc = self.channels.get(slug)
            if hc is None or hc.finished:
                hc = HlsChannel(self, slug, channel_id)
                await hc.start(manager)  # may raise StreamLimitError
                self.channels[slug] = hc
                self._ensure_reaper()
            hc.touch()
            return hc

    def existing(self, slug: str) -> HlsChannel | None:
        hc = self.channels.get(slug)
        if hc is not None:
            hc.touch()
        return hc

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap(), name="hls-reaper")

    async def _reap(self) -> None:
        while self.channels:
            await asyncio.sleep(5)
            for slug, hc in list(self.channels.items()):
                if hc.finished or hc.idle_for() > self.idle_limit():
                    if not hc.finished:
                        log.info("hls %s: no requests for %.0fs, leaving the channel", slug, hc.idle_for())
                    await hc.stop()
                    self.channels.pop(slug, None)

    def status(self) -> list[dict]:
        return [{"slug": s, "segments": len(h.segments), "next_seq": h.next_seq, "idle_s": round(h.idle_for(), 1)}
                for s, h in self.channels.items() if not h.finished]

    async def stop_all(self) -> None:
        for hc in list(self.channels.values()):
            await hc.stop()
        self.channels.clear()
        if self._reaper and not self._reaper.done():
            self._reaper.cancel()


hls_manager = HlsManager()
