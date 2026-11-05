"""Provider registry. Add a new provider by subclassing MediaProvider and
registering it here; the catalogue, scheduler and feeds need no changes."""

from __future__ import annotations

from .base import MediaProvider, MediaRecord, PlaybackOption, ProviderError, QuotaExhausted

_registry: dict[str, MediaProvider] = {}


def _factories():
    from .archive import ArchiveProvider
    from .youtube import YouTubeProvider

    return {"archive": ArchiveProvider, "youtube": YouTubeProvider}


def get_provider(name: str) -> MediaProvider:
    if name not in _registry:
        factories = _factories()
        if name not in factories:
            raise ProviderError(f"unknown provider '{name}'")
        _registry[name] = factories[name]()
    return _registry[name]


def provider_names() -> list[str]:
    return list(_factories())


def provider_labels() -> dict[str, str]:
    return {k: v.label for k, v in _factories().items()}


def set_provider(name: str, provider: MediaProvider) -> None:
    """Override a provider instance (tests)."""
    _registry[name] = provider


async def close_all() -> None:
    for p in list(_registry.values()):
        await p.aclose()
    _registry.clear()


__all__ = [
    "MediaProvider",
    "MediaRecord",
    "PlaybackOption",
    "ProviderError",
    "QuotaExhausted",
    "get_provider",
    "provider_names",
    "provider_labels",
    "set_provider",
    "close_all",
]
