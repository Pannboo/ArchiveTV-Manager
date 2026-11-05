"""Experimental YouTube streaming (proof of concept; opt-in, off by default).

With ``ATV_YOUTUBE_STREAMING=true``, YouTube items get a ``youtube_stream``
playback source. yt-dlp resolves its current stream addresses at play time;
FFmpeg reads them directly, exactly like any other remote source. Nothing is
downloaded or cached. The item still airs only after the administrator has
approved it and explicitly authorised its rights, so deciding what may be
rebroadcast stays a human decision.

Deliberately not done: logging in or passing cookies (age-restricted,
members-only or private videos simply fail), proxies, bot-check or PO-token
workarounds, and anything DRM-protected (yt-dlp refuses DRM formats). If
YouTube won't serve a video to an anonymous viewer, it doesn't air.

Live broadcasts are supported as live sources: a single live video, or a
channel's ``/live`` address (``youtube.com/@handle/live``), which always
points at whatever that channel is broadcasting now (recurring streams get
a new video id each time). Upcoming (not yet started) broadcasts fail until
they go live.

YouTube now serves separate video and audio streams, so a resolved source is
a pair; the stream engine gives FFmpeg two inputs. Addresses expire after a
few hours and are re-resolved as needed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from .config import settings
from .security import UnsafeURLError, check_url_resolves_public, host_matches

log = logging.getLogger(__name__)

STREAM_HOSTS = ["googlevideo.com"]
CACHE_SECONDS = 3600.0
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_LIVE = re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/(@[\w.-]{3,30}|channel/UC[\w-]{22})/live/?(?:\?.*)?$")
LIVE_CACHE_SECONDS = 300.0  # a channel's live address can move to a new broadcast


@dataclass
class Resolved:
    video: str
    audio: str | None
    user_agent: str
    vcodec: str | None
    acodec: str | None
    height: int | None
    duration: float | None
    expires: float  # monotonic
    live: bool = False


_cache: dict[str, Resolved] = {}  # video id -> resolved pair
_by_video_url: dict[str, Resolved] = {}


def enabled() -> bool:
    return bool(settings.youtube_streaming)


def channel_live_url(url: str) -> str | None:
    """Canonical ``https://www.youtube.com/<@handle|channel/UC…>/live`` or None."""
    m = _CHANNEL_LIVE.match((url or "").strip())
    return f"https://www.youtube.com/{m.group(1)}/live" if m else None


def cache_key(url: str) -> str | None:
    return channel_live_url(url) or video_id(url)


def video_id(url: str) -> str | None:
    from .providers.youtube import parse_youtube_url

    ref = parse_youtube_url(url or "")
    return ref[1] if ref and ref[0] == "video" and _VIDEO_ID.match(ref[1]) else None


def _format_selector() -> str:
    h = max(144, int(settings.youtube_stream_max_height))
    # Live broadcasts come as separate HLS video and audio playlists; VODs as
    # separate DASH video/audio. Prefer H.264 + AAC for a straight remux.
    live = (f"bv*[protocol^=m3u8][vcodec^=avc1][height<={h}]+ba[protocol^=m3u8]/"
            f"b[protocol^=m3u8][vcodec^=avc1][height<={h}]/")
    # H.264 + AAC remux straight into MPEG-TS; fall back to anything else
    # FFmpeg can transcode. Progressive (single-file) formats last.
    return (live + f"bv*[vcodec^=avc1][height<={h}]+ba[ext=m4a]/bv*[height<={h}]+ba/"
            f"b[vcodec^=avc1][acodec!=none][height<={h}]/b[acodec!=none][vcodec!=none]")


class _QuietLogger:
    """yt-dlp writes errors to stderr even when quiet; they come back as exceptions anyway."""

    def debug(self, msg: str) -> None:
        pass

    info = warning = error = debug


def _extract(target: str) -> dict:
    import yt_dlp

    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "format": _format_selector(),
        "socket_timeout": 20, "retries": 1, "cachedir": False, "logger": _QuietLogger(),
    }
    runtimes = {name: {} for name in ("deno", "node") if shutil.which(name)}
    if runtimes:
        opts["js_runtimes"] = runtimes
    with yt_dlp.YoutubeDL(opts) as ydl:
        url = target if target.startswith("https://") else f"https://www.youtube.com/watch?v={target}"
        return ydl.extract_info(url, download=False)


def _expiry(url: str) -> float:
    try:
        exp = int(parse_qs(urlsplit(url).query).get("expire", ["0"])[0])
    except ValueError:
        exp = 0
    remaining = exp - time.time() - 300 if exp else CACHE_SECONDS
    return time.monotonic() + max(60.0, min(CACHE_SECONDS, remaining))


async def resolve(url: str, fresh: bool = False) -> Resolved:
    """Current stream addresses for a YouTube video, validated like any media URL."""
    if not enabled():
        raise UnsafeURLError("experimental YouTube streaming is disabled (ATV_YOUTUBE_STREAMING)")
    key = cache_key(url)
    if not key:
        raise UnsafeURLError("not a YouTube video or channel /live URL")
    vid = key
    hit = _cache.get(vid)
    if hit and not fresh and hit.expires > time.monotonic():
        return hit
    try:
        info = await asyncio.wait_for(asyncio.to_thread(_extract, vid), 60)
    except asyncio.TimeoutError:
        raise UnsafeURLError("yt-dlp timed out resolving the video")
    except Exception as e:  # yt_dlp.utils.DownloadError and friends
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
        raise UnsafeURLError(f"YouTube stream unavailable: {msg[:200]}")
    if info.get("live_status") == "is_upcoming":
        raise UnsafeURLError("the broadcast has not started yet")
    live = bool(info.get("is_live") or info.get("live_status") == "is_live")
    if info.get("_has_drm") or info.get("has_drm"):
        raise UnsafeURLError("DRM-protected video")
    parts = info.get("requested_formats") or [info]
    v = next((p for p in parts if p.get("vcodec") not in (None, "none")), None)
    # The other requested part is the audio; YouTube's live audio playlists
    # don't declare a codec, so don't require one.
    a = next((p for p in parts if p is not v and p.get("vcodec") in (None, "none")), None) if len(parts) > 1 else None
    if v is None or not v.get("url"):
        raise UnsafeURLError("no playable video format")
    for p in (v, a):
        if p is None:
            continue
        host = (urlsplit(p["url"]).hostname or "").lower()
        if urlsplit(p["url"]).scheme != "https" or not host_matches(host, STREAM_HOSTS):
            raise UnsafeURLError(f"unexpected stream host {host}")
        await check_url_resolves_public(p["url"])
    res = Resolved(
        video=v["url"], audio=a["url"] if a else None,
        user_agent=(v.get("http_headers") or {}).get("User-Agent") or settings.http_user_agent,
        vcodec=v.get("vcodec"),
        acodec=(a.get("acodec") if a and a.get("acodec") not in (None, "none") else "mp4a") if a else v.get("acodec"),
        height=v.get("height"),
        duration=None if live else (float(info["duration"]) if info.get("duration") else None),
        expires=min(_expiry(v["url"]), time.monotonic() + LIVE_CACHE_SECONDS) if live or key.startswith("https://") else _expiry(v["url"]),
        live=live,
    )
    _cache[vid] = res
    _by_video_url[res.video] = res
    log.info("youtube %s resolved%s: %s %sp + %s", vid, " (live)" if live else "", res.vcodec, res.height, res.acodec)
    return res


def companion(target: str | None) -> Resolved | None:
    """The resolved pair a video target belongs to (for the audio input)."""
    return _by_video_url.get(target or "")


def forget(url: str) -> None:
    vid = cache_key(url)
    old = _cache.pop(vid, None) if vid else None
    if old:
        _by_video_url.pop(old.video, None)
