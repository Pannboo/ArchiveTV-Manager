"""Specification §17 failure conditions, exercised against real FFmpeg streams:
FFmpeg crashing, the network dropping mid-programme, a restart during a
programme, the database becoming unavailable, and a full temporary
filesystem. In every case viewers must keep receiving a valid stream."""

import asyncio
import os
import shutil
import signal
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from archivetv import db, monitor
from archivetv.catalogue import assign_to_channel
from archivetv.config import settings
from archivetv.db import utcnow
from archivetv.enums import ChannelRole, Eligibility, PlaybackType, ScheduleMode
from archivetv.models import Channel, PlaybackEvent
from archivetv.scheduler import extend
from archivetv.stream import StreamManager

from .test_stream import _clip, _dts, _local_item

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def _channel(s, slug, clips, start_ago=0.0):
    ch = Channel(slug=slug, tvg_id=slug, name=slug.title(), mode=ScheduleMode.SEQUENTIAL)
    s.add(ch)
    s.flush()
    for it in clips:
        assign_to_channel(s, it, ch.id, ChannelRole.PROGRAMME)
    s.commit()
    s.refresh(ch)
    extend(s, ch, utcnow() - timedelta(seconds=start_ago), days=0.05)
    s.commit()
    return ch


async def _watch(m, cid, seconds, during=None):
    """Read a live stream for ``seconds``; ``during(st, t)`` runs each 0.25s.
    Returns (bytes, largest gap in seconds between chunks, stream)."""
    st, q = await m.subscribe(cid)
    loop = asyncio.get_running_loop()
    buf, last, gap = bytearray(), loop.time(), 0.0
    start = loop.time()
    end = start + seconds

    async def driver():
        while loop.time() < end:
            if during:
                r = during(st, loop.time() - start)
                if asyncio.iscoroutine(r):
                    await r
            await asyncio.sleep(0.25)

    drv = asyncio.create_task(driver())
    first = True
    while loop.time() < end:
        try:
            chunk = await asyncio.wait_for(q.get(), timeout=max(0.05, end - loop.time()))
        except asyncio.TimeoutError:
            break
        if chunk is None:
            break
        now = loop.time()
        if not first:
            gap = max(gap, now - last)
        first, last = False, now
        buf += chunk
    drv.cancel()
    st.remove_viewer(q)
    return bytes(buf), gap, st


def _check_continuous(tmp_path, data, name):
    p = tmp_path / f"{name}.ts"
    p.write_bytes(data)
    assert len(data) % 188 == 0 and data[:1] == b"\x47"
    for sel in ("v:0", "a:0"):
        d = _dts(p, sel)
        assert d and not [(x, y) for x, y in zip(d, d[1:]) if y < x], f"{name} {sel}: timestamps went backwards"
    return _dts(p, "v:0")


def test_ffmpeg_crash_mid_programme(session, tmp_path):
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    _clip(settings.media_dir / "c1.mp4", 8)
    _clip(settings.media_dir / "c2.mp4", 8)
    s = session
    items = [_local_item(s, f"c{i}.mp4", Eligibility.IPTV_READY, 8.0) for i in (1, 2)]
    ch = _channel(s, "crash", items)
    killed = []

    def kill_once(st, t):
        if t > 3 and not killed and st.proc and st.proc.returncode is None:
            os.killpg(st.proc.pid, signal.SIGKILL)  # simulate FFmpeg crashing
            killed.append(t)

    async def run():
        m = StreamManager()
        try:
            return await _watch(m, ch.id, 11, kill_once)
        finally:
            await m.stop_all()

    data, gap, _ = asyncio.run(run())
    assert killed, "FFmpeg was never killed"
    span = _check_continuous(tmp_path, data, "crash")
    assert span[-1] - span[0] > 7, "stream did not continue after the crash"
    assert gap < 3.5, f"viewer starved for {gap:.1f}s after the crash"
    s.expire_all()
    assert s.query(PlaybackEvent).filter(PlaybackEvent.channel_id == ch.id).count() >= 1


class _DroppingHandler(BaseHTTPRequestHandler):
    """Serves the first part of a file, then the 'network' goes away."""
    payload = b""
    served = 0

    def do_GET(self):  # noqa: N802
        cls = type(self)
        cls.served += 1
        if cls.served > 1:  # every reconnect attempt fails
            self.close_connection = True
            self.connection.shutdown(2)
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", str(len(cls.payload)))
        self.send_header("Accept-Ranges", "none")
        self.end_headers()
        self.wfile.write(cls.payload[: len(cls.payload) // 3])
        self.wfile.flush()
        self.connection.shutdown(2)  # connection drops mid-file

    def do_HEAD(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(type(self).payload)))
        self.end_headers()

    def log_message(self, *a):
        pass


def _lan_ip() -> str | None:
    """A private, non-loopback address of this machine (loopback is never an
    allowed media host, by design of the SSRF guard)."""
    import ipaddress
    import socket

    import subprocess

    candidates = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("10.255.255.255", 1))
            candidates.append(sk.getsockname()[0])
    except OSError:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True).stdout
        candidates += [line.split()[3].split("/")[0] for line in out.splitlines() if len(line.split()) > 3]
    for ip in candidates:
        a = ipaddress.ip_address(ip)
        if a.is_private and not (a.is_loopback or a.is_link_local):
            return ip
    return None


def test_network_drop_mid_programme(session, tmp_path, monkeypatch):
    host = _lan_ip()
    if host is None:
        pytest.skip("no private LAN address to serve test media from")
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    # A streamable container (like most remote IPTV sources), so the first
    # part of the file is playable on its own.
    _clip(tmp_path / "remote.mp4", 12)
    remote = tmp_path / "remote.ts"
    import subprocess

    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp_path / "remote.mp4"), "-c", "copy", str(remote)], check=True)
    _clip(settings.media_dir / "after.mp4", 10)
    _DroppingHandler.payload, _DroppingHandler.served = remote.read_bytes(), 0
    srv = ThreadingHTTPServer((host, 0), _DroppingHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "extra_media_hosts", [host])
    monkeypatch.setattr(settings, "allow_private_media_hosts", True)
    s = session
    net = _local_item(s, "unused", Eligibility.IPTV_READY, 12.0)
    ps = net.playback_sources[0]
    ps.playback_type, ps.local_path, ps.url = PlaybackType.DIRECT_URL, None, f"http://{host}:{srv.server_port}/remote.ts"
    nxt = _local_item(s, "after.mp4", Eligibility.IPTV_READY, 10.0)
    ch = _channel(s, "netdrop", [net, nxt])

    async def run():
        m = StreamManager()
        try:
            return await _watch(m, ch.id, 30)
        finally:
            await m.stop_all()

    try:
        data, gap, st = asyncio.run(run())
    finally:
        srv.shutdown()
    assert _DroppingHandler.served >= 1
    span = _check_continuous(tmp_path, data, "netdrop")
    assert span[-1] - span[0] > 12, "stream did not move on to the next programme"
    s.expire_all()
    events = s.query(PlaybackEvent).filter(PlaybackEvent.channel_id == ch.id).all()
    assert events, "the failure was not logged"


def test_restart_resumes_at_wall_clock_position(session, tmp_path):
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    _clip(settings.media_dir / "long.mp4", 40)
    s = session
    it = _local_item(s, "long.mp4", Eligibility.IPTV_READY, 40.0)
    ch = _channel(s, "resume", [it], start_ago=6)

    async def tune(seconds):
        m = StreamManager()  # a fresh process: nothing carried over but the database
        seen = {}

        def first_plan(st, _t):
            if st.current is not None:
                seen.setdefault("offset", st.current.offset)
                seen.setdefault("entry", st.current.entry_id)

        try:
            await _watch(m, ch.id, seconds, first_plan)
            return seen.get("offset"), seen.get("entry")
        finally:
            await m.stop_all()

    from archivetv.scheduler import current_entry

    for _ in range(2):  # two separate "process starts"
        before = utcnow()
        offset, entry_id = asyncio.run(tune(1.5))
        entry = current_entry(s, ch.id, before)
        if entry_id != entry.id:  # joined right at a programme boundary
            entry = s.get(type(entry), entry_id)
        expected = max(0.0, (before - entry.start).total_seconds())
        # The new process joins where the guide says the programme is now,
        # allowing for start-up time.
        assert offset is not None and expected - 0.5 <= offset <= expected + 3.0, (offset, expected)


def test_database_outage_keeps_viewers_on_air(session, tmp_path):
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    _clip(settings.media_dir / "d1.mp4", 3)
    _clip(settings.media_dir / "d2.mp4", 3)
    s = session
    items = [_local_item(s, f"d{i}.mp4", Eligibility.IPTV_READY, 3.0) for i in (1, 2)]
    ch = _channel(s, "dbdown", items)
    good = s.get_bind()
    bad = create_engine(f"sqlite:///{tmp_path}/missing-dir/nope.sqlite3")
    health_during = {}

    def outage(st, t):
        if 2 < t < 9 and "down" not in health_during:
            db.SessionLocal.configure(bind=bad)
            health_during["down"] = TestClient(__import__("archivetv.main", fromlist=["app"]).app).get("/health").status_code
        elif t >= 9 and "up" not in health_during:
            db.SessionLocal.configure(bind=good)
            health_during["up"] = t

    async def run():
        m = StreamManager()
        try:
            return await _watch(m, ch.id, 15, outage)
        finally:
            db.SessionLocal.configure(bind=good)
            await m.stop_all()

    data, gap, st = asyncio.run(run())
    assert health_during.get("down") == 503
    span = _check_continuous(tmp_path, data, "dbdown")
    assert span[-1] - span[0] > 12 and gap < 3.5, (span[-1] - span[0], gap)
    assert st.current is not None and st.current.kind == "programme", "did not return to programmes after the outage"


def test_full_temp_filesystem_raises_alert(monkeypatch):
    class Usage:
        total, used, free = 256 * 2**20, 250 * 2**20, 6 * 2**20

    real = shutil.disk_usage
    monkeypatch.setattr(monitor.shutil, "disk_usage", lambda p: Usage if str(p).startswith("/tmp") else real(p))
    monkeypatch.delenv("TMPDIR", raising=False)
    snap = monitor.snapshot(StreamManager())
    assert any("temporary filesystem" in a for a in snap["alerts"])


class _BusyMirrorHandler(BaseHTTPRequestHandler):
    """Answers the first GETs with 503 (a busy mirror), then serves the file."""
    payload = b""
    gets = 0

    def do_GET(self):  # noqa: N802
        cls = type(self)
        cls.gets += 1
        if cls.gets <= 2:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", str(len(cls.payload)))
        self.end_headers()
        try:
            self.wfile.write(cls.payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", str(len(type(self).payload)))
        self.end_headers()

    def log_message(self, *a):
        pass


def test_transient_mirror_errors_are_retried_not_failed_over(session, tmp_path, monkeypatch):
    host = _lan_ip()
    if host is None:
        pytest.skip("no private LAN address to serve test media from")
    import subprocess

    _clip(tmp_path / "busy.mp4", 30)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp_path / "busy.mp4"), "-c", "copy", str(tmp_path / "busy.ts")], check=True)
    _BusyMirrorHandler.payload, _BusyMirrorHandler.gets = (tmp_path / "busy.ts").read_bytes(), 0
    srv = ThreadingHTTPServer((host, 0), _BusyMirrorHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "extra_media_hosts", [host])
    monkeypatch.setattr(settings, "allow_private_media_hosts", True)
    s = session
    it = _local_item(s, "unused", Eligibility.IPTV_READY, 30.0)
    ps = it.playback_sources[0]
    ps.playback_type, ps.local_path, ps.url = PlaybackType.DIRECT_URL, None, f"http://{host}:{srv.server_port}/busy.ts"
    ch = _channel(s, "busy", [it])

    async def run():
        m = StreamManager()
        try:
            return await _watch(m, ch.id, 12)
        finally:
            await m.stop_all()

    try:
        data, gap, st = asyncio.run(run())
    finally:
        srv.shutdown()
    assert _BusyMirrorHandler.gets >= 3
    span = _check_continuous(tmp_path, data, "busy")
    assert span[-1] - span[0] > 3, "the programme never played after the mirror recovered"
    s.expire_all()
    assert s.get(type(ps), ps.id).health_ok, "a transient mirror error must not mark the source unhealthy"
