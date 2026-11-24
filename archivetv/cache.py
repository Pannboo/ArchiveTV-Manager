"""Optional temporary cache for eligible remote media (never in minimal-disk mode).

Two policies share the cache directory:

* **Slow sources** (``cache_slow_sources``, on by default once minimal-disk
  mode is off): on channels someone is watching, sources the speed check
  flagged as downloading too slowly to stream are fetched ahead of their
  airings and deleted once those airings are over (or the viewers leave).
  The stream engine airs them only from the finished file (see ``must_wait``).
* **Everything** (``cache_enabled``): the next programmes of channels being
  watched are fetched so a flaky origin doesn't interrupt playback.

Files are evicted oldest-first beyond the size limit; nothing here is a
permanent download.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

import httpx

from .catalogue import source_is_slow, source_speed_ratio
from .config import settings
from .enums import PlaybackType
from .models import PlaybackSource
from .security import resolve_media_url

log = logging.getLogger(__name__)
_inflight: set[str] = set()
# Downloads run a couple at a time so the soonest airing gets the bandwidth.
MAX_PARALLEL = 2
_slots: asyncio.Semaphore | None = None
# Margin on the measured speed when deciding how early a download must start.
LEAD_SAFETY = 1.5


def cacheable(ps: PlaybackSource) -> bool:
    return (settings.cache_active and ps.allow_cache is not False and ps.playback_type == PlaybackType.DIRECT_URL
            and bool(ps.url) and not ps.live)


def wants(ps: PlaybackSource) -> bool:
    """This source should be fetched to disk before it airs."""
    return cacheable(ps) and (settings.cache_enabled or (settings.cache_slow_sources and source_is_slow(ps)))


def must_wait(ps: PlaybackSource) -> bool:
    """Air this source only from the cache: it is too slow to stream and the
    slow-source cache is responsible for it."""
    return cacheable(ps) and settings.cache_slow_sources and source_is_slow(ps)


def lead_seconds(ps: PlaybackSource, duration: float) -> float:
    """How long before air a download should start: the configured lead, or
    longer when the measured speed means the whole file takes longer."""
    lead = settings.cache_lead_hours * 3600.0
    ratio = source_speed_ratio(ps)
    if ratio:
        lead = max(lead, duration / ratio * LEAD_SAFETY)
    return lead


def in_progress(url: str) -> bool:
    return url in _inflight


_tasks: dict[str, asyncio.Task] = {}  # url -> background download


def start_prefetch(url: str) -> None:
    """Fetch in the background (a no-op while that URL is already downloading)."""
    if url in _inflight or url in _tasks:
        return
    t = asyncio.create_task(prefetch(url))
    _tasks[url] = t
    t.add_done_callback(lambda _t: _tasks.pop(url, None))


def cancel_except(urls: set[str]) -> int:
    """Stop background downloads nobody needs any more (their partial file is
    removed). Returns how many were cancelled."""
    n = 0
    for url, t in list(_tasks.items()):
        if url not in urls and not t.done():
            t.cancel()
            n += 1
    return n


def cache_path(url: str) -> Path:
    h = hashlib.sha256(url.encode()).hexdigest()[:32]
    ext = url.rsplit(".", 1)[-1].lower() if "." in url.rsplit("/", 1)[-1] else "bin"
    return settings.cache_dir / f"{h}.{ext[:5]}"


def cached(url: str) -> Path | None:
    if not settings.cache_active:
        return None
    p = cache_path(url)
    if p.is_file():
        p.touch()
        return p
    return None


def evict() -> None:
    limit = settings.cache_max_gb * 1024**3
    files = sorted((f for f in settings.cache_dir.glob("*") if f.is_file() and not f.name.endswith(".part")), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    while files and total > limit:
        f = files.pop(0)
        total -= f.stat().st_size
        f.unlink(missing_ok=True)


def sweep(keep: set[Path]) -> int:
    """Delete finished cache files not in ``keep`` (slow-source mode cleans up
    after airings; downloads in progress are left alone). Returns how many."""
    if not settings.cache_dir.exists():
        return 0
    n = 0
    for f in settings.cache_dir.glob("*"):
        if f.is_file() and not f.name.endswith(".part") and f not in keep:
            f.unlink(missing_ok=True)
            n += 1
    return n


async def prefetch(url: str) -> None:
    global _slots
    if not settings.cache_active or url in _inflight or cached(url):
        return
    _inflight.add(url)
    _slots = _slots or asyncio.Semaphore(MAX_PARALLEL)
    dest = cache_path(url)
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        await _slots.acquire()
    except BaseException:
        _inflight.discard(url)
        raise
    try:
        log.info("caching %s", url)
        final = await resolve_media_url(url)
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15), headers={"User-Agent": settings.http_user_agent}) as c:
            async with c.stream("GET", final) as r:
                r.raise_for_status()
                with part.open("wb") as fh:
                    async for chunk in r.aiter_bytes(1 << 20):
                        fh.write(chunk)
        part.rename(dest)
        evict()
        log.info("cached %s", url)
    except Exception as e:  # noqa: BLE001
        log.warning("cache prefetch failed for %s: %s", url, e)
        part.unlink(missing_ok=True)
    except asyncio.CancelledError:
        log.info("cache download cancelled for %s", url)
        part.unlink(missing_ok=True)
        raise
    finally:
        _slots.release()
        _inflight.discard(url)
        await asyncio.sleep(0)
