"""Read-ahead buffer and slow-source detection.

The pacer is exercised with synthetic MPEG-TS packets (only PIDs and PES
timestamps matter to it); the end-to-end test streams a real FFmpeg channel
from an HTTP origin that stalls mid-programme."""

import asyncio
import shutil
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from archivetv import stream as stream_mod
from archivetv import validator
from archivetv.catalogue import best_iptv_source, source_is_slow, speed_fields
from archivetv.config import settings
from archivetv.db import utcnow
from archivetv.enums import Eligibility, PlaybackType, ReviewStatus, RightsStatus
from archivetv.models import MediaItem, MediaSource, PlaybackSource
from archivetv.stream import VIDEO_PID, ChannelStream, StreamManager


def _pkt(pts: float | None, pid: int = VIDEO_PID) -> bytes:
    """One 188-byte TS packet; with ``pts`` it starts a PES carrying that timestamp."""
    if pts is None:
        return bytes([0x47, (pid >> 8) & 0x1F, pid & 0xFF, 0x10]) + b"\xff" * 184
    p = int(round(pts * 90000)) & ((1 << 33) - 1)
    pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + bytes([
        0x21 | ((p >> 29) & 0x0E), (p >> 22) & 0xFF, 0x01 | ((p >> 14) & 0xFE), (p >> 7) & 0xFF, 0x01 | ((p << 1) & 0xFE),
    ])
    return bytes([0x47, 0x40 | ((pid >> 8) & 0x1F), pid & 0xFF, 0x10]) + pes + b"\xff" * (184 - len(pes))


def _released_pts(st: ChannelStream, q: asyncio.Queue) -> list[float]:
    out = []
    while not q.empty():
        chunk = q.get_nowait()
        out.append(st._pes_pts(chunk, 0, chunk[3]))
    return out


def test_pes_timestamp_round_trip():
    st = ChannelStream(StreamManager(), 1)
    assert st._scan_pts(_pkt(12.5) + _pkt(None) + _pkt(13.0)) == pytest.approx(13.0, abs=1e-4)
    assert st.seg_max_pts == pytest.approx(13.0, abs=1e-4)
    assert st._scan_pts(_pkt(None)) is None


def test_pacer_releases_at_playback_speed(monkeypatch):
    monkeypatch.setattr(stream_mod, "TARGET_LEAD", 1.0)

    async def run():
        st = ChannelStream(StreamManager(), 1)
        q = st.add_viewer()
        st._pacer = asyncio.create_task(st._pace())
        try:
            for i in range(12):  # 6 seconds of output arrive at once
                st._enqueue(_pkt(10.0 + 0.5 * i))
            await asyncio.sleep(0.2)
            first = _released_pts(st, q)
            await asyncio.sleep(1.5)
            later = _released_pts(st, q)
            return first, later, st.buffered_ahead()
        finally:
            st._pacer.cancel()

    first, later, ahead = asyncio.run(run())
    # TARGET_LEAD (1 s) goes out at once: PTS 10.0, 10.5, 11.0 ...
    assert first == pytest.approx([10.0, 10.5, 11.0], abs=1e-3)
    # ... then the rest follows the clock, not the arrival time.
    assert 2 <= len(later) <= 4, later
    assert ahead > 2.0


def test_pacer_recovers_from_an_underrun_at_playback_speed(monkeypatch):
    monkeypatch.setattr(stream_mod, "TARGET_LEAD", 1.0)

    async def run():
        st = ChannelStream(StreamManager(), 1)
        q = st.add_viewer()
        st._pacer = asyncio.create_task(st._pace())
        try:
            st._enqueue(_pkt(10.0))
            st._enqueue(_pkt(10.5))
            await asyncio.sleep(2.6)  # the origin stalls; the buffer runs dry
            _released_pts(st, q)
            for i in range(8):
                st._enqueue(_pkt(11.0 + 0.5 * i))
            await asyncio.sleep(0.1)
            return _released_pts(st, q), st.underruns
        finally:
            st._pacer.cancel()

    resumed, underruns = asyncio.run(run())
    assert underruns == 1
    # Restarted from the late chunk with the usual lead, not a burst of everything.
    assert resumed == pytest.approx([11.0, 11.5, 12.0], abs=1e-3)


def test_buffer_applies_back_pressure(monkeypatch):
    monkeypatch.setattr(stream_mod, "TARGET_LEAD", 1.0)
    monkeypatch.setattr(stream_mod, "READAHEAD_SLACK", 0.5)
    monkeypatch.setattr(settings, "readahead_seconds", 2)
    st = ChannelStream(StreamManager(), 1)
    for i in range(7):
        st._enqueue(_pkt(20.0 + 0.5 * i))  # spans 3.0 s: under 2 + 1 + 0.5
    assert not st._buffer_full()
    st._enqueue(_pkt(24.0))
    assert st._buffer_full()


def test_readahead_setting_is_clamped(monkeypatch):
    monkeypatch.setattr(settings, "readahead_seconds", -5)
    assert stream_mod.readahead() == 0
    monkeypatch.setattr(settings, "readahead_seconds", 10_000)
    assert stream_mod.readahead() == stream_mod.MAX_READAHEAD


def test_ffmpeg_reads_ahead_for_media_but_not_for_cards(monkeypatch):
    monkeypatch.setattr(settings, "readahead_seconds", 20)
    st = ChannelStream(StreamManager(), 1)
    now = utcnow()
    media = stream_mod.SegmentPlan(1, "programme", "x", now, 60.0, 0.0, target="/m/x.mp4")
    cmd = st._cmd(media, drawtext=False, catchup=True)
    assert cmd[cmd.index("-readrate_initial_burst") + 1] == "23.00"
    assert "-readrate_catchup" in cmd
    card = stream_mod.SegmentPlan(None, "slate", "x", now, 10.0, 0.0)
    cmd = st._cmd(card, drawtext=False, catchup=True)
    assert cmd[cmd.index("-readrate_initial_burst") + 1] == "3.00"
    assert "-readrate_catchup" not in cmd
    st.emitted_until = now + timedelta(seconds=15)  # already 15 s ahead: top up only
    cmd = st._cmd(media, drawtext=False, catchup=False)
    assert float(cmd[cmd.index("-readrate_initial_burst") + 1]) == pytest.approx(8.0, abs=0.1)
    assert "-readrate_catchup" not in cmd


# ---------------------------------------------------------------- slow sources


def _item_with(s, *sources):
    it = MediaItem(source=MediaSource(provider="archive", external_id=f"x{time.monotonic_ns()}"), title="x",
                   duration=60.0, review_status=ReviewStatus.APPROVED)
    for ps in sources:
        it.playback_sources.append(ps)
    s.add(it)
    s.flush()
    return it


def _src(**kw):
    base = dict(playback_type=PlaybackType.DIRECT_URL, url="https://archive.org/download/x/x.mp4",
                eligibility=Eligibility.IPTV_READY, rights_status=RightsStatus.AUTHORISED, duration=60.0)
    return PlaybackSource(**{**base, **kw})


def test_slow_flag_follows_measured_speed_against_bitrate(monkeypatch):
    monkeypatch.setattr(settings, "min_speed_ratio", 1.5)
    assert source_is_slow(_src(bitrate_kbps=3700, throughput_kbps=2900))
    assert not source_is_slow(_src(bitrate_kbps=800, throughput_kbps=2900))
    assert not source_is_slow(_src(bitrate_kbps=3700))  # never measured: no verdict
    assert not source_is_slow(_src(bitrate_kbps=3700, throughput_kbps=100, live=True))
    f = speed_fields(_src(bitrate_kbps=3700, throughput_kbps=2900))
    assert f == {"bitrate_kbps": 3700, "throughput_kbps": 2900, "speed_ratio": 0.78, "slow": True}


def test_best_source_avoids_slow_ones_while_another_works(session, monkeypatch):
    monkeypatch.setattr(settings, "min_speed_ratio", 1.5)
    slow = _src(priority=10, bitrate_kbps=3700, throughput_kbps=2000)
    fast = _src(priority=20, bitrate_kbps=800, throughput_kbps=9000)
    it = _item_with(session, slow, fast)
    assert best_iptv_source(it) is fast
    fast.health_ok = False
    assert best_iptv_source(it) is slow  # a slow source still beats none


def test_validation_records_bitrate_and_throughput(monkeypatch):
    probe = {"format": {"duration": "165.2", "bit_rate": "3662000", "format_name": "mov,mp4"},
             "streams": [{"codec_type": "video", "codec_name": "av1", "pix_fmt": "yuv420p", "width": 1280, "height": 720},
                         {"codec_type": "audio", "codec_name": "aac"}]}

    async def target(ps, client=None, use_cache=False):
        return "https://ia800507.us.archive.org/x.mp4"

    async def fake_probe(t, timeout=45):
        return probe

    async def ranges(url):
        return True

    seen = {}

    async def measure(url, seconds, max_bytes=0):
        seen["url"], seen["seconds"] = url, seconds
        return 2_100_000 / 8  # 2.1 Mbps

    monkeypatch.setattr(validator, "playable_target", target)
    monkeypatch.setattr(validator, "ffprobe", fake_probe)
    monkeypatch.setattr(validator, "_range_supported", ranges)
    monkeypatch.setattr(validator, "measure_throughput", measure)
    monkeypatch.setattr(settings, "speed_check_seconds", 4.0)
    ps = _src(eligibility=Eligibility.UNVERIFIED)
    asyncio.run(validator.validate_source(ps))
    assert ps.eligibility == Eligibility.IPTV_NEEDS_TRANSCODE
    assert (ps.bitrate_kbps, ps.throughput_kbps) == (3662, 2100)
    assert seen == {"url": "https://ia800507.us.archive.org/x.mp4", "seconds": 4.0}
    assert source_is_slow(ps)

    # A failed measurement keeps the last good one; 0 seconds skips the check.
    async def broken(url, seconds, max_bytes=0):
        return None

    monkeypatch.setattr(validator, "measure_throughput", broken)
    asyncio.run(validator.validate_source(ps))
    assert ps.throughput_kbps == 2100
    monkeypatch.setattr(settings, "speed_check_seconds", 0)
    monkeypatch.setattr(validator, "measure_throughput", measure)
    seen.clear()
    asyncio.run(validator.validate_source(ps))
    assert seen == {}


class _ThrottledHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(64 * 1024 * 100))
        self.end_headers()
        try:
            for _ in range(100):
                self.wfile.write(b"\0" * 64 * 1024)
                self.wfile.flush()
                time.sleep(0.1)  # ~640 KiB/s
        except OSError:
            pass

    def log_message(self, *a):
        pass


def test_measure_throughput_reports_sustained_speed():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ThrottledHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        bps = asyncio.run(validator.measure_throughput(f"http://127.0.0.1:{srv.server_port}/x", 1.5))
    finally:
        srv.shutdown()
    assert bps is not None and 400_000 < bps < 900_000, bps


# ------------------------------------------------------------ end to end


class _StallingHandler(BaseHTTPRequestHandler):
    """Serves a file, but goes silent for ``stall`` seconds partway through."""
    payload = b""
    stall_at = 0
    stall = 0.0

    def do_GET(self):  # noqa: N802
        cls = type(self)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", str(len(cls.payload)))
        self.send_header("Accept-Ranges", "none")
        self.end_headers()
        sent, stalled = 0, False
        try:
            while sent < len(cls.payload):
                if not stalled and sent >= cls.stall_at:
                    time.sleep(cls.stall)
                    stalled = True
                self.wfile.write(cls.payload[sent : sent + 32768])
                sent += 32768
        except OSError:
            pass

    def do_HEAD(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(type(self).payload)))
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_origin_stall_with_readahead_end_to_end(session, tmp_path, monkeypatch):
    """A 7 s origin stall 12 s into a high-bitrate (~14 Mbps) programme: the
    read-ahead path (burst, -readrate_catchup, pacer, back-pressure) keeps
    viewers fed with continuous timestamps and no underrun.

    There is deliberately no "without read-ahead" control: over a local
    interface the kernel's TCP receive buffer grows to tens of MB and hides
    the stall either way. Across the internet it stays small for a reader
    consuming at playback speed, which is why the buffer is needed there;
    the pacer tests above cover the mechanics independently."""
    readahead_s, watch_s = 12, 11.0
    import subprocess

    from .test_failures import _channel, _check_continuous, _lan_ip, _watch
    from .test_stream import _local_item

    host = _lan_ip()
    if host is None:
        pytest.skip("no private LAN address to serve test media from")
    remote = tmp_path / "remote.ts"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=20",
         "-f", "lavfi", "-i", "sine=f=440:d=20", "-vf", "noise=alls=15:allf=t", "-c:v", "libx264", "-preset", "ultrafast",
         "-crf", "23", "-g", "50", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-f", "mpegts", str(remote)],
        check=True,
    )
    data = remote.read_bytes()
    _StallingHandler.payload, _StallingHandler.stall_at, _StallingHandler.stall = data, len(data) * 12 // 20, 7.0
    srv = ThreadingHTTPServer((host, 0), _StallingHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "extra_media_hosts", [host])
    monkeypatch.setattr(settings, "allow_private_media_hosts", True)
    monkeypatch.setattr(settings, "readahead_seconds", readahead_s)
    it = _local_item(session, "unused", Eligibility.IPTV_READY, 20.0)
    ps = it.playback_sources[0]
    ps.playback_type, ps.local_path, ps.url = PlaybackType.DIRECT_URL, None, f"http://{host}:{srv.server_port}/remote.ts"
    ch = _channel(session, f"stall{readahead_s}", [it])

    async def run():
        m = StreamManager()
        try:
            out, gap, st = await _watch(m, ch.id, watch_s)
            return out, gap, st.underruns
        finally:
            await m.stop_all()

    try:
        out, gap, underruns = asyncio.run(run())
    finally:
        srv.shutdown()
    _check_continuous(tmp_path, out, "stall")
    assert gap < 1.5 and underruns == 0, f"viewers waited {gap:.1f}s ({underruns} underruns)"
