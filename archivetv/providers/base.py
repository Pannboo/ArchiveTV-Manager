"""Provider abstraction.

Adapters only discover and describe content. They never schedule, publish to
Dispatcharr, or decide approval. ``get_playback_info`` describes the delivery
methods a provider offers and the permissions attached to them; it does not
promise an FFmpeg-playable URL.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..enums import Eligibility, PlaybackType


class ProviderError(Exception):
    pass


class QuotaExhausted(ProviderError):
    pass


@dataclass
class PlaybackOption:
    playback_type: PlaybackType
    url: str | None
    label: str | None = None
    eligibility: Eligibility = Eligibility.UNVERIFIED
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    size_bytes: int | None = None
    license_url: str | None = None
    # Content checksum as "<algorithm>:<hex>" (e.g. "sha1:8743…"), when the
    # provider publishes one. Identical checksums mean identical files.
    checksum: str | None = None
    # Whether this option may ever be used for IPTV. Embed/reference options
    # from YouTube are always False.
    iptv_capable: bool = False
    priority: int = 100


@dataclass
class MediaRecord:
    """Normalised metadata for one catalogue item, as produced by an adapter."""

    provider: str
    external_id: str
    title: str
    original_url: str | None = None
    parent_external_id: str | None = None
    description: str | None = None
    creator: str | None = None
    creator_id: str | None = None
    publication_date: str | None = None
    duration: float | None = None
    thumbnail_url: str | None = None
    tags: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    series: str | None = None
    season: int | None = None
    episode: int | None = None
    available: bool = True
    restrictions: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    playback: list[PlaybackOption] = field(default_factory=list)
    artwork_in_epg: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationResult:
    ok: bool
    eligibility: Eligibility
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class MediaProvider(ABC):
    name: str = "base"
    label: str = "Base"

    @abstractmethod
    async def search(self, query: str | None, filters: dict[str, Any], kind: str, max_results: int) -> list[MediaRecord]:
        """Run a discovery query. ``kind`` is a RuleKind value."""

    @abstractmethod
    async def get_metadata(self, media_id: str) -> MediaRecord | None:
        """Fetch fresh metadata for one item by external id."""

    async def expand(self, stubs: list[MediaRecord]) -> list[MediaRecord]:
        """Turn lightweight search hits into full catalogue records.

        ``search`` may return stubs (id + title only) so the discovery engine
        can skip already-known items before spending requests on metadata.
        A stub may expand to several records (an Archive item with several
        videos).
        """
        out: list[MediaRecord] = []
        for s in stubs:
            rec = await self.get_metadata(s.external_id)
            if rec:
                out.append(rec)
        return out

    async def get_playback_info(self, media_id: str) -> list[PlaybackOption]:
        rec = await self.get_metadata(media_id)
        return rec.playback if rec else []

    async def validate(self, media_id: str) -> ValidationResult:
        rec = await self.get_metadata(media_id)
        if rec is None:
            return ValidationResult(False, Eligibility.UNAVAILABLE, "not found")
        best = max((p.eligibility for p in rec.playback), default=Eligibility.REFERENCE_ONLY, key=_elig_rank)
        return ValidationResult(rec.available, best)

    async def check_availability(self, media_id: str) -> bool:
        rec = await self.get_metadata(media_id)
        return bool(rec and rec.available)

    def status(self) -> dict[str, Any]:
        return {"configured": True}

    async def aclose(self) -> None:  # pragma: no cover - optional
        pass


_RANK = {
    Eligibility.IPTV_READY: 5,
    Eligibility.IPTV_NEEDS_TRANSCODE: 4,
    Eligibility.EMBED_ONLY: 3,
    Eligibility.REFERENCE_ONLY: 2,
    Eligibility.UNVERIFIED: 1,
    Eligibility.UNAVAILABLE: 0,
}


def _elig_rank(e: Eligibility) -> int:
    return _RANK.get(Eligibility(e), 0)
