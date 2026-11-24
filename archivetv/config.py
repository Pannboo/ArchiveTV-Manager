"""Runtime configuration.

Secrets (admin password, API keys) come only from the environment or from
Docker secret files (``<NAME>_FILE``). They are never stored in the database
and never rendered to the frontend.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


def _env(name: str, default: str | None = None) -> str | None:
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        try:
            return Path(file_path).read_text().strip()
        except OSError:
            pass
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str = "") -> list[str]:
    raw = _env(name, default) or ""
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("ATV_DATA_DIR", "./data")).resolve())
    media_dir: Path = field(default_factory=lambda: Path(_env("ATV_MEDIA_DIR", "./media")).resolve())
    cache_dir: Path = field(default_factory=lambda: Path(_env("ATV_CACHE_DIR", "./data/cache")).resolve())

    # Admin auth. If ATV_ADMIN_PASSWORD is unset a random one is generated at
    # startup and printed to the log once.
    admin_username: str = field(default_factory=lambda: _env("ATV_ADMIN_USERNAME", "admin") or "admin")
    admin_password: str | None = field(default_factory=lambda: _env("ATV_ADMIN_PASSWORD"))
    secret_key: str = field(default_factory=lambda: _env("ATV_SECRET_KEY") or secrets.token_hex(32))

    # Optional shared token for the feed endpoints (M3U/XMLTV/live). When set,
    # feeds require ?token=... and generated URLs include it.
    feed_token: str | None = field(default_factory=lambda: _env("ATV_FEED_TOKEN") or None)
    # Optional bearer token for the /api/v1 REST API (the admin session also works).
    api_token: str | None = field(default_factory=lambda: _env("ATV_API_TOKEN") or None)
    # Base URL Dispatcharr uses to reach this service, e.g. http://archivetv:8090
    public_base_url: str | None = field(default_factory=lambda: (_env("ATV_PUBLIC_BASE_URL") or "").rstrip("/") or None)

    youtube_api_key: str | None = field(default_factory=lambda: _env("YOUTUBE_API_KEY") or None)
    youtube_daily_quota: int = field(default_factory=lambda: _int("YOUTUBE_DAILY_QUOTA", 10000))
    # Keep this much quota in reserve for manual imports / refreshes.
    youtube_quota_reserve: int = field(default_factory=lambda: _int("YOUTUBE_QUOTA_RESERVE", 500))
    youtube_refresh_days: int = field(default_factory=lambda: _int("YOUTUBE_REFRESH_DAYS", 30))
    # Opt-in, unofficial YouTube discovery (ytscrape, YouTube's private
    # InnerTube API) used only while no API key is configured. Metadata only;
    # may conflict with YouTube's Terms of Service and can break without notice.
    youtube_unofficial: bool = field(default_factory=lambda: _bool("ATV_YOUTUBE_UNOFFICIAL", False))
    youtube_unofficial_interval: int = field(default_factory=lambda: _int("ATV_YOUTUBE_UNOFFICIAL_INTERVAL", 2))
    # Air approved, rights-authorised YouTube items by resolving their streams
    # with yt-dlp at play time. On by default; false makes YouTube discovery-only.
    youtube_streaming: bool = field(default_factory=lambda: _bool("ATV_YOUTUBE_STREAMING", True))
    youtube_stream_max_height: int = field(default_factory=lambda: _int("ATV_YOUTUBE_STREAM_MAX_HEIGHT", 1080))

    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo(_env("ATV_DISPLAY_TZ", "Europe/London") or "Europe/London"))
    schedule_days: int = field(default_factory=lambda: _int("ATV_SCHEDULE_DAYS", 7))

    # Schedule-based catch-up: programme history is kept this many days by
    # default (per-channel override) and replayed from the original source.
    catchup_days: int = field(default_factory=lambda: _int("ATV_CATCHUP_DAYS", 7))
    max_catchup_sessions: int = field(default_factory=lambda: _int("ATV_MAX_CATCHUP_SESSIONS", 3))
    # Credentials for the Xtream Codes-compatible upstream interface that
    # Dispatcharr ingests. The password is generated on first start if unset.
    xc_username: str = field(default_factory=lambda: _env("ATV_XC_USERNAME", "archivetv") or "archivetv")
    xc_password: str | None = field(default_factory=lambda: _env("ATV_XC_PASSWORD") or None)

    ffmpeg_path: str = field(default_factory=lambda: _env("ATV_FFMPEG", "ffmpeg") or "ffmpeg")
    ffprobe_path: str = field(default_factory=lambda: _env("ATV_FFPROBE", "ffprobe") or "ffprobe")
    max_streams: int = field(default_factory=lambda: _int("ATV_MAX_STREAMS", 4))
    stream_idle_seconds: int = field(default_factory=lambda: _int("ATV_STREAM_IDLE_SECONDS", 30))
    ffmpeg_threads: int = field(default_factory=lambda: _int("ATV_FFMPEG_THREADS", 2))
    transcode_preset: str = field(default_factory=lambda: _env("ATV_TRANSCODE_PRESET", "veryfast") or "veryfast")
    transcode_height: int = field(default_factory=lambda: _int("ATV_TRANSCODE_HEIGHT", 720))
    transcode_video_bitrate: str = field(default_factory=lambda: _env("ATV_TRANSCODE_VBITRATE", "2500k") or "2500k")
    # Seconds of output each channel may prepare ahead of real time, held in
    # RAM and released to viewers at playback speed. Absorbs origin slowdowns
    # (archive.org mirrors vary a lot) without writing media to disk. 0 = off.
    readahead_seconds: int = field(default_factory=lambda: _int("ATV_READAHEAD_SECONDS", 20))
    # Validation measures how fast a remote source downloads for this many
    # seconds (0 = skip) and flags it as slow below bitrate × ratio.
    speed_check_seconds: float = field(default_factory=lambda: _float("ATV_SPEED_CHECK_SECONDS", 6.0))
    min_speed_ratio: float = field(default_factory=lambda: _float("ATV_MIN_SPEED_RATIO", 1.5))
    # Remote files are fetched over this many parallel range requests through
    # an in-RAM relay (archive.org limits speed per connection). 1 = off.
    parallel_connections: int = field(default_factory=lambda: _int("ATV_PARALLEL_CONNECTIONS", 4))

    # Minimal-disk mode (default): nothing is ever downloaded or recorded to
    # disk; media is piped straight from the source through FFmpeg into RAM
    # queues. The optional prefetch cache below is refused while this is on.
    minimal_disk: bool = field(default_factory=lambda: _bool("ATV_DISABLE_MEDIA_RECORDING", True))
    cache_enabled: bool = field(default_factory=lambda: _bool("ATV_CACHE_ENABLED", False))
    cache_max_gb: int = field(default_factory=lambda: _int("ATV_CACHE_MAX_GB", 20))
    # Optional fallback to the parallel relay (off by default): with
    # minimal-disk mode off, sources the speed check flags as slow are
    # downloaded to disk before they air on channels being watched, and
    # deleted after. Downloads start this many hours ahead, or earlier when
    # the measured speed says a file needs longer.
    cache_slow_sources: bool = field(default_factory=lambda: _bool("ATV_CACHE_SLOW_SOURCES", False))
    cache_lead_hours: int = field(default_factory=lambda: _int("ATV_CACHE_LEAD_HOURS", 3))

    # Resource monitoring thresholds (alerts on the dashboard, /health and webhook).
    db_max_mb: int = field(default_factory=lambda: _int("ATV_DB_MAX_MB", 512))
    tmp_warn_percent: int = field(default_factory=lambda: _int("ATV_TMP_WARN_PERCENT", 80))
    mem_warn_percent: int = field(default_factory=lambda: _int("ATV_MEM_WARN_PERCENT", 85))
    # Operational history (playback events, sessions, audit) kept this long.
    history_keep_days: int = field(default_factory=lambda: _int("ATV_HISTORY_KEEP_DAYS", 90))

    # Remote hosts that admin-supplied direct media URLs may point at, in
    # addition to the Internet Archive. Suffix match ("example.org" matches
    # "cdn.example.org").
    extra_media_hosts: list[str] = field(default_factory=lambda: _list("ATV_ALLOWED_MEDIA_HOSTS"))
    # Allow media URLs that resolve to private/LAN addresses (e.g. a NAS).
    allow_private_media_hosts: bool = field(default_factory=lambda: _bool("ATV_ALLOW_PRIVATE_MEDIA_HOSTS", False))

    # Creative Commons / public-domain music for break cards (see cards.py).
    music_dir: Path = field(default_factory=lambda: Path(_env("ATV_MUSIC_DIR", "") or (Path(_env("ATV_MEDIA_DIR", "./media")) / "music")).resolve())
    # Break-card font: a fontconfig pattern or a .ttf/.otf path (e.g. an
    # OFL condensed display face such as Oswald dropped into the data dir).
    card_font: str | None = field(default_factory=lambda: _env("ATV_CARD_FONT") or None)
    # HLS output (/hls/<channel>/index.m3u8): segment length and window, in RAM.
    hls_segment_seconds: float = field(default_factory=lambda: _float("ATV_HLS_SEGMENT_SECONDS", 4.0))
    hls_window: int = field(default_factory=lambda: _int("ATV_HLS_WINDOW", 6))
    # Public read-only guide API (/api/public/...) for web players and apps.
    public_guide: bool = field(default_factory=lambda: _bool("ATV_PUBLIC_GUIDE", True))
    # Lab mode for channel packs: an auto_air pack airs every playable find,
    # Archive or YouTube, without the public-domain / Creative Commons check.
    # For a private lab, where you decide what you may watch.
    packs_air_everything: bool = field(default_factory=lambda: _bool("ATV_PACKS_AIR_EVERYTHING", False))
    # AI helpers (ai.py): theme checks, title clean-up and tagging, channel
    # vetting. Backend "local" is any OpenAI-compatible server on the lab
    # network (Ollama, LM Studio, llama.cpp); "anthropic" is Claude; "none"
    # turns them off. Keys are secrets: environment or Docker secret file only.
    ai_backend: str = field(default_factory=lambda: (_env("ATV_AI_BACKEND") or "").strip().lower()
                            or ("anthropic" if _env("ANTHROPIC_API_KEY") else "none"))
    ai_base_url: str = field(default_factory=lambda: (_env("ATV_AI_BASE_URL", "http://127.0.0.1:11434/v1") or "").rstrip("/"))
    ai_model: str = field(default_factory=lambda: _env("ATV_AI_MODEL", "qwen2.5:7b-instruct") or "qwen2.5:7b-instruct")
    ai_api_key: str | None = field(default_factory=lambda: _env("ATV_AI_API_KEY") or None)  # local servers rarely need one
    # Clean up titles, tag and classify every new find (free with a local model).
    ai_enrich: bool = field(default_factory=lambda: _bool("ATV_AI_ENRICH", True))
    anthropic_api_key: str | None = field(default_factory=lambda: _env("ANTHROPIC_API_KEY") or None)
    theme_check_model: str = field(default_factory=lambda: _env("ATV_THEME_CHECK_MODEL", "claude-opus-5-5") or "claude-opus-5-5")
    # YouTube keyword searches (100 quota units each, and a separate daily
    # call limit) shared by every pack per day; followed channels cost ~1 unit.
    youtube_search_budget: int = field(default_factory=lambda: _int("ATV_YOUTUBE_SEARCH_BUDGET", 30))

    notify_webhook_url: str | None = field(default_factory=lambda: _env("ATV_NOTIFY_WEBHOOK") or None)
    backup_keep: int = field(default_factory=lambda: _int("ATV_BACKUP_KEEP", 7))
    jobs_enabled: bool = field(default_factory=lambda: _bool("ATV_JOBS_ENABLED", True))
    http_user_agent: str = "ArchiveTV-Manager/2.0 (+self-hosted)"

    @property
    def cache_active(self) -> bool:
        """Any caching at all. It writes media to disk, so minimal-disk mode vetoes it."""
        return (self.cache_enabled or self.cache_slow_sources) and not self.minimal_disk

    @property
    def db_path(self) -> Path:
        return self.data_dir / "archivetv.sqlite3"

    def ensure_dirs(self) -> None:
        dirs = [self.data_dir, self.media_dir, self.data_dir / "backups", self.data_dir / "run"]
        if self.cache_active:
            dirs.append(self.cache_dir)
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
