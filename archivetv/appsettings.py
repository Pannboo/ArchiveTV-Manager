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
    "readahead_seconds": (int, "Read-ahead buffer (s)",
                          "Seconds of each channel prepared ahead of playback and held in RAM, so a slow source (e.g. a busy "
                          "archive.org mirror) drains the buffer instead of stalling viewers. 0 turns it off"),
    "cache_enabled": (bool, "Cache every source", "Also pre-fetch the next programmes of watched channels to disk, whatever their speed (needs minimal-disk mode off)"),
    "parallel_connections": (int, "Parallel connections per programme",
                             "Remote files are fetched over this many simultaneous range requests, held briefly in RAM. "
                             "archive.org limits speed per connection, so 4 is often several times faster than 1. 1 turns it off"),
    "cache_slow_sources": (bool, "Pre-download slow sources to disk",
                           "Fallback for sources still too slow with parallel connections: on watched channels, download sources "
                           "flagged slow before they air and delete them afterwards. Needs minimal-disk mode off"),
    "cache_lead_hours": (int, "Pre-download lead (hours)",
                         "Start slow-source downloads this long before air (longer when the measured speed requires it)"),
    "cache_max_gb": (int, "Cache size limit (GB)", "Oldest cached files are evicted beyond this"),
    "ai_backend": (str, "AI backend", "none, local (an OpenAI-compatible server such as Ollama or LM Studio on your network) "
                   "or anthropic (Claude; needs ANTHROPIC_API_KEY). Used for theme checks, title clean-up and channel vetting"),
    "ai_base_url": (str, "Local AI address", "OpenAI-compatible base URL, e.g. http://ai-box:11434/v1 for Ollama"),
    "ai_model": (str, "Local AI model", "Model name on that server, e.g. qwen2.5:7b-instruct"),
    "ai_enrich": (bool, "AI: clean up and tag new finds",
                  "Clean titles (year, episode), tag finds for time blocks, and hold back clips, trailers and reaction videos"),
    "youtube_search_budget": (int, "YouTube searches per day (all packs)",
                              "Keyword searches cost 100 quota units each and YouTube also caps them per day. Packs share this "
                              "budget; followed channels don't use it"),
    "packs_air_everything": (bool, "Lab mode: auto-air packs air every find",
                             "Channel packs set to auto-air put every playable find on air, Internet Archive or YouTube, without "
                             "checking for a public-domain or Creative Commons licence. For a private lab network only"),
    "youtube_daily_quota": (int, "YouTube daily quota", "Units available to this project per day"),
    "youtube_quota_reserve": (int, "YouTube quota reserve", "Units kept back for manual imports and refreshes"),
    "youtube_refresh_days": (int, "YouTube metadata refresh (days)", "Re-fetch stored YouTube metadata after this many days"),
    "youtube_streaming": (bool, "Air YouTube videos (yt-dlp)",
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
