"""Admin-tunable settings stored in the database (non-secret only)."""

from __future__ import annotations

from .config import settings
from .db import session_scope
from .models import AppSetting

# key -> (type, label, help)
TUNABLES: dict[str, tuple[type, str, str]] = {
    "public_base_url": (str, "Feed base URL", "URL Dispatcharr uses to reach this service, e.g. http://archivetv:8090"),
    "schedule_days": (int, "Schedule horizon (days)", "How far ahead schedules are generated"),
    "max_streams": (int, "Max simultaneous channels", "Limit on concurrently running FFmpeg channel streams"),
    "stream_idle_seconds": (int, "Idle stop delay (s)", "Stop a channel's FFmpeg this long after its last viewer leaves"),
    "ffmpeg_threads": (int, "FFmpeg threads", "Threads per FFmpeg process"),
    "transcode_preset": (str, "x264 preset", "ultrafast … medium; faster presets use less CPU"),
    "transcode_height": (int, "Transcode height", "Output height when transcoding (720 or 1080)"),
    "transcode_video_bitrate": (str, "Transcode video bitrate", "e.g. 2500k"),
    "cache_enabled": (bool, "Temporary caching", "Pre-fetch the next programme of active channels to local disk (refused while minimal-disk mode is on)"),
    "cache_max_gb": (int, "Cache size limit (GB)", "Oldest cached files are evicted beyond this"),
    "youtube_daily_quota": (int, "YouTube daily quota", "Units available to this project per day"),
    "youtube_quota_reserve": (int, "YouTube quota reserve", "Units kept back for manual imports and refreshes"),
    "youtube_refresh_days": (int, "YouTube metadata refresh (days)", "Re-fetch stored YouTube metadata after this many days"),
    "youtube_streaming": (bool, "Experimental: air YouTube videos (yt-dlp)",
                          "Adds a yt-dlp stream source to YouTube items so approved items whose rights you authorise can air. "
                          "Proof of concept outside the specification: you decide what you may rebroadcast."),
    "youtube_stream_max_height": (int, "YouTube stream max resolution",
                                  "Highest YouTube resolution to pick, e.g. 720 or 1080 (H.264 is remuxed, so higher costs bandwidth, not CPU)"),
    "youtube_unofficial": (bool, "Unofficial YouTube discovery (no key)",
                           "Use ytscrape (YouTube's private API) for search and channel rules while no API key is set. "
                           "Metadata only; may conflict with YouTube's Terms of Service and can break. The official API is used once a key is added."),
}


def _coerce(kind: type, raw: str):
    if kind is bool:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if kind is int:
        return int(raw)
    return raw.strip()


def apply_overrides() -> None:
    with session_scope() as s:
        for row in s.query(AppSetting).all():
            if row.key in TUNABLES:
                try:
                    val = _coerce(TUNABLES[row.key][0], row.value)
                except ValueError:
                    continue
                if row.key == "public_base_url":
                    val = (val or "").rstrip("/") or None
                setattr(settings, row.key, val)
    _sync_quota()


def save(values: dict[str, str]) -> list[str]:
    errors = []
    with session_scope() as s:
        for key, (kind, label, _) in TUNABLES.items():
            if kind is bool:
                raw = "true" if values.get(key) else "false"
            elif key in values:
                raw = values[key]
            else:
                continue
            try:
                _coerce(kind, raw)
            except ValueError:
                errors.append(f"{label}: invalid value")
                continue
            row = s.get(AppSetting, key) or AppSetting(key=key, value="")
            row.value = raw
            s.add(row)
    apply_overrides()
    return errors


def _sync_quota() -> None:
    from .providers import _registry

    yt = _registry.get("youtube")
    if yt is not None and hasattr(yt, "quota"):
        yt.quota.daily_limit = settings.youtube_daily_quota
        yt.quota.reserve = settings.youtube_quota_reserve
