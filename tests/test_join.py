"""Joining a programme partway through, after other output has already gone
to viewers (a retry, or a card handing over): the new segment's timestamps
must carry on after the old ones, never before them. Where FFmpeg's seek
lands depends on the container: exact in MP4, approximate in MPEG-TS."""

import asyncio
import shutil
import subprocess
from datetime import timedelta

import pytest

from archivetv.db import utcnow
from archivetv.enums import EntryKind
from archivetv.stream import MUX_START, ChannelStream, SegmentPlan, StreamManager, earliest_pts

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("join")
    out = {}
    for ext in ("ts", "mp4"):
        p = d / f"src.{ext}"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=20",
             "-f", "lavfi", "-i", "sine=f=440:d=20", "-c:v", "libx264", "-preset", "ultrafast", "-g", "50",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p)],
            check=True,
        )
        out[ext] = p
    return out


@pytest.mark.parametrize("ext", ["ts", "mp4"])
@pytest.mark.parametrize("offset", [5.3, 7.9, 11.1])
@pytest.mark.parametrize("copy", [True, False])
def test_mid_programme_join_never_goes_back_in_time(clips, ext, offset, copy):
    async def run():
        st = ChannelStream(StreamManager(), 1)
        st.ts_offset = 100.0  # 100 s already aired; the next output may start at 101.4
        st.emitted_until = utcnow() + timedelta(seconds=3)
        plan = SegmentPlan(7, EntryKind.PROGRAMME, "p", utcnow(), 1.0, offset, source_id=1, copy=copy)
        plan.target = str(clips[ext])
        plan = await st._resolve_join(plan)
        p = await asyncio.create_subprocess_exec(*st._cmd(plan, False), stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL)
        out, _ = await p.communicate()
        return earliest_pts(out)

    first = asyncio.run(run())
    floor = 100.0 + MUX_START
    assert first is not None and first >= floor - 0.001, f"output starts {floor - first:.3f}s before the previous segment's end"
    assert first < floor + 0.5, f"output starts {first - floor:.2f}s late"
