"""Integration test: the stream engine produces one continuous MPEG-TS across
programme transitions (remux + transcode), using generated local clips."""

import asyncio
import json
import shutil
import subprocess

import pytest

from archivetv.catalogue import assign_to_channel
from archivetv.config import settings
from archivetv.enums import ChannelRole, Eligibility, PlaybackType, ReviewStatus, RightsStatus, ScheduleMode
from archivetv.models import Channel, MediaItem, MediaSource, PlaybackSource
from archivetv.scheduler import extend
from archivetv.stream import StreamManager

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def _clip(path, seconds, vcodec="libx264", size="640x360", rate=44100):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=25:d={seconds}",
         "-f", "lavfi", "-i", f"sine=f=440:d={seconds}:r={rate}", "-c:v", vcodec, "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )


def _local_item(s, rel, eligibility, duration):
    item = MediaItem(source=MediaSource(provider="local", external_id=rel), title=rel, duration=duration, review_status=ReviewStatus.APPROVED)
    item.playback_sources.append(PlaybackSource(playback_type=PlaybackType.LOCAL_FILE, local_path=rel, eligibility=eligibility,
                                                rights_status=RightsStatus.AUTHORISED, duration=duration, audio_codec="aac"))
    s.add(item)
    s.flush()
    return item


def _dts(path, sel):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", sel, "-show_entries", "packet=dts_time", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout
    vals = (x.strip(",") for x in out.split())
    return [float(x) for x in vals if x and x != "N/A"]


def test_continuous_stream_across_transitions(session, tmp_path):
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    _clip(settings.media_dir / "a.mp4", 4)
    _clip(settings.media_dir / "b.mkv", 4, vcodec="mpeg4", size="320x240")
    s = session
    ch = Channel(slug="itest", tvg_id="itest", name="Itest", mode=ScheduleMode.SEQUENTIAL)
    s.add(ch)
    s.flush()
    a = _local_item(s, "a.mp4", Eligibility.IPTV_READY, 4.0)
    b = _local_item(s, "b.mkv", Eligibility.IPTV_NEEDS_TRANSCODE, 4.0)
    for it in (a, b):
        assign_to_channel(s, it, ch.id, ChannelRole.PROGRAMME)
    s.commit()
    s.refresh(ch)
    extend(s, ch, days=0.05)
    s.commit()

    async def capture() -> bytes:
        m = StreamManager()
        st, q = await m.subscribe(ch.id)
        buf = bytearray()
        loop = asyncio.get_running_loop()
        end = loop.time() + 14
        while loop.time() < end:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=end - loop.time())
            except asyncio.TimeoutError:
                break
            if chunk is None:
                break
            buf += chunk
        st.remove_viewer(q)
        await m.stop_all()
        return bytes(buf)

    data = asyncio.run(capture())
    out = tmp_path / "out.ts"
    out.write_bytes(data)
    assert len(data) % 188 == 0 and data[0] == 0x47
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(out)],
                                     capture_output=True, text=True).stdout)
    assert [st["codec_name"] for st in info["streams"]] == ["h264", "aac"]
    for sel in ("v:0", "a:0"):
        dts = _dts(out, sel)
        assert dts[-1] - dts[0] > 9, f"{sel} too short: {dts[-1] - dts[0]:.1f}s (expected several programmes)"
        backwards = [(x, y) for x, y in zip(dts, dts[1:]) if y < x]
        gaps = [(x, y) for x, y in zip(dts, dts[1:]) if y - x > 0.5]
        assert not backwards, backwards[:5]
        assert not gaps, gaps[:5]
