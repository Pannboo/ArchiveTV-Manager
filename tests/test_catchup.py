"""Schedule-based catch-up: timestamp parsing, request validation and an
FFmpeg integration test replaying past programmes alongside a live viewer."""

import asyncio
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from archivetv import catchup
from archivetv.catalogue import assign_to_channel
from archivetv.catchup import CatchupError, parse_timestamp, resolve_request
from archivetv.config import settings
from archivetv.db import utcnow
from archivetv.enums import ChannelRole, Eligibility, ReviewStatus, ScheduleMode
from archivetv.models import CatchupSession, Channel, ScheduleEntry
from archivetv.scheduler import current_entry, extend, maintain
from archivetv.stream import StreamManager

from .test_stream import _clip, _dts, _local_item

LONDON = ZoneInfo("Europe/London")


def test_parse_timestamp_formats():
    # 14:30 BST on 1 July 2026 is 13:30 UTC.
    want = datetime(2026, 7, 1, 13, 30)
    for raw in ("2026-07-01:14-30", "2026-07-01_14-30", "2026-07-01:14-30-00", "2026-07-01 14:30:00", "2026-07-01T14:30"):
        assert parse_timestamp(raw, LONDON) == want, raw
    assert parse_timestamp("2026-07-01T13:30:00Z", LONDON) == want
    assert parse_timestamp(str(int((want - datetime(1970, 1, 1)).total_seconds())), LONDON) == want
    # GMT in winter: local time equals UTC.
    assert parse_timestamp("2026-01-10:08-00", LONDON) == datetime(2026, 1, 10, 8, 0)
    # Regression: fromisoformat() reads "05-06" as 05:00 at UTC-06:00.
    utc = ZoneInfo("UTC")
    assert parse_timestamp("2026-11-05:05-06", utc) == datetime(2026, 11, 5, 5, 6)
    assert parse_timestamp("2026-11-05_05-06", utc) == datetime(2026, 11, 5, 5, 6)
    assert parse_timestamp("2026-11-05:05:06:30", utc) == datetime(2026, 11, 5, 5, 6, 30)
    assert parse_timestamp("2026-11-05 05:06:30", utc) == datetime(2026, 11, 5, 5, 6, 30)
    for bad in ("", "yesterday", "2026-13-01:10-00"):
        with pytest.raises(CatchupError):
            parse_timestamp(bad, LONDON)


def _channel_with_history(s, minutes_ago: float, clips=(("a.mp4", Eligibility.IPTV_READY), ("b.mkv", Eligibility.IPTV_NEEDS_TRANSCODE)),
                          seconds: float = 4.0):
    ch = Channel(slug="hist", tvg_id="hist", name="Hist", mode=ScheduleMode.SEQUENTIAL)
    s.add(ch)
    s.flush()
    items = []
    for rel, elig in clips:
        it = _local_item(s, rel, elig, seconds)
        assign_to_channel(s, it, ch.id, ChannelRole.PROGRAMME)
        items.append(it)
    s.commit()
    s.refresh(ch)
    extend(s, ch, utcnow() - timedelta(minutes=minutes_ago), days=0.02)
    s.commit()
    return ch, items


def test_resolve_request_rules(session):
    s = session
    ch, items = _channel_with_history(s, minutes_ago=10)
    now = utcnow()
    t = now - timedelta(minutes=5, seconds=1)
    req = resolve_request(s, ch, t, now=now)
    e = current_entry(s, ch.id, t)
    assert req.entry_id == e.id and req.offset == pytest.approx((t - e.start).total_seconds())
    assert req.duration >= catchup.MIN_DURATION

    with pytest.raises(CatchupError) as ex:
        resolve_request(s, ch, now + timedelta(minutes=1), now=now)
    assert ex.value.status == 416
    with pytest.raises(CatchupError) as ex:
        resolve_request(s, ch, now - timedelta(days=8), now=now)
    assert ex.value.status == 410

    # Rejected after airing: history stays, but it can no longer be replayed.
    e.media_item.review_status = ReviewStatus.REJECTED
    s.flush()
    with pytest.raises(CatchupError) as ex:
        resolve_request(s, ch, t, now=now)
    assert ex.value.status == 410
    e.media_item.review_status = ReviewStatus.APPROVED

    # A source known not to support seeking can only be replayed from the start.
    e.playback_source.seekable = False
    s.flush()
    with pytest.raises(CatchupError) as ex:
        resolve_request(s, ch, e.start + timedelta(seconds=2), now=now)
    assert ex.value.status == 409
    assert resolve_request(s, ch, e.start, now=now).offset == 0

    ch.catchup_enabled = False
    with pytest.raises(CatchupError) as ex:
        resolve_request(s, ch, t, now=now)
    assert ex.value.status == 403


def test_history_retained_for_catchup_days(session):
    s = session
    ch, _ = _channel_with_history(s, minutes_ago=3 * 24 * 60)
    ch.catchup_days = 7
    maintain(s, ch)
    s.commit()
    oldest = s.query(ScheduleEntry).filter_by(channel_id=ch.id).order_by(ScheduleEntry.start).first()
    assert oldest.start < utcnow() - timedelta(days=2.9)
    ch.catchup_days = 1
    maintain(s, ch)
    s.commit()
    oldest = s.query(ScheduleEntry).filter_by(channel_id=ch.id).order_by(ScheduleEntry.start).first()
    assert oldest.end >= utcnow() - timedelta(days=1, minutes=1)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_catchup_session_independent_of_live(session, tmp_path, monkeypatch):
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    _clip(settings.media_dir / "a.mp4", 4)
    _clip(settings.media_dir / "b.mkv", 4, vcodec="mpeg4", size="320x240")
    monkeypatch.setattr(catchup, "MIN_DURATION", 5.0)
    s = session
    ch, _ = _channel_with_history(s, minutes_ago=2)
    t = utcnow() - timedelta(seconds=90)
    req = resolve_request(s, ch, t, duration=10)
    cid = ch.id

    async def run() -> tuple[bytes, bytes, dict]:
        m = StreamManager()
        live, lq = await m.subscribe(cid)
        cu, cq = await catchup.start_session(m, req, "direct", "pytest")
        status = cu.status()
        bufs = {"live": bytearray(), "cu": bytearray()}
        loop = asyncio.get_running_loop()
        end = loop.time() + 14

        async def drain(q, key):
            while loop.time() < end:
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=end - loop.time())
                except asyncio.TimeoutError:
                    return
                if chunk is None:
                    return
                bufs[key] += chunk

        await asyncio.gather(drain(lq, "live"), drain(cq, "cu"))
        live_alive = not live.finished
        live.remove_viewer(lq)
        await m.stop_all()
        status["live_alive_after_catchup"] = live_alive
        return bytes(bufs["live"]), bytes(bufs["cu"]), status

    live_data, cu_data, status = asyncio.run(run())
    assert status["programme"] == req.title and status["live_alive_after_catchup"]
    for name, data in (("live", live_data), ("catchup", cu_data)):
        out = tmp_path / f"{name}.ts"
        out.write_bytes(data)
        assert len(data) % 188 == 0 and data[:1] == b"\x47", name
        for sel in ("v:0", "a:0"):
            dts = _dts(out, sel)
            assert not [(x, y) for x, y in zip(dts, dts[1:]) if y < x], f"{name} {sel} went backwards"
            assert not [(x, y) for x, y in zip(dts, dts[1:]) if y - x > 0.5], f"{name} {sel} has gaps"
    cu_span = _dts(tmp_path / "catchup.ts", "v:0")
    # The session plays the requested 10s across programme boundaries, then
    # ends. Remuxing from a mid-clip seek starts at the previous keyframe, so
    # allow up to one test clip (one GOP) of pre-roll.
    assert 9.0 < cu_span[-1] - cu_span[0] < 10.0 + 4.5
    s.expire_all()
    row = s.get(CatchupSession, status["session_id"])
    assert row.status == "completed" and row.bytes_out > 0 and row.ended_at is not None


def test_xc_minute_times_snap_to_programme_start(session):
    """Dispatcharr drops seconds: 08:19:00 for a programme starting 08:19:48
    must start that programme, not the tail of the one before."""
    s = session
    ch, _ = _channel_with_history(s, minutes_ago=30, seconds=50.0)
    now = utcnow()
    starts = {x.start for x in s.query(ScheduleEntry).filter_by(channel_id=ch.id)}
    e = next(x for x in s.query(ScheduleEntry).filter_by(channel_id=ch.id).order_by(ScheduleEntry.start)
             if x.start < now - timedelta(minutes=2) and x.start.second >= 5
             and x.start.replace(second=0, microsecond=0) not in starts)
    truncated = e.start.replace(second=0, microsecond=0)
    assert resolve_request(s, ch, truncated, now=now, snap_seconds=60).entry_id == e.id
    assert resolve_request(s, ch, truncated, now=now, snap_seconds=60).offset == 0
    plain = resolve_request(s, ch, truncated, now=now)
    assert plain.entry_id != e.id or plain.offset > 0  # direct requests are honoured exactly
    exact = resolve_request(s, ch, e.start, now=now, snap_seconds=60)
    assert exact.entry_id == e.id  # an exact programme start is never moved


def test_ts_response_sends_keepalive_until_data():
    """Null packets go out at once and while the source opens, so upstream
    proxies (Dispatcharr's ~5 s read timeout) never time out."""
    from archivetv.main import NULL_PACKET, ts_response

    class FakeStream:
        removed = False

        def remove_viewer(self, q):
            self.removed = True

    async def run():
        q: asyncio.Queue = asyncio.Queue()
        st = FakeStream()
        resp = ts_response(st, q)
        it = resp.body_iterator.__aiter__()
        first = await it.__anext__()
        t0 = asyncio.get_running_loop().time()
        pad = await it.__anext__()  # nothing queued yet: keepalive after ~1 s
        waited = asyncio.get_running_loop().time() - t0
        real = b"\x47" + b"\x00" * 187
        await q.put(real)
        await q.put(None)
        rest = [c async for c in it]
        return first, pad, waited, rest, st.removed

    first, pad, waited, rest, removed = asyncio.run(run())
    assert len(first) >= 1024 and all(first[i] == 0x47 for i in range(0, len(first), 188))
    assert first[:4] == NULL_PACKET[:4] and pad.startswith(NULL_PACKET[:4]) and 0.8 < waited < 1.6
    assert rest == [b"\x47" + b"\x00" * 187] and removed


def test_snap_prefers_programme_matching_duration(session):
    from archivetv.models import MediaItem

    s = session
    ch, items = _channel_with_history(s, minutes_ago=30, seconds=50.0)
    now = utcnow()
    es = s.query(ScheduleEntry).filter_by(channel_id=ch.id).order_by(ScheduleEntry.start).all()
    # Two programmes inside one minute: a 20 s clip then a 10 min film.
    base = es[3].start.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for e in es[3:]:
        s.delete(e)
    s.flush()
    short = ScheduleEntry(channel_id=ch.id, start=es[2].end, end=base + timedelta(seconds=20), kind="programme", title="Clip",
                          media_item_id=items[0].id, playback_source_id=items[0].playback_sources[0].id)
    film = ScheduleEntry(channel_id=ch.id, start=base + timedelta(seconds=20), end=base + timedelta(minutes=10, seconds=20),
                         kind="programme", title="Film", media_item_id=items[1].id, playback_source_id=items[1].playback_sources[0].id)
    s.add_all([short, film])
    s.flush()
    # Shift the clip so it starts inside the same minute as the film.
    short.start = base + timedelta(seconds=5)
    s.flush()
    assert isinstance(s.get(MediaItem, items[0].id), MediaItem)
    pick = resolve_request(s, ch, base, duration=15 * 60, now=now, snap_seconds=60)  # Dispatcharr: 10 min + 5
    assert pick.title == "Film" and pick.offset == 0
    longest = resolve_request(s, ch, base, now=now, snap_seconds=60)
    assert longest.title == "Film"
