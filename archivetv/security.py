"""Remote URL validation, SSRF protection and media-host allowlisting.

Anything handed to FFmpeg/FFprobe or fetched server-side passes through here.
Redirects are followed by us (never by FFmpeg) so every hop is checked.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from .config import settings

# Media hosts allowed for direct playback without extra configuration.
BUILTIN_MEDIA_HOSTS = ["archive.org"]

# YouTube's delivery infrastructure is never an IPTV source, regardless of
# allowlist configuration. Watch pages are not media files and extraction is
# out of scope by policy.
YOUTUBE_HOSTS = ["youtube.com", "youtu.be", "googlevideo.com", "youtube-nocookie.com", "ytimg.com", "ggpht.com"]

MEDIA_SCHEMES = {"http", "https"}


class UnsafeURLError(ValueError):
    pass


def host_matches(host: str, suffixes: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in suffixes)


def is_youtube_url(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return host_matches(host, YOUTUBE_HOSTS)


def allowed_media_hosts() -> list[str]:
    return BUILTIN_MEDIA_HOSTS + settings.extra_media_hosts


def _check_ip(ip: str) -> None:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if settings.allow_private_media_hosts and (addr.is_private and not addr.is_loopback and not addr.is_link_local):
        return
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        raise UnsafeURLError(f"address {addr} is not a public address")


def check_url_syntax(url: str) -> tuple[str, str]:
    """Static checks: scheme, host allowlist, YouTube ban. Returns (scheme, host)."""
    if not url or len(url) > 2048:
        raise UnsafeURLError("missing or overlong URL")
    try:
        parts = urlsplit(url.strip())
    except ValueError as e:
        raise UnsafeURLError(f"unparseable URL: {e}") from e
    scheme = (parts.scheme or "").lower()
    if scheme not in MEDIA_SCHEMES:
        raise UnsafeURLError(f"scheme '{scheme}' is not allowed")
    if parts.username or parts.password:
        raise UnsafeURLError("credentials in URLs are not allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise UnsafeURLError("URL has no host")
    if host_matches(host, YOUTUBE_HOSTS):
        raise UnsafeURLError(
            "YouTube URLs are not playable IPTV sources. Attach an independently authorised media file or stream instead."
        )
    if not host_matches(host, allowed_media_hosts()):
        raise UnsafeURLError(f"host '{host}' is not in the media host allowlist (ATV_ALLOWED_MEDIA_HOSTS)")
    return scheme, host


async def check_url_resolves_public(url: str) -> None:
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        ipaddress.ip_address(host)
        addrs = [host]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise UnsafeURLError(f"cannot resolve {host}: {e}") from e
        addrs = list({info[4][0] for info in infos})
    for a in addrs:
        _check_ip(a)


async def validate_media_url(url: str) -> str:
    check_url_syntax(url)
    await check_url_resolves_public(url)
    return url.strip()


async def resolve_media_url(url: str, client: httpx.AsyncClient | None = None, max_hops: int = 6) -> str:
    """Follow redirects ourselves, validating every hop. Returns the final URL.

    The final URL is what FFmpeg receives, so FFmpeg never follows a redirect
    to an unvetted host.
    """
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=15, headers={"User-Agent": settings.http_user_agent})
    try:
        current = await validate_media_url(url)
        for _ in range(max_hops):
            resp = await client.head(current, follow_redirects=False)
            if resp.status_code in (405, 501):
                resp = await client.get(current, headers={"Range": "bytes=0-0"}, follow_redirects=False)
                await resp.aclose()
            if resp.is_redirect:
                nxt = urljoin(current, resp.headers.get("location", ""))
                current = await validate_media_url(nxt)
                continue
            if resp.status_code >= 400:
                raise UnsafeURLError(f"HTTP {resp.status_code} from {urlsplit(current).hostname}")
            return current
        raise UnsafeURLError("too many redirects")
    finally:
        if own_client:
            await client.aclose()


def safe_local_path(path: str) -> Path:
    """Resolve a media-dir-relative (or absolute) path, refusing escapes."""
    base = settings.media_dir.resolve()
    p = Path(path)
    candidate = (p if p.is_absolute() else base / p).resolve()
    if candidate != base and base not in candidate.parents:
        raise UnsafeURLError("local files must live inside the media directory")
    if not candidate.is_file():
        raise UnsafeURLError(f"file not found: {candidate.relative_to(base) if base in candidate.parents else candidate}")
    return candidate


# FFmpeg input protocol whitelist: plain files and HTTP(S) only (HLS needs the
# same set; no concat/subfile/data/pipe tricks from remote playlists).
FFMPEG_PROTOCOLS = "file,http,https,tcp,tls,crypto"
