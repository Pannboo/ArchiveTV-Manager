"""Internet Archive discovery adapter (Advanced Search + Metadata APIs)."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import quote

import httpx

from ..config import settings
from ..enums import Eligibility, PlaybackType, RuleKind
from .base import MediaProvider, MediaRecord, PlaybackOption, ProviderError

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/{identifier}"
DOWNLOAD_URL = "https://archive.org/download/{identifier}/{name}"
DETAILS_URL = "https://archive.org/details/{identifier}"
EMBED_URL = "https://archive.org/embed/{identifier}"
THUMB_URL = "https://archive.org/services/img/{identifier}"

# Archive "format" values we can play, best first. H.264 MP4 derivatives remux
# straight to MPEG-TS; everything else needs a transcode.
FORMAT_PRIORITY: dict[str, tuple[int, bool]] = {
    "h.264": (10, True),
    "h.264 ia": (11, True),
    "512kb mpeg4": (20, True),
    "mpeg4": (25, True),
    "hires mpeg4": (30, False),
    "h.264 hd": (9, True),
    "matroska": (40, False),
    "webm": (45, False),
    "mpeg2": (50, False),
    "quicktime": (55, False),
    "ogg video": (60, False),
    "divx": (65, False),
    "windows media": (70, False),
    "cinepack": (80, False),
    "mpeg1": (75, False),
}
VIDEO_EXTS = {".mp4", ".m4v", ".mkv", ".webm", ".mpeg", ".mpg", ".mov", ".ogv", ".avi", ".wmv", ".ts"}
_STEM_SUFFIX = re.compile(r"(?:[._-](?:\d+kb|edit|hires|ia|hd|sd|ipod|archive))+$", re.I)
_SXXEXX = re.compile(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})")


def parse_length(value: Any) -> float | None:
    if value in (None, ""):
        return None
    s = str(value).strip()
    try:
        if ":" in s:
            secs = 0.0
            for part in s.split(":"):
                secs = secs * 60 + float(part)
            return secs
        return float(s)
    except ValueError:
        return None


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        out: list[str] = []
        for x in v:
            out.extend(_as_list(x))
        return out
    return [p.strip() for p in str(v).split(";") if p.strip()]


def _first(v: Any) -> str | None:
    if isinstance(v, list):
        return str(v[0]) if v else None
    return str(v) if v is not None else None


def normalise_stem(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    while "." in base and ("." + base.rsplit(".", 1)[1].lower()) in VIDEO_EXTS | {".ia"}:
        base = base.rsplit(".", 1)[0]
    base = _STEM_SUFFIX.sub("", base)
    return base.lower()


def _escape_term(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_query(query: str | None, filters: dict[str, Any], kind: str) -> str:
    parts = ["mediatype:(movies)"]
    if kind == RuleKind.VIDEO and query:
        parts.append(f"identifier:{_escape_term(query.strip())}")
        return " AND ".join(parts)
    if query:
        parts.append(f"({query})")
    for field, key in (("collection", "collection"), ("creator", "creator"), ("subject", "subject")):
        vals = _as_list(filters.get(key))
        if vals:
            parts.append(f"{field}:(" + " OR ".join(_escape_term(v) for v in vals) + ")")
    y0, y1 = filters.get("year_from"), filters.get("year_to")
    if y0 or y1:
        parts.append(f"year:[{int(y0) if y0 else '*'} TO {int(y1) if y1 else '*'}]")
    if filters.get("licensed_only"):
        parts.append("licenseurl:*")
    for kw in _as_list(filters.get("excluded_keywords")):
        parts.append(f"NOT {_escape_term(kw)}")
    return " AND ".join(parts)


class ArchiveProvider(MediaProvider):
    name = "archive"
    label = "Internet Archive"

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=10),
            headers={"User-Agent": settings.http_user_agent},
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_json(self, url: str, params: Any = None) -> Any:
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = await self._client.get(url, params=params)
                if r.status_code in (429, 502, 503, 504):
                    raise httpx.HTTPStatusError("retryable", request=r.request, response=r)
                r.raise_for_status()
                return r.json()
            except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as e:
                last = e
                await asyncio.sleep(1.5 * (attempt + 1))
        raise ProviderError(f"Internet Archive request failed: {last}")

    async def search(self, query, filters, kind, max_results) -> list[MediaRecord]:
        q = build_query(query, filters or {}, kind)
        params: list[tuple[str, str]] = [
            ("q", q),
            ("rows", str(max(1, min(int(max_results), 500)))),
            ("page", str(int((filters or {}).get("page", 1)))),
            ("output", "json"),
        ]
        for f in ("identifier", "title", "creator", "date", "licenseurl"):
            params.append(("fl[]", f))
        params.append(("sort[]", (filters or {}).get("sort") or "addeddate desc"))
        data = await self._get_json(SEARCH_URL, params)
        docs = (data.get("response") or {}).get("docs") or []
        return [
            MediaRecord(provider=self.name, external_id=d["identifier"], title=_first(d.get("title")) or d["identifier"])
            for d in docs
            if d.get("identifier")
        ]

    async def expand(self, stubs: list[MediaRecord]) -> list[MediaRecord]:
        sem = asyncio.Semaphore(4)

        async def one(stub: MediaRecord) -> list[MediaRecord]:
            async with sem:
                try:
                    return await self.get_item_records(stub.external_id)
                except ProviderError:
                    return []

        out: list[MediaRecord] = []
        for recs in await asyncio.gather(*(one(s) for s in stubs)):
            out.extend(recs)
        return out

    async def get_item_records(self, identifier: str) -> list[MediaRecord]:
        data = await self._get_json(METADATA_URL.format(identifier=quote(identifier, safe="")))
        if not data or "metadata" not in data:
            return []
        return records_from_metadata(data)

    async def get_metadata(self, media_id: str) -> MediaRecord | None:
        identifier = media_id.split("/", 1)[0]
        for rec in await self.get_item_records(identifier):
            if rec.external_id == media_id:
                return rec
        return None

    async def check_availability(self, media_id: str) -> bool:
        rec = await self.get_metadata(media_id)
        return bool(rec and rec.available and rec.playback)


def file_checksum(f: dict[str, Any]) -> str | None:
    """Strongest checksum the Archive publishes for a file."""
    for algo in ("sha1", "md5"):
        v = str(f.get(algo) or "").strip().lower()
        if v and all(c in "0123456789abcdef" for c in v):
            return f"{algo}:{v}"
    return None


def records_from_metadata(data: dict[str, Any]) -> list[MediaRecord]:
    """Turn one Archive item into catalogue records, one per distinct video.

    An Archive item often holds the same video several times (original upload
    plus derivatives such as ``.mp4``/``.ogv``). Files are grouped by their
    ``original`` link and normalised file stem; each group becomes one
    MediaRecord whose files are alternative playback options.
    """
    meta = data.get("metadata") or {}
    identifier = _first(meta.get("identifier")) or ""
    files = [f for f in data.get("files") or [] if isinstance(f, dict) and f.get("name")]
    item_title = _first(meta.get("title")) or identifier
    license_url = _first(meta.get("licenseurl"))
    restricted = str(_first(meta.get("access-restricted-item")) or "").lower() == "true" or bool(data.get("is_dark"))

    by_name = {f["name"]: f for f in files}
    video_files = []
    for f in files:
        fmt = str(f.get("format") or "").lower()
        ext = "." + f["name"].rsplit(".", 1)[-1].lower() if "." in f["name"] else ""
        if fmt in FORMAT_PRIORITY or (ext in VIDEO_EXTS and "thumb" not in fmt):
            if str(f.get("private") or "").lower() == "true":
                continue
            video_files.append(f)

    # Union-find over files: derivative->original, and same normalised stem.
    parent: dict[str, str] = {f["name"]: f["name"] for f in video_files}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    stems: dict[str, str] = {}
    for f in video_files:
        orig = f.get("original")
        if isinstance(orig, str) and orig in parent:
            union(f["name"], orig)
        stem = normalise_stem(f["name"])
        if stem in stems:
            union(f["name"], stems[stem])
        else:
            stems[stem] = f["name"]

    groups: dict[str, list[dict]] = {}
    for f in video_files:
        groups.setdefault(find(f["name"]), []).append(f)

    base_flags: list[str] = []
    if license_url:
        base_flags.append(f"Licence declared by uploader: {license_url} — confirm before authorising")
    else:
        base_flags.append("No licence information on the Archive item — rights must be verified manually")
    if restricted:
        base_flags.append("Archive marks this item access-restricted")

    tags = _as_list(meta.get("subject"))
    collections = _as_list(meta.get("collection"))
    records: list[MediaRecord] = []
    multi = len(groups) > 1
    for root, gfiles in sorted(groups.items(), key=lambda kv: kv[0].lower()):
        options: list[PlaybackOption] = []
        durations = []
        for f in gfiles:
            fmt = str(f.get("format") or "").lower()
            prio, _remuxable = FORMAT_PRIORITY.get(fmt, (90, False))
            length = parse_length(f.get("length"))
            if length:
                durations.append(length)
            options.append(
                PlaybackOption(
                    playback_type=PlaybackType.DIRECT_URL,
                    url=DOWNLOAD_URL.format(identifier=quote(identifier, safe=""), name=quote(f["name"])),
                    label=f"{f['name']} ({f.get('format') or 'video'})",
                    eligibility=Eligibility.UNVERIFIED,
                    container=f["name"].rsplit(".", 1)[-1].lower(),
                    width=_int(f.get("width")),
                    height=_int(f.get("height")),
                    duration=length,
                    size_bytes=_int(f.get("size")),
                    checksum=file_checksum(f),
                    license_url=license_url,
                    iptv_capable=not restricted,
                    priority=prio,
                )
            )
        options.sort(key=lambda o: o.priority)
        options.append(
            PlaybackOption(
                playback_type=PlaybackType.ARCHIVE_EMBED,
                url=EMBED_URL.format(identifier=quote(identifier, safe="")),
                label="Internet Archive embedded player",
                eligibility=Eligibility.EMBED_ONLY,
                priority=900,
            )
        )
        primary = by_name.get(root, gfiles[0])
        stem = normalise_stem(primary["name"])
        title = item_title
        if multi:
            ftitle = primary.get("title") or primary["name"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
            title = f"{item_title} — {ftitle}"
        m = _SXXEXX.search(primary["name"])
        records.append(
            MediaRecord(
                provider="archive",
                external_id=f"{identifier}/{stem}",
                parent_external_id=identifier,
                title=title,
                original_url=DETAILS_URL.format(identifier=quote(identifier, safe="")),
                description=_clean_html(_first(meta.get("description"))),
                creator=_first(meta.get("creator")),
                publication_date=_first(meta.get("date")),
                duration=max(durations) if durations else parse_length(meta.get("runtime")),
                thumbnail_url=THUMB_URL.format(identifier=quote(identifier, safe="")),
                tags=tags,
                categories=collections,
                season=int(m.group(1)) if m else None,
                episode=int(m.group(2)) if m else None,
                available=not restricted,
                restrictions={"license_url": license_url, "access_restricted": restricted, "collections": collections},
                flags=list(base_flags),
                playback=options,
                artwork_in_epg=True,
                raw={"identifier": identifier, "files": [f["name"] for f in gfiles]},
            )
        )
    return records


def _int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


_TAG = re.compile(r"<[^>]+>")


def _clean_html(s: str | None) -> str | None:
    if not s:
        return s
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    return _TAG.sub("", s).strip()
