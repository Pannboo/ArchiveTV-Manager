"""Experimental YouTube streaming (ATV_YOUTUBE_STREAMING): off by default,
gated by approval and rights, and the resolver only ever hands FFmpeg
validated googlevideo.com HTTPS addresses."""

import asyncio

import pytest

from archivetv import ytstream
from archivetv.catalogue import is_schedulable, set_rights, source_is_iptv_ok
from archivetv.config import settings
from archivetv.enums import Eligibility, PlaybackType, ReviewStatus, RightsStatus
from archivetv.models import MediaItem, MediaSource, PlaybackSource
from archivetv.providers.base import MediaRecord
from archivetv.providers.youtube import STREAM_FLAG, with_stream_option
from archivetv.security import UnsafeURLError

GOOD = "https://rr1---sn-abc.googlevideo.com/videoplayback?expire=9999999999&itag=134"
AUDIO = "https://rr1---sn-abc.googlevideo.com/videoplayback?expire=9999999999&itag=140"


def _info(video=GOOD, audio=AUDIO, **extra):
    return {"duration": 19, "requested_formats": [
        {"url": video, "vcodec": "avc1.4D400C", "acodec": "none", "height": 240, "http_headers": {"User-Agent": "UA/1"}},
        {"url": audio, "vcodec": "none", "acodec": "mp4a.40.2"}], **extra}


@pytest.fixture()
def streaming(monkeypatch):
    monkeypatch.setattr(settings, "youtube_streaming", True)
    ytstream._cache.clear()
    ytstream._by_video_url.clear()

    async def public(url):
        return None

    monkeypatch.setattr(ytstream, "check_url_resolves_public", public)
    return monkeypatch


def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "youtube_streaming", False)
    with pytest.raises(UnsafeURLError, match="disabled"):
        asyncio.run(ytstream.resolve("https://youtu.be/jNQXAC9IVRw"))
    rec = MediaRecord(provider="youtube", external_id="jNQXAC9IVRw", title="t")
    assert with_stream_option(rec).playback == []


def test_resolver_returns_validated_pair(streaming):
    streaming.setattr(ytstream, "_extract", lambda vid: _info())
    r = asyncio.run(ytstream.resolve("https://www.youtube.com/watch?v=jNQXAC9IVRw"))
    assert (r.video, r.audio, r.user_agent, r.acodec) == (GOOD, AUDIO, "UA/1", "mp4a.40.2")
    assert ytstream.companion(GOOD) is r


@pytest.mark.parametrize("info,match", [
    (_info(video="http://rr1.googlevideo.com/x?expire=1"), "unexpected stream host"),
    (_info(video="https://evil.example/x"), "unexpected stream host"),
    (_info(audio="https://169.254.169.254/latest"), "unexpected stream host"),
    (_info(live_status="is_upcoming"), "not started"),
    (_info(has_drm=True), "DRM"),
])
def test_resolver_rejects_unsafe_results(streaming, info, match):
    streaming.setattr(ytstream, "_extract", lambda vid: info)
    with pytest.raises(UnsafeURLError, match=match):
        asyncio.run(ytstream.resolve("https://youtu.be/jNQXAC9IVRw"))


def test_resolver_rejects_non_video_urls(streaming):
    for url in ("https://www.youtube.com/playlist?list=PL1", "https://example.org/watch?v=jNQXAC9IVRw"):
        with pytest.raises(UnsafeURLError):
            asyncio.run(ytstream.resolve(url))


def test_stream_source_gated_by_switch_approval_and_rights(session, streaming):
    s = session
    rec = with_stream_option(MediaRecord(provider="youtube", external_id="jNQXAC9IVRw", title="t"))
    assert rec.playback[-1].playback_type == PlaybackType.YOUTUBE_STREAM and STREAM_FLAG in rec.flags
    it = MediaItem(source=MediaSource(provider="youtube", external_id="jNQXAC9IVRw"), title="Zoo", duration=19.0,
                   review_status=ReviewStatus.PENDING_REVIEW)
    ps = PlaybackSource(playback_type=PlaybackType.YOUTUBE_STREAM, url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
                        eligibility=Eligibility.IPTV_READY, health_ok=True, duration=19.0)
    it.playback_sources.append(ps)
    s.add(it)
    s.flush()
    assert not is_schedulable(it)
    it.review_status = ReviewStatus.APPROVED
    assert not is_schedulable(it)  # rights still unreviewed
    set_rights(s, ps, RightsStatus.AUTHORISED, "test")
    assert is_schedulable(it) and source_is_iptv_ok(ps)
    streaming.setattr(settings, "youtube_streaming", False)
    assert not source_is_iptv_ok(ps) and not is_schedulable(it)
    with pytest.raises(ValueError, match="switched off"):
        set_rights(s, ps, RightsStatus.AUTHORISED, "again")
    # Other YouTube source types still can never be authorised.
    emb = PlaybackSource(playback_type=PlaybackType.YOUTUBE_EMBED, url="https://www.youtube.com/embed/jNQXAC9IVRw")
    it.playback_sources.append(emb)
    with pytest.raises(ValueError):
        set_rights(s, emb, RightsStatus.AUTHORISED)


def test_live_broadcasts_and_channel_live_urls(streaming):
    seen = []

    def extract(target):
        seen.append(target)
        return {"is_live": True, "live_status": "is_live", "url": GOOD.replace("videoplayback", "manifest.m3u8"),
                "vcodec": "avc1.4D401F", "acodec": "mp4a.40.2", "height": 720, "http_headers": {"User-Agent": "UA/1"}}

    streaming.setattr(ytstream, "_extract", extract)
    assert ytstream.channel_live_url("https://youtube.com/@MyFireplace/live") == "https://www.youtube.com/@MyFireplace/live"
    assert ytstream.channel_live_url("https://www.youtube.com/channel/UC4QobU6STFB0P71PMvOGN5A/live/")
    assert ytstream.channel_live_url("https://www.youtube.com/@x/videos") is None
    r = asyncio.run(ytstream.resolve("https://www.youtube.com/@MyFireplace/live"))
    assert r.live and r.audio is None and r.duration is None and r.acodec == "mp4a.40.2"
    assert seen == ["https://www.youtube.com/@MyFireplace/live"]  # yt-dlp follows the channel to its current broadcast


def _live_item(s, title="Fireplace", healthy=True):
    it = MediaItem(source=MediaSource(provider="youtube", external_id=f"live:@{title}"), title=title,
                   review_status=ReviewStatus.APPROVED, tags=["live"])
    it.playback_sources.append(PlaybackSource(
        playback_type=PlaybackType.YOUTUBE_STREAM, url=f"https://www.youtube.com/@{title}/live", live=True,
        eligibility=Eligibility.IPTV_READY, rights_status=RightsStatus.AUTHORISED, health_ok=healthy))
    s.add(it)
    s.flush()
    return it


def test_live_slots_follow_blocks_and_hours(session, streaming):
    from datetime import datetime, timedelta

    from archivetv.catalogue import assign_to_channel
    from archivetv.enums import ChannelRole, ScheduleMode
    from archivetv.models import Channel, ChannelBlock, ScheduleEntry
    from archivetv.scheduler import check_continuity, extend

    s = session
    live = _live_item(s)
    assert is_schedulable(live)  # no duration needed for a live source
    ch = Channel(slug="fire", tvg_id="fire", name="Fire", mode=ScheduleMode.BLOCK)
    s.add(ch)
    s.flush()
    assign_to_channel(s, live, ch.id, ChannelRole.PROGRAMME)
    # 20:00–23:00 London (GMT in November = UTC)
    s.add(ChannelBlock(channel_id=ch.id, name="Fireplace", start_time="20:00", end_time="23:00", tag_filter="live"))
    s.flush()
    s.refresh(ch)
    start = datetime(2026, 11, 6, 19, 0)
    extend(s, ch, start, days=0.25)
    es = s.query(ScheduleEntry).filter_by(channel_id=ch.id).order_by(ScheduleEntry.start).all()
    assert check_continuity(es) == []
    block = [e for e in es if e.block_name == "Fireplace"]
    assert block and block[0].start == datetime(2026, 11, 6, 20, 0) and all(e.end <= datetime(2026, 11, 6, 23, 0) for e in block)
    assert sum((e.end - e.start).total_seconds() for e in block) == 3 * 3600
    # Outside the block, a live-only channel still gets hourly live slots.
    assert all((e.end - e.start) <= timedelta(hours=1) for e in es)


def test_live_programmes_are_never_replayed(session, streaming):
    from datetime import timedelta

    from archivetv import catchup
    from archivetv.catalogue import assign_to_channel
    from archivetv.db import utcnow
    from archivetv.enums import ChannelRole
    from archivetv.models import Channel
    from archivetv.scheduler import extend

    s = session
    live = _live_item(s)
    ch = Channel(slug="lv", tvg_id="lv", name="Lv")
    s.add(ch)
    s.flush()
    assign_to_channel(s, live, ch.id, ChannelRole.PROGRAMME)
    s.refresh(ch)
    extend(s, ch, utcnow() - timedelta(hours=3), days=0.2)
    with pytest.raises(catchup.CatchupError) as e:
        catchup.resolve_request(s, ch, utcnow() - timedelta(hours=2))
    assert e.value.status == 404 and "live broadcast" in e.value.message


def test_live_source_coming_back_reschedules(session, streaming):
    from datetime import timedelta

    from archivetv import jobs
    from archivetv.catalogue import assign_to_channel
    from archivetv.db import utcnow
    from archivetv.enums import ChannelRole
    from archivetv.models import Channel, ScheduleEntry
    from archivetv.scheduler import extend

    s = session
    live = _live_item(s, healthy=False)
    ch = Channel(slug="back", tvg_id="back", name="Back")
    s.add(ch)
    s.flush()
    assign_to_channel(s, live, ch.id, ChannelRole.PROGRAMME)
    s.refresh(ch)
    extend(s, ch, utcnow(), days=0.1)
    s.commit()
    assert {e.kind for e in s.query(ScheduleEntry).filter_by(channel_id=ch.id)} == {"slate"}  # offline: off air

    async def ok(ps):
        ps.health_ok = True
        return True

    streaming.setattr(jobs, "health_check", ok)
    asyncio.run(jobs.live_tick())
    s.expire_all()
    future = s.query(ScheduleEntry).filter(ScheduleEntry.channel_id == ch.id, ScheduleEntry.start > utcnow() + timedelta(minutes=5)).all()
    assert future and all(e.media_item_id == live.id for e in future)


def test_channel_live_import_requires_switch(session, monkeypatch):
    from archivetv.discovery import import_youtube_live

    monkeypatch.setattr(settings, "youtube_streaming", False)
    r = asyncio.run(import_youtube_live("https://www.youtube.com/@MyFireplace/live"))
    assert r.new == 0 and "switched off" in r.errors[0]
