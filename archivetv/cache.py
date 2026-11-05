"""Optional temporary cache for eligible remote media.

Disabled by default. When enabled, the next scheduled programme of each
active channel is fetched to the cache directory so a slow or flaky origin
doesn't interrupt playback. Files are evicted oldest-first beyond the size
limit; nothing here is a permanent download.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

import httpx

from .config import settings
from .security import resolve_media_url

log = logging.getLogger(__name__)
_inflight: set[str] = set()


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


async def prefetch(url: str) -> None:
    if not settings.cache_active or url in _inflight or cached(url):
        return
    _inflight.add(url)
    dest = cache_path(url)
    part = dest.with_suffix(dest.suffix + ".part")
    try:
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
    finally:
        _inflight.discard(url)
        await asyncio.sleep(0)
