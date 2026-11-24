"""The parallel fetcher: exact bytes for any range, real speed-up against an
origin that limits each connection, graceful fallback, and a stream that
stays smooth where a single connection would stutter."""

import asyncio
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from archivetv import relay
from archivetv.config import settings
from archivetv.enums import Eligibility, PlaybackType
from archivetv.stream import StreamManager


class _Origin(BaseHTTPRequestHandler):
    """Range-capable origin that throttles every connection to ``per_conn`` B/s."""
    payload = b""
    per_conn = 0
    ranges = True
    drop_once_at: int | None = None  # close the first request reaching this offset
    active = 0
    peak = 0
    requests = 0
    lock = threading.Lock()

    def _range(self):
        h = self.headers.get("Range")
        if not h or not type(self).ranges:
            return None
        a, _, b = h.split("=", 1)[1].partition("-")
        size = len(type(self).payload)
        return int(a), min(int(b), size - 1) if b else size - 1

    def _head(self, rng):
        size = len(type(self).payload)
        a, b = rng or (0, size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", str(b - a + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        self.end_headers()
        return a, b

    def do_HEAD(self):  # noqa: N802
        self._head(self._range())

    def do_GET(self):  # noqa: N802
        cls = type(self)
        with cls.lock:
            cls.active += 1
            cls.requests += 1
            cls.peak = max(cls.peak, cls.active)
        try:
            a, b = self._head(self._range())
            pos, step = a, 16384
            while pos <= b:
                if cls.drop_once_at is not None and pos <= cls.drop_once_at < pos + step:
                    cls.drop_once_at = None
                    return  # connection closes mid-response
                piece = cls.payload[pos : min(b + 1, pos + step)]
                if cls.per_conn:
                    time.sleep(len(piece) / cls.per_conn)  # throttle before writing, so a finished request ends at once
                self.wfile.write(piece)
                pos += step
        except OSError:
            pass
        finally:
            with cls.lock:
                cls.active -= 1

    def log_message(self, *a):
        pass


@pytest.fixture()
def origin(monkeypatch):
    monkeypatch.setattr(settings, "parallel_connections", 4)
    _Origin.payload = os.urandom(5 * relay.CHUNK + 12345)
    _Origin.per_conn, _Origin.ranges, _Origin.drop_once_at = 0, True, None
    _Origin.active = _Origin.peak = _Origin.requests = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/file.mp4"
    srv.shutdown()


async def _get(url, rng=None):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers={"Range": rng} if rng else {})
        return r.status_code, r.headers, r.content


def test_relay_serves_exact_bytes_for_any_range(origin):
    data = _Origin.payload
    size = len(data)

    async def run():
        local = await relay.wrap(origin)
        assert local.startswith("http://127.0.0.1:") and local != origin
        assert await relay.wrap(origin) == local  # one relay per file
        try:
            out = {}
            out["full"] = await _get(local)
            out["span"] = await _get(local, f"bytes=100-{relay.CHUNK * 2 + 50}")  # crosses two chunk edges
            out["open"] = await _get(local, f"bytes={size - 70000}-")
            out["suffix"] = await _get(local, "bytes=-1000")
            out["bad"] = await _get(local, f"bytes={size + 5}-")
            async with httpx.AsyncClient() as c:
                out["head"] = (await c.head(local)).headers
            return out
        finally:
            await relay.close_all()

    out = asyncio.run(run())
    assert out["full"][0] == 200 and out["full"][2] == data
    st, h, body = out["span"]
    assert st == 206 and body == data[100 : relay.CHUNK * 2 + 51]
    assert h["content-range"] == f"bytes 100-{relay.CHUNK * 2 + 50}/{size}"
    assert out["open"][2] == data[size - 70000 :]
    assert out["suffix"][2] == data[-1000:]
    assert out["bad"][0] == 416
    assert out["head"]["content-length"] == str(size) and out["head"]["accept-ranges"] == "bytes"


def test_parallel_connections_beat_the_per_connection_limit(origin):
    _Origin.payload = os.urandom(48 * relay.CHUNK)  # long enough for four connections to stay busy
    _Origin.per_conn = 2_000_000  # 2 MB/s per connection
    size = len(_Origin.payload)

    async def run():
        local = await relay.wrap(origin)
        t0 = time.monotonic()
        try:
            st, _, body = await _get(local)
        finally:
            await relay.close_all()
        return time.monotonic() - t0, body

    elapsed, body = asyncio.run(run())
    assert body == _Origin.payload
    single = size / _Origin.per_conn  # ~12.6 s on one connection
    assert elapsed < single / 3, f"{elapsed:.1f}s vs {single:.1f}s on one connection"
    assert _Origin.peak == 4


def test_reader_can_jump_to_the_end_and_back(origin):
    """Like FFmpeg reading an MP4 whose index sits at the end of the file."""
    data = _Origin.payload

    async def run():
        local = await relay.wrap(origin)
        r = relay._by_url[origin]
        try:
            head = await _get(local, "bytes=0-4095")
            tail = await _get(local, f"bytes={len(data) - 4096}-")
            again = await _get(local, "bytes=4096-")
            return head[2], tail[2], again[2], len(r.chunks)
        finally:
            await relay.close_all()

    head, tail, again, held = asyncio.run(run())
    assert head == data[:4096] and tail == data[-4096:] and again == data[4096:]
    assert held <= relay.AHEAD + relay.KEEP_BEHIND + 3  # memory stays bounded


def test_a_dropped_origin_connection_is_resumed(origin):
    _Origin.drop_once_at = relay.CHUNK + 300_000

    async def run():
        local = await relay.wrap(origin)
        try:
            return await _get(local)
        finally:
            await relay.close_all()

    st, _, body = asyncio.run(run())
    assert st == 200 and body == _Origin.payload


def test_origin_without_range_support_is_read_directly(origin):
    _Origin.ranges = False

    async def run():
        try:
            return await relay.wrap(origin)
        finally:
            await relay.close_all()

    assert asyncio.run(run()) == origin


def test_relay_can_be_turned_off(origin, monkeypatch):
    monkeypatch.setattr(settings, "parallel_connections", 1)
    assert asyncio.run(relay.wrap(origin)) == origin


def start_now(s, ch):
    """Shift a channel's schedule so the programme on air starts now: tests
    then don't depend on where the wall clock falls within the hour."""
    from archivetv.db import utcnow
    from archivetv.models import ScheduleEntry

    es = s.query(ScheduleEntry).filter_by(channel_id=ch.id).order_by(ScheduleEntry.start).all()
    now = utcnow()
    cur = next(e for e in es if e.start <= now < e.end)
    d = now - cur.start
    for e in es:
        e.start, e.end = e.start + d, e.end + d
    s.commit()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
@pytest.mark.parametrize("conns", [4, 1])
def test_stream_from_a_per_connection_throttled_origin(session, tmp_path, monkeypatch, conns):
    """Each origin connection runs at 0.6x the programme's bitrate, like the
    AV1 clip on archive.org. Through four parallel connections viewers get a
    smooth stream; on one connection (the control) they stutter."""
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
    _Origin.payload = remote.read_bytes()
    _Origin.per_conn = int(len(_Origin.payload) / 20 * 0.6)
    _Origin.ranges, _Origin.drop_once_at = True, None
    srv = ThreadingHTTPServer((host, 0), _Origin)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "extra_media_hosts", [host])
    monkeypatch.setattr(settings, "allow_private_media_hosts", True)
    monkeypatch.setattr(settings, "parallel_connections", conns)
    it = _local_item(session, "unused", Eligibility.IPTV_READY, 20.0)
    ps = it.playback_sources[0]
    ps.playback_type, ps.local_path, ps.url = PlaybackType.DIRECT_URL, None, f"http://{host}:{srv.server_port}/remote.ts"
    ch = _channel(session, f"relay{conns}", [it])
    start_now(session, ch)

    async def run():
        m = StreamManager()
        try:
            out, gap, st = await _watch(m, ch.id, 12)
            return out, gap, st.underruns
        finally:
            await m.stop_all()
            await relay.close_all()

    try:
        out, gap, underruns = asyncio.run(run())
    finally:
        srv.shutdown()
    _check_continuous(tmp_path, out, f"relay{conns}")
    if conns > 1:
        assert underruns == 0 and gap < 1.5, f"viewers waited {gap:.1f}s ({underruns} underruns)"
    else:
        assert underruns > 0, "control: one throttled connection should not keep up"
