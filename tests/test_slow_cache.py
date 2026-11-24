"""Slow sources are downloaded before they air, deleted afterwards, and aired
only from the finished file (a card shows while they are still downloading)."""

import asyncio
import shutil
import subprocess
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from archivetv import cache, jobs
from archivetv import stream as stream_mod
from archivetv.config import settings
from archivetv.db import utcnow
from archivetv.enums import ChannelRole, Eligibility, EntryKind, PlaybackType, ReviewStatus, RightsStatus, ScheduleMode
from archivetv.models import Channel, MediaItem, MediaSource, PlaybackSource, ScheduleEntry
from archivetv.stream import ChannelStream, SegmentPlan, StreamManager

SLOW = dict(bitrate_kbps=3700, throughput_kbps=1960)  # 0.53x, like the AV1 clip on archive.org
FAST = dict(bitrate_kbps=800, throughput_kbps=5800)


@pytest.fixture()
def slow_cache(monkeypatch, tmp_path):
    """Minimal-disk off, slow-source caching on, general cache off."""
    monkeypatch.setattr(settings, "minimal_disk", False)
    monkeypatch.setattr(settings, "cache_enabled", False)
    monkeypatch.setattr(settings, "cache_slow_sources", True)
    monkeypatch.setattr(settings, "cache_lead_hours", 3)
    monkeypatch.setattr(settings, "min_speed_ratio", 1.5)
    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(settings, "parallel_connections", 1)  # the disk path is the fallback when the relay is off
    settings.cache_dir.mkdir()
    monkeypatch.setattr(cache, "_slots", None)
    return settings.cache_dir


def _src(n=0, **kw):
    base = dict(playback_type=PlaybackType.DIRECT_URL, url=f"https://archive.org/download/x{n}/x{n}.mp4",
                eligibility=Eligibility.IPTV_READY, rights_status=RightsStatus.AUTHORISED, duration=60.0)
    return PlaybackSource(**{**base, **kw})


def test_only_slow_sources_are_cached_and_only_with_disk_allowed(slow_cache, monkeypatch):
    slow, fast = _src(**SLOW), _src(**FAST)
    assert cache.wants(slow) and cache.must_wait(slow)
    assert not cache.wants(fast) and not cache.must_wait(fast)
    assert not cache.wants(_src(bitrate_kbps=3700))  # speed never measured
    assert not cache.wants(_src(allow_cache=False, **SLOW))  # admin opted this source out
    assert not cache.wants(_src(live=True, **SLOW))
    monkeypatch.setattr(settings, "cache_enabled", True)
    assert cache.wants(fast) and not cache.must_wait(fast)  # general cache: fetched, but may stream
    monkeypatch.setattr(settings, "cache_slow_sources", False)
    monkeypatch.setattr(settings, "cache_enabled", False)
    assert not settings.cache_active and not cache.must_wait(slow)
    monkeypatch.setattr(settings, "cache_slow_sources", True)
    monkeypatch.setattr(settings, "minimal_disk", True)  # minimal-disk vetoes all of it
    assert not settings.cache_active and not cache.wants(slow) and not cache.must_wait(slow)


def test_download_starts_early_enough_for_the_measured_speed(slow_cache):
    ps = _src(bitrate_kbps=4000, throughput_kbps=2000)  # downloads at half playback speed
    assert cache.lead_seconds(ps, 600) == 3 * 3600  # short programme: the configured lead
    assert cache.lead_seconds(ps, 3 * 3600) == 3 * 3600 / 0.5 * cache.LEAD_SAFETY  # long one: 9 h


def _scheduled(s, ch, ps, start, seconds=600):
    it = MediaItem(source=MediaSource(provider="archive", external_id=f"x{time.monotonic_ns()}"), title=ps.url,
                   duration=float(seconds), review_status=ReviewStatus.APPROVED)
    it.playback_sources.append(ps)
    s.add(it)
    s.flush()
    e = ScheduleEntry(channel_id=ch.id, start=start, end=start + timedelta(seconds=seconds), kind=EntryKind.PROGRAMME,
                      media_item_id=it.id, playback_source_id=ps.id, title=it.title)
    s.add(e)
    return e


def test_cache_job_fetches_soonest_slow_airings_and_sweeps_finished_ones(session, slow_cache, monkeypatch):
    s, now = session, utcnow()
    ch = Channel(slug="slow", tvg_id="slow", name="Slow", mode=ScheduleMode.SEQUENTIAL)
    off = Channel(slug="off", tvg_id="off", name="Off", enabled=False)
    idle = Channel(slug="idle", tvg_id="idle", name="Idle")  # enabled, but nobody is watching
    s.add_all([ch, off, idle])
    s.flush()
    soon = _src(1, **SLOW)
    airing = _src(2, **SLOW)
    later = _src(3, **SLOW)
    fast = _src(4, **FAST)
    done = _src(5, **SLOW)
    disabled = _src(6, **SLOW)
    _scheduled(s, ch, soon, now + timedelta(hours=1))
    _scheduled(s, ch, airing, now - timedelta(minutes=2))
    _scheduled(s, ch, later, now + timedelta(hours=10))  # outside the 3 h lead
    _scheduled(s, ch, fast, now + timedelta(minutes=30))
    _scheduled(s, ch, done, now - timedelta(hours=2))  # ended 1 h 50 min ago
    _scheduled(s, off, disabled, now + timedelta(minutes=5))
    unwatched = _src(8, **SLOW)
    _scheduled(s, idle, unwatched, now + timedelta(minutes=5))
    s.commit()
    for ps in (soon, done, unwatched):
        cache.cache_path(ps.url).write_bytes(b"x")
    stray = slow_cache / "leftover.mp4"
    stray.write_bytes(b"x")
    partial = slow_cache / "inflight.mp4.part"
    partial.write_bytes(b"x")

    started: list[str] = []
    monkeypatch.setattr(cache, "start_prefetch", started.append)
    monkeypatch.setattr(stream_mod.manager, "status", lambda: [{"channel_id": ch.id}, {"channel_id": off.id}])
    asyncio.run(jobs.cache_tick())

    assert started == [airing.url, soon.url]  # soonest first; prefetch skips the one already cached
    assert cache.cache_path(soon.url).exists()
    assert not cache.cache_path(done.url).exists() and not stray.exists()
    assert not cache.cache_path(unwatched.url).exists()  # its channel has no viewers
    assert partial.exists()  # a download in progress is left alone


def test_nothing_is_fetched_while_no_channel_is_watched(session, slow_cache, monkeypatch):
    ch = Channel(slug="nv", tvg_id="nv", name="Nv")
    session.add(ch)
    session.flush()
    _scheduled(session, ch, _src(9, **SLOW), utcnow() + timedelta(minutes=5))
    session.commit()
    started: list[str] = []
    monkeypatch.setattr(cache, "start_prefetch", started.append)
    monkeypatch.setattr(stream_mod.manager, "status", lambda: [])
    asyncio.run(jobs.cache_tick())
    assert started == []


def test_downloads_for_a_channel_nobody_watches_are_cancelled(slow_cache, monkeypatch):
    """Viewers leave mid-download: the download stops and its partial file goes."""
    url = "https://archive.org/download/big/big.mp4"
    part = cache.cache_path(url).with_suffix(cache.cache_path(url).suffix + ".part")

    async def run():
        gate = asyncio.Event()

        async def resolve(u, **kw):
            part.write_bytes(b"partial")
            await gate.wait()  # stands in for a long download

        monkeypatch.setattr(cache, "resolve_media_url", resolve)
        cache.start_prefetch(url)
        await asyncio.sleep(0.05)
        assert cache.in_progress(url) and part.exists()
        assert cache.cancel_except({"https://archive.org/download/other/other.mp4"}) == 1
        await asyncio.sleep(0.05)
        return cache.in_progress(url)

    assert asyncio.run(run()) is False
    assert not part.exists() and not cache.cache_path(url).exists()


def test_cache_job_does_nothing_in_minimal_disk_mode(session, slow_cache, monkeypatch):
    monkeypatch.setattr(settings, "minimal_disk", True)
    ch = Channel(slug="md", tvg_id="md", name="Md")
    session.add(ch)
    session.flush()
    _scheduled(session, ch, _src(7, **SLOW), utcnow() + timedelta(minutes=5))
    session.commit()
    started: list[str] = []
    monkeypatch.setattr(cache, "start_prefetch", started.append)
    asyncio.run(jobs.cache_tick())
    assert started == []


def test_slow_source_airs_a_card_until_downloaded_then_the_file(slow_cache, monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(cache, "start_prefetch", started.append)
    st = ChannelStream(StreamManager(), 1)
    url = "https://archive.org/download/av1/av1.mp4"
    snap = PlaybackSource(id=9, playback_type=PlaybackType.DIRECT_URL, url=url)
    pos = utcnow()

    def plan():
        p = SegmentPlan(42, EntryKind.PROGRAMME, "AV1 clip", pos, 600.0, 120.0, source_id=9, copy=True)
        p.source_url, p.cache_only = url, True
        return p

    card = asyncio.run(st._resolve(plan(), snap))
    assert card.kind == EntryKind.SLATE and card.target is None and card.entry_id == 42
    assert card.duration == stream_mod.SLATE_SEGMENT and "downloading" in card.label
    assert started == [url]  # the download is kicked off right away

    cache.cache_path(url).write_bytes(b"not really video")
    monkeypatch.setattr(stream_mod, "keyframe_before", lambda *a: asyncio.sleep(0, 118.0))
    ready = asyncio.run(st._resolve(plan(), snap))
    assert ready.kind == EntryKind.PROGRAMME and ready.target == str(cache.cache_path(url))
    assert ready.offset == pytest.approx(120.0, abs=1.0)  # joins at the scheduled position


class _SlowHandler(BaseHTTPRequestHandler):
    payload = b""
    bps = 0  # bytes per second; 0 = as fast as possible

    def do_GET(self):  # noqa: N802
        cls = type(self)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(cls.payload)))
        self.end_headers()
        step = 65536
        try:
            for i in range(0, len(cls.payload), step):
                self.wfile.write(cls.payload[i : i + step])
                if cls.bps:
                    time.sleep(step / cls.bps)
        except OSError:
            pass

    def log_message(self, *a):
        pass


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
@pytest.mark.parametrize("ext", ["mp4", "ts"])
def test_slow_source_end_to_end_card_then_programme_from_disk(session, slow_cache, tmp_path, monkeypatch, ext):
    """A slow source is airing when the viewer tunes in and isn't downloaded:
    viewers get the card, the download finishes in the background, and the
    programme then airs from the file at its scheduled position, with
    continuous timestamps throughout."""
    from .test_failures import _channel, _check_continuous, _lan_ip, _watch
    from .test_stream import _local_item

    host = _lan_ip()
    if host is None:
        pytest.skip("no private LAN address to serve test media from")
    remote = tmp_path / f"remote.{ext}"  # MPEG-TS seeks are approximate: the join is measured, not predicted
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=30",
         "-f", "lavfi", "-i", "sine=f=440:d=30", "-c:v", "libx264", "-preset", "ultrafast", "-g", "50",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(remote)],
        check=True,
    )
    _SlowHandler.payload = remote.read_bytes()
    _SlowHandler.bps = len(_SlowHandler.payload) / 3  # the whole file takes ~3 s
    srv = ThreadingHTTPServer((host, 0), _SlowHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "extra_media_hosts", [host])
    monkeypatch.setattr(settings, "allow_private_media_hosts", True)
    monkeypatch.setattr(stream_mod, "SLATE_SEGMENT", 2.0)
    it = _local_item(session, "unused", Eligibility.IPTV_READY, 30.0)
    ps = it.playback_sources[0]
    ps.playback_type, ps.local_path = PlaybackType.DIRECT_URL, None
    ps.url = f"http://{host}:{srv.server_port}/remote.{ext}"
    ps.bitrate_kbps, ps.throughput_kbps = SLOW["bitrate_kbps"], SLOW["throughput_kbps"]
    ch = _channel(session, "slowe2e", [it])
    from .test_relay import start_now

    start_now(session, ch)
    url = ps.url
    seen: list[tuple[float, str, str | None]] = []

    def record(st, t):
        if st.current:
            seen.append((t, st.current.kind, st.current.target))

    async def run():
        m = StreamManager()
        try:
            return await _watch(m, ch.id, 9, record)
        finally:
            await m.stop_all()

    try:
        out, gap, _ = asyncio.run(run())
    finally:
        srv.shutdown()
    _check_continuous(tmp_path, out, f"slowcache-{ext}")
    kinds = [k for _, k, _ in seen]
    assert kinds[0] == EntryKind.SLATE, "a card airs while the slow file downloads"
    played = [(t, tgt) for t, k, tgt in seen if k == EntryKind.PROGRAMME]
    assert played, f"the programme never aired from the download: {kinds}"
    assert all(tgt == str(cache.cache_path(url)) for _, tgt in played), "the slow source must not be streamed"
    assert gap < 1.5, f"viewers waited {gap:.1f}s"
