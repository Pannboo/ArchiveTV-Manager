"""Technical validation of playback sources (FFprobe) and health checks."""

from __future__ import annotations

import asyncio
import time
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .cache import cached
from .config import settings
from .db import utcnow
from .enums import Eligibility, PlaybackType
from .models import PlaybackSource
from .security import FFMPEG_PROTOCOLS, UnsafeURLError, resolve_media_url, safe_local_path

log = logging.getLogger(__name__)

REMUX_VIDEO = {"h264"}
REMUX_AUDIO = {"aac", "mp3", "mp2", "ac3"}


@dataclass
class ProbeResult:
    ok: bool
    eligibility: Eligibility
    message: str | None = None
    info: dict[str, Any] = field(default_factory=dict)


async def ffprobe(target: str, timeout: float = 45) -> dict[str, Any]:
    args = [
        settings.ffprobe_path,
        "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        "-protocol_whitelist", FFMPEG_PROTOCOLS,
    ]
    if target.startswith("http"):
        args += ["-rw_timeout", "15000000", "-user_agent", settings.http_user_agent]
    args.append(target)
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("ffprobe timed out")
    if proc.returncode != 0:
        raise RuntimeError((err.decode(errors="replace").strip() or f"ffprobe exited {proc.returncode}")[:500])
    return json.loads(out or b"{}")


def classify(probe: dict[str, Any], live: bool = False) -> tuple[Eligibility, dict[str, Any], str | None]:
    streams = probe.get("streams") or []
    fmt = probe.get("format") or {}
    v = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    try:
        duration = float(fmt.get("duration") or (v or {}).get("duration") or 0) or None
    except ValueError:
        duration = None
    info = {
        "container": fmt.get("format_name"),
        "video_codec": (v or {}).get("codec_name"),
        "audio_codec": (a or {}).get("codec_name"),
        "width": (v or {}).get("width"),
        "height": (v or {}).get("height"),
        "duration": duration,
        "pix_fmt": (v or {}).get("pix_fmt"),
    }
    if not v:
        return Eligibility.UNAVAILABLE, info, "no video stream"
    if live:
        info["duration"] = None  # a live stream has no length
    elif not duration:
        return Eligibility.UNAVAILABLE, info, "duration could not be determined"
    remux = info["video_codec"] in REMUX_VIDEO and (a is None or info["audio_codec"] in REMUX_AUDIO) and info["pix_fmt"] in (None, "yuv420p", "yuvj420p")
    return (Eligibility.IPTV_READY if remux else Eligibility.IPTV_NEEDS_TRANSCODE), info, None


# Redirect chains (archive.org -> storage node) take ~1-2s to re-validate, so
# the stream engine reuses a vetted final URL briefly. Dropped on any failure.
TARGET_TTL = 600.0
_targets: dict[int, tuple[str, str, float]] = {}  # source id -> (source url, final url, expiry)


def forget_target(source_id: int | None) -> None:
    if source_id is not None:
        _targets.pop(source_id, None)


async def playable_target(ps: PlaybackSource, client: httpx.AsyncClient | None = None, use_cache: bool = False) -> str:
    """The exact string FFmpeg/FFprobe may receive for this source, after all safety checks.

    ``use_cache`` (stream engine only) allows the prefetch cache and recently
    resolved redirect targets; validation and health checks always go to the origin.
    """
    if ps.playback_type == PlaybackType.LOCAL_FILE:
        return str(safe_local_path(ps.local_path or ""))
    if ps.playback_type == PlaybackType.DIRECT_URL:
        url = ps.url or ""
        if use_cache and ps.allow_cache is not False:
            hit = cached(url)
            if hit:
                return str(hit)
        if use_cache and ps.id is not None:
            known = _targets.get(ps.id)
            if known and known[0] == url and known[2] > time.monotonic():
                return known[1]
        final = await resolve_media_url(url, client=client)
        if ps.id is not None:
            _targets[ps.id] = (url, final, time.monotonic() + TARGET_TTL)
        return final
    if ps.playback_type == PlaybackType.YOUTUBE_STREAM:
        from . import ytstream

        return (await ytstream.resolve(ps.url or "", fresh=not use_cache)).video
    raise UnsafeURLError(f"{ps.playback_type} sources are not IPTV-playable")


async def _range_supported(url: str) -> bool | None:
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": settings.http_user_agent}) as c:
            r = await c.get(url, headers={"Range": "bytes=0-0"})
            return r.status_code == 206
    except httpx.HTTPError:
        return None


async def validate_source(ps: PlaybackSource) -> ProbeResult:
    """Probe a PlaybackSource and update its technical fields in place.

    Never touches rights_status: technical compatibility and permission are
    separate decisions.
    """
    ps.last_validated_at = utcnow()
    if ps.playback_type in (PlaybackType.YOUTUBE_EMBED, PlaybackType.ARCHIVE_EMBED):
        ps.eligibility = Eligibility.EMBED_ONLY
        return ProbeResult(True, Eligibility.EMBED_ONLY)
    if ps.playback_type == PlaybackType.REFERENCE:
        ps.eligibility = Eligibility.REFERENCE_ONLY
        return ProbeResult(True, Eligibility.REFERENCE_ONLY)
    try:
        target = await playable_target(ps)
        probe = await ffprobe(target)
    except (UnsafeURLError, RuntimeError, OSError, httpx.HTTPError, json.JSONDecodeError) as e:
        msg = str(e)[:500]
        ps.eligibility = Eligibility.UNAVAILABLE
        ps.last_error = msg
        ps.failure_count += 1
        ps.health_ok = False
        return ProbeResult(False, Eligibility.UNAVAILABLE, msg)
    if ps.playback_type == PlaybackType.YOUTUBE_STREAM:
        # Video and audio are separate streams: probe found the video, the
        # resolver knows the audio codec.
        from . import ytstream

        pair = ytstream.companion(target)
        has_audio = pair is not None and (pair.audio is not None or pair.acodec not in (None, "none"))
        if has_audio and not any(st.get("codec_type") == "audio" for st in probe.get("streams") or []):
            probe.setdefault("streams", []).append({"codec_type": "audio", "codec_name": "aac" if pair.acodec.startswith("mp4a") else pair.acodec})
        ps.live = bool(pair and pair.live)
    elig, info, msg = classify(probe, live=bool(ps.live))
    ps.eligibility = elig
    ps.container = info["container"]
    ps.video_codec = info["video_codec"]
    ps.audio_codec = info["audio_codec"]
    ps.width = info["width"]
    ps.height = info["height"]
    ps.duration = info["duration"]
    if ps.live:
        ps.seekable = False
    elif ps.playback_type in (PlaybackType.LOCAL_FILE, PlaybackType.YOUTUBE_STREAM):
        ps.seekable = True
    else:
        ps.seekable = await _range_supported(target)
    ps.last_error = msg
    if elig in (Eligibility.IPTV_READY, Eligibility.IPTV_NEEDS_TRANSCODE):
        ps.failure_count = 0
        ps.health_ok = True
    return ProbeResult(elig != Eligibility.UNAVAILABLE, elig, msg, info)


async def health_check(ps: PlaybackSource) -> bool:
    """Cheap availability check (HEAD / file exists) without a full probe.

    Retries once before reporting failure; the scheduler only uses sources
    whose most recent check passed.
    """
    ok = False
    for attempt in range(2):
        try:
            await playable_target(ps)
            ok = True
            break
        except (UnsafeURLError, httpx.HTTPError, OSError) as e:
            ps.last_error = f"health check: {e}"[:500]
            if attempt == 0:
                await asyncio.sleep(3)
    if ok:
        ps.failure_count = 0
        ps.health_ok = True
    else:
        ps.failure_count += 1
        ps.health_ok = False
    ps.last_validated_at = utcnow()
    return ok
