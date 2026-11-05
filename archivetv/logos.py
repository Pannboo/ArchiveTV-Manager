"""Uploaded channel logos.

Stored in ``<data>/logos`` (small, persistent, included in nothing else) and
served at ``/logos/<file>``. Only raster formats identified by their magic
bytes are accepted — SVG is refused because it can carry script.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .config import settings

MAX_BYTES = 1024 * 1024
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
)
NAME = re.compile(r"^[a-z0-9-]{1,64}-[0-9a-f]{12}\.(png|jpg|webp)$")
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


class LogoError(ValueError):
    pass


def logo_dir() -> Path:
    return settings.data_dir / "logos"


def sniff(data: bytes) -> str:
    for sig, ext, _ in _SIGNATURES:
        if data.startswith(sig):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise LogoError("logo must be a PNG, JPEG or WebP image")


def save(slug: str, data: bytes, previous: str | None = None) -> str:
    """Store a logo and return its file name (content-addressed, so a new
    upload changes the URL and clients refetch it)."""
    if not data:
        raise LogoError("empty file")
    if len(data) > MAX_BYTES:
        raise LogoError(f"logo is larger than {MAX_BYTES // 1024} KB")
    ext = sniff(data)
    name = f"{slug}-{hashlib.sha256(data).hexdigest()[:12]}.{ext}"
    d = logo_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    if previous and previous != name:
        remove(previous)
    return name


def remove(name: str | None) -> None:
    if name and NAME.match(name):
        (logo_dir() / name).unlink(missing_ok=True)


def path_for(name: str) -> Path | None:
    if not NAME.match(name):
        return None
    p = logo_dir() / name
    return p if p.is_file() else None


def url(channel, base: str | None) -> str | None:
    """Public logo URL for a channel: an uploaded logo wins over a logo URL."""
    if channel.logo_file and base:
        return f"{base.rstrip('/')}/logos/{channel.logo_file}"
    return channel.logo_url
