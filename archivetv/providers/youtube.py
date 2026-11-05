"""YouTube discovery adapter — official YouTube Data API v3 only.

YouTube is a *discovery* provider here. Records it produces carry only
embed/reference playback options; nothing in this module resolves, extracts
or downloads media streams. Broadcasting a YouTube-discovered programme
requires a separately authorised PlaybackSource attached by an administrator.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from ..config import settings
from ..enums import Eligibility, PlaybackType, RuleKind
from ..quota import QuotaTracker
from .base import MediaProvider, MediaRecord, PlaybackOption, ProviderError, QuotaExhausted

API = "https://www.googleapis.com/youtube/v3"
OEMBED = "https://www.youtube.com/oembed"
KEYLESS_FLAG = "Imported without a YouTube API key (oEmbed): duration, description and restrictions are unknown"
UNOFFICIAL_FLAG = ("Metadata from YouTube's unofficial private API (ytscrape), used because no API key is set; "
                   "refreshed from the official API once a key is added")
# Quota units per call (YouTube Data API v3 cost table).
COST = {"search": 100, "videos": 1, "playlistItems": 1, "channels": 1, "videoCategories": 1, "playlists": 1}

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PLAYLIST_ID = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_HANDLE = re.compile(r"^@[A-Za-z0-9._-]{3,30}$")
_ISO_DUR = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")

YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com"}

DISCOVERY_ONLY_FLAG = (
    "YouTube is a discovery source only: attach an independently authorised IPTV playback source "
    "(rights-holder file, approved direct URL or licensed stream) before this can air"
)


def parse_youtube_url(value: str) -> tuple[str, str] | None:
    """Normalise a YouTube URL (or bare id/handle) to (kind, canonical id)."""
    v = (value or "").strip()
    if not v:
        return None
    if _HANDLE.match(v):
        return ("handle", v)
    if _CHANNEL_ID.match(v):
        return ("channel", v)
    if _VIDEO_ID.match(v):
        return ("video", v)
    if "://" not in v:
        v = "https://" + v
    try:
        parts = urlsplit(v)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    qs = parse_qs(parts.query)
    if host in ("youtu.be", "www.youtu.be"):
        vid = path.strip("/").split("/")[0]
        return ("video", vid) if _VIDEO_ID.match(vid) else None
    if host not in YT_HOSTS:
        return None
    segs = [s for s in path.split("/") if s]
    if path.startswith("/watch"):
        vid = (qs.get("v") or [""])[0]
        if _VIDEO_ID.match(vid):
            return ("video", vid)
        lst = (qs.get("list") or [""])[0]
        return ("playlist", lst) if _PLAYLIST_ID.match(lst) else None
    if path.startswith("/playlist"):
        lst = (qs.get("list") or [""])[0]
        return ("playlist", lst) if _PLAYLIST_ID.match(lst) else None
    if segs and segs[0] in ("shorts", "embed", "live", "v") and len(segs) > 1 and _VIDEO_ID.match(segs[1]):
        return ("video", segs[1])
    if segs and segs[0].startswith("@") and _HANDLE.match(segs[0]):
        return ("handle", segs[0])
    if len(segs) > 1 and segs[0] == "channel" and _CHANNEL_ID.match(segs[1]):
        return ("channel", segs[1])
    return None


def parse_iso_duration(s: str | None) -> float | None:
    if not s:
        return None
    m = _ISO_DUR.match(s)
    if not m:
        return None
    d, h, mi, sec = m.groups()
    return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec or 0)


def _rfc3339(v: Any) -> str | None:
    if not v:
        return None
    s = str(v)
    if len(s) == 10:
        return s + "T00:00:00Z"
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


class YouTubeProvider(MediaProvider):
    name = "youtube"
    label = "YouTube"

    def __init__(self, api_key: str | None = None, client: httpx.AsyncClient | None = None, quota: QuotaTracker | None = None):
        self.api_key = api_key if api_key is not None else settings.youtube_api_key
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=10), headers={"User-Agent": settings.http_user_agent}
        )
        self.quota = quota or QuotaTracker("youtube", settings.youtube_daily_quota, settings.youtube_quota_reserve)
        self._categories: dict[str, str] | None = None
        self._scraper = None  # ytscrape.AsyncYouTube, created on first unofficial use
        # Interactive imports (single URL) may dip into the quota reserve.
        self.use_reserve = False

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._scraper is not None:
            await self._scraper.close()
            self._scraper = None

    @property
    def unofficial(self) -> bool:
        """ytscrape is used only when opted in and no official key exists."""
        return bool(settings.youtube_unofficial and not self.api_key)

    def _scrape(self):
        if self._scraper is None:
            import ytscrape

            # Deliberately polite: one request at a time, spaced out, no
            # proxies and no bot-detection workarounds. If YouTube pushes
            # back, the scan stops and the error is shown on the dashboard.
            self._scraper = ytscrape.AsyncYouTube(
                region="GB", language="en", timeout=20.0, max_concurrency=1, max_retries=2,
                min_interval=float(max(1, settings.youtube_unofficial_interval)),
            )
        return self._scraper

    async def _scrape_call(self, coro_fn):
        import ytscrape

        try:
            return await coro_fn(self._scrape())
        except (ytscrape.BotDetected, ytscrape.CaptchaRequired, ytscrape.RateLimited, ytscrape.ConsentRequired) as e:
            raise ProviderError(f"YouTube refused unofficial access ({type(e).__name__}); add an API key to use the official API") from e
        except ytscrape.YtScrapeError as e:
            raise ProviderError(f"Unofficial YouTube lookup failed: {type(e).__name__}: {str(e)[:160]}") from e

    def status(self) -> dict[str, Any]:
        return {
            "configured": bool(self.api_key) or self.unofficial,
            "unofficial": self.unofficial,
            # Without a key, single-video imports still work via official oEmbed.
            "keyless_import": not self.api_key,
            "quota_used": self.quota.used(),
            "quota_limit": self.quota.daily_limit,
        }

    async def _call(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise ProviderError("YouTube API key not configured (set YOUTUBE_API_KEY)")
        cost = COST.get(endpoint, 1)
        if not self.quota.can_spend(cost, use_reserve=self.use_reserve):
            raise QuotaExhausted("YouTube API quota budget exhausted for today")
        q = {k: v for k, v in params.items() if v not in (None, "")}
        q["key"] = self.api_key
        last: Exception | None = None
        for _attempt in range(2):
            try:
                r = await self._client.get(f"{API}/{endpoint}", params=q)
            except httpx.TransportError as e:
                last = e
                continue
            self.quota.spend(cost)
            if r.status_code == 200:
                return r.json()
            reason = ""
            try:
                err = r.json().get("error", {})
                reason = (err.get("errors") or [{}])[0].get("reason", "") or err.get("status", "")
                msg = err.get("message", r.text[:200])
            except ValueError:
                msg = r.text[:200]
            if r.status_code == 403 and reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
                self.quota.mark_exhausted()
                raise QuotaExhausted(f"YouTube quota exceeded: {msg}")
            if r.status_code >= 500:
                last = ProviderError(f"YouTube API {r.status_code}: {msg}")
                continue
            # Never echo the request URL: it contains the API key.
            raise ProviderError(f"YouTube API {r.status_code} ({reason or 'error'}): {msg}")
        raise ProviderError(f"YouTube API request failed: {type(last).__name__}")

    async def resolve_channel(self, ref: str) -> tuple[str, str, str]:
        """Return (channel_id, title, uploads_playlist_id) for a handle, URL or UC id."""
        parsed = parse_youtube_url(ref) or ("channel", ref)
        kind, value = parsed
        params: dict[str, Any] = {"part": "snippet,contentDetails"}
        if kind == "handle":
            params["forHandle"] = value
        elif kind == "channel":
            params["id"] = value
        else:
            raise ProviderError(f"not a channel reference: {ref}")
        data = await self._call("channels", params)
        items = data.get("items") or []
        if not items:
            raise ProviderError(f"YouTube channel not found: {ref}")
        ch = items[0]
        uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
        return ch["id"], ch["snippet"]["title"], uploads

    async def _playlist_video_ids(self, playlist_id: str, limit: int) -> list[str]:
        ids: list[str] = []
        token = None
        while len(ids) < limit:
            data = await self._call(
                "playlistItems",
                {"part": "contentDetails", "playlistId": playlist_id, "maxResults": min(50, limit - len(ids)), "pageToken": token},
            )
            for it in data.get("items") or []:
                vid = (it.get("contentDetails") or {}).get("videoId")
                if vid:
                    ids.append(vid)
            token = data.get("nextPageToken")
            if not token:
                break
        return ids

    async def search(self, query, filters, kind, max_results) -> list[MediaRecord]:
        filters = filters or {}
        limit = max(1, min(int(max_results), 500))
        if self.unofficial:
            return await self._search_unofficial(query, filters, kind, limit)
        playlist_of: str | None = None
        if kind == RuleKind.VIDEO:
            ref = parse_youtube_url(query or "")
            if not ref:
                raise ProviderError(f"Unrecognised YouTube URL: {query}")
            if ref[0] == "video":
                ids = [ref[1]]
            elif ref[0] == "playlist":
                ids, playlist_of = await self._playlist_video_ids(ref[1], limit), ref[1]
            else:
                _, _, uploads = await self.resolve_channel(ref[1])
                ids = await self._playlist_video_ids(uploads, limit)
        elif kind == RuleKind.PLAYLIST:
            ref = parse_youtube_url(filters.get("playlist") or query or "")
            pid = ref[1] if ref and ref[0] == "playlist" else (filters.get("playlist") or query or "").strip()
            ids, playlist_of = await self._playlist_video_ids(pid, limit), pid
        elif kind == RuleKind.CHANNEL:
            # Enumerating the uploads playlist costs 1 unit/page vs 100 for search.
            _, _, uploads = await self.resolve_channel(filters.get("channel") or query or "")
            ids = await self._playlist_video_ids(uploads, limit)
        else:
            ids = await self._search_ids(query, filters, limit)
        return [
            MediaRecord(provider=self.name, external_id=v, title=v, raw={"playlist_id": playlist_of} if playlist_of else {})
            for v in dict.fromkeys(ids)
        ]

    async def _search_ids(self, query: str | None, filters: dict[str, Any], limit: int) -> list[str]:
        channel_id = None
        if filters.get("channel"):
            channel_id, _, _ = await self.resolve_channel(filters["channel"])
        duration = None
        mn, mx = filters.get("min_duration"), filters.get("max_duration")
        if mn and float(mn) >= 20 * 60:
            duration = "long"
        elif mx and float(mx) <= 4 * 60:
            duration = "short"
        cats = [c for c in str(filters.get("categories") or "").split(",") if c.strip().isdigit()]
        ids: list[str] = []
        token = None
        while len(ids) < limit:
            data = await self._call(
                "search",
                {
                    "part": "id",
                    "type": "video",
                    "q": query,
                    "channelId": channel_id,
                    "publishedAfter": _rfc3339(filters.get("published_after")),
                    "publishedBefore": _rfc3339(filters.get("published_before")),
                    "videoDuration": duration,
                    "videoCategoryId": cats[0].strip() if len(cats) == 1 else None,
                    "videoEmbeddable": "true" if filters.get("embeddable_only") else None,
                    "order": filters.get("order") or "date",
                    "regionCode": filters.get("region") or "GB",
                    "safeSearch": filters.get("safe_search") or "moderate",
                    "maxResults": min(50, limit - len(ids)),
                    "pageToken": token,
                },
            )
            for it in data.get("items") or []:
                vid = (it.get("id") or {}).get("videoId")
                if vid:
                    ids.append(vid)
            token = data.get("nextPageToken")
            if not token:
                break
        return ids

    async def _category_names(self) -> dict[str, str]:
        if self._categories is None:
            try:
                data = await self._call("videoCategories", {"part": "snippet", "regionCode": "GB"})
                self._categories = {c["id"]: c["snippet"]["title"] for c in data.get("items") or []}
            except ProviderError:
                self._categories = {}
        return self._categories

    async def _search_unofficial(self, query, filters, kind, limit) -> list[MediaRecord]:
        import ytscrape

        def stub(v: str) -> MediaRecord:
            return MediaRecord(provider=self.name, external_id=v, title=v)

        ref = parse_youtube_url(query or "") if kind in (RuleKind.VIDEO, RuleKind.CHANNEL) else None
        if kind == RuleKind.VIDEO and ref and ref[0] == "video":
            return [stub(ref[1])]
        if kind == RuleKind.PLAYLIST or (ref and ref[0] == "playlist"):
            raise ProviderError("YouTube playlists need the official API key (unofficial mode supports search and channels)")
        if kind in (RuleKind.CHANNEL, RuleKind.VIDEO):
            target = filters.get("channel") or query or ""
            ref = parse_youtube_url(target) or ("channel", target.strip())
            chan = await self._unofficial_channel_id(ref)
            videos = await self._scrape_call(lambda yt: yt.channel_videos(chan, max_results=limit))
        else:
            if not (query or "").strip():
                raise ProviderError("a keyword search needs a query")
            videos = await self._scrape_call(lambda yt: yt.search(query, filter=ytscrape.SearchFilter.VIDEOS, max_results=limit))
        ids = []
        async for v in videos:
            if getattr(v, "video_id", None):
                ids.append(v.video_id)
            if len(ids) >= limit:
                break
        return [stub(v) for v in dict.fromkeys(ids)]

    async def _unofficial_channel_id(self, ref: tuple[str, str]) -> str:
        """UC… ids are used directly. @handles are resolved through channel
        search (exact handle match only): resolving them from the channel
        page lands on YouTube's cookie-consent wall, which is not bypassed."""
        import ytscrape

        kind, value = ref
        if kind == "channel" and value.startswith("UC"):
            return value
        if kind != "handle":
            raise ProviderError("Use the channel's /channel/UC… URL or its @handle in unofficial mode")
        handle = value if value.startswith("@") else f"@{value}"
        res = await self._scrape_call(lambda yt: yt.search(handle, filter=ytscrape.SearchFilter.CHANNELS, max_results=10))
        async for c in res:
            if (getattr(c, "handle", "") or "").lower() == handle.lower() and getattr(c, "channel_id", None):
                return c.channel_id
        raise ProviderError(f"Could not find YouTube channel {handle}; paste its /channel/UC… URL instead")

    async def _fetch_unofficial(self, vid: str) -> MediaRecord:
        import ytscrape

        try:
            d = await self._scrape_call(lambda yt: yt.video(vid))
        except ProviderError as e:
            cause = e.__cause__
            if isinstance(cause, ytscrape.AgeRestricted):  # subclass of VideoUnavailable: check first
                rec = await self.oembed_record(vid)
                rec.flags.append("Age-restricted on YouTube")
                return rec
            if isinstance(cause, ytscrape.VideoUnavailable):
                return _unavailable_record(vid)
            raise
        return details_to_record(d)

    async def fetch_videos(self, ids: list[str]) -> list[MediaRecord]:
        if self.unofficial:
            recs = [await self._fetch_unofficial(v) for v in ids]
        elif not self.api_key:
            recs = [await self.oembed_record(v) for v in ids]
        else:
            recs = await self._fetch_official(ids)
        return [with_stream_option(r) for r in recs]

    async def _fetch_official(self, ids: list[str]) -> list[MediaRecord]:
        out: list[MediaRecord] = []
        cats = await self._category_names() if ids else {}
        for i in range(0, len(ids), 50):
            batch = ids[i : i + 50]
            data = await self._call(
                "videos", {"part": "snippet,contentDetails,status,liveStreamingDetails", "id": ",".join(batch), "maxResults": 50}
            )
            found = {it["id"]: it for it in data.get("items") or []}
            for vid in batch:
                it = found.get(vid)
                out.append(video_to_record(it, cats) if it else _unavailable_record(vid))
        return out

    async def expand(self, stubs: list[MediaRecord]) -> list[MediaRecord]:
        recs = await self.fetch_videos([s.external_id for s in stubs])
        pl = {s.external_id: s.raw.get("playlist_id") for s in stubs}
        for r in recs:
            if pl.get(r.external_id):
                r.parent_external_id = pl[r.external_id]
                r.restrictions["playlist_id"] = pl[r.external_id]
        return recs

    async def get_metadata(self, media_id: str) -> MediaRecord | None:
        recs = await self.fetch_videos([media_id])
        return recs[0] if recs else None


    async def oembed_record(self, vid: str) -> MediaRecord:
        """Keyless single-video metadata from YouTube's official oEmbed endpoint.

        oEmbed gives title, channel and thumbnail only, so duration,
        description and restrictions stay unknown until a Data API key is
        configured and the item is refreshed. Its status codes tell us
        enough to choose the preview option: 200 public and embeddable,
        401/403 embedding disabled, 404/400 private or removed.
        """
        watch = f"https://www.youtube.com/watch?v={vid}"
        try:
            r = await self._client.get(OEMBED, params={"url": watch, "format": "json"})
        except httpx.TransportError as e:
            raise ProviderError(f"YouTube oEmbed request failed: {type(e).__name__}")
        if r.status_code in (400, 404):
            return _unavailable_record(vid)
        if r.status_code >= 500:
            raise ProviderError(f"YouTube oEmbed {r.status_code}")
        embeddable = r.status_code == 200
        d = r.json() if embeddable else {}
        author_url = d.get("author_url") or ""
        handle = author_url.rstrip("/").rsplit("/", 1)[-1] if "/@" in author_url else None
        flags = [DISCOVERY_ONLY_FLAG, KEYLESS_FLAG]
        if not embeddable:
            flags.append("Embedding disabled by the uploader — reference only")
        option = PlaybackOption(
            playback_type=PlaybackType.YOUTUBE_EMBED if embeddable else PlaybackType.REFERENCE,
            url=f"https://www.youtube.com/embed/{vid}" if embeddable else watch,
            label="YouTube embedded player" if embeddable else "YouTube page (reference)",
            eligibility=Eligibility.EMBED_ONLY if embeddable else Eligibility.REFERENCE_ONLY,
            iptv_capable=False,
            priority=900 if embeddable else 950,
        )
        return MediaRecord(
            provider="youtube", external_id=vid, title=d.get("title") or f"YouTube video {vid}", original_url=watch,
            creator=d.get("author_name"), creator_id=handle, thumbnail_url=d.get("thumbnail_url"),
            restrictions={"embeddable": embeddable, "metadata_source": "oembed"}, flags=flags, playback=[option],
            artwork_in_epg=False, raw={"oembed": d},
        )


STREAM_FLAG = ("Experimental YouTube streaming is on: this item can air once approved, but only authorise its rights "
               "if you are allowed to rebroadcast it")


def with_stream_option(rec: MediaRecord) -> MediaRecord:
    """Add the experimental yt-dlp stream source (ATV_YOUTUBE_STREAMING) to
    available, non-live videos. It still needs approval, a passing
    validation and explicitly authorised rights before anything airs."""
    if not settings.youtube_streaming or not rec.available:
        return rec
    if (rec.restrictions or {}).get("live_broadcast") == "upcoming":
        return rec  # added when the item is refreshed after it goes live
    rec.playback.append(PlaybackOption(
        playback_type=PlaybackType.YOUTUBE_STREAM, url=f"https://www.youtube.com/watch?v={rec.external_id}",
        label="YouTube stream (experimental, yt-dlp)", eligibility=Eligibility.UNVERIFIED, iptv_capable=True, priority=500,
    ))
    # The "discovery only" note no longer applies once streaming is on.
    rec.flags = [f for f in rec.flags if f != DISCOVERY_ONLY_FLAG] + [STREAM_FLAG]
    return rec


def details_to_record(d: Any) -> MediaRecord:
    """Map ytscrape VideoDetails onto a preview-only catalogue record."""
    vid = d.video_id
    embeddable = bool(getattr(d, "embed_url", None))
    countries = set(getattr(d, "available_countries", None) or ())
    live = bool(getattr(d, "is_live", False))
    flags = [DISCOVERY_ONLY_FLAG, UNOFFICIAL_FLAG]
    if not embeddable:
        flags.append("Embedding disabled by the uploader — reference only")
    if live:
        flags.append("Live broadcast")
    if countries and "GB" not in countries:
        flags.append("Not available in GB")
    if getattr(d, "is_family_safe", True) is False:
        flags.append("Not family-safe on YouTube")
    pub = getattr(d, "published_at", None) or getattr(d, "uploaded_at", None)
    pub_s = pub.date().isoformat() if hasattr(pub, "date") else (str(getattr(d, "upload_date", "") or "")[:10] or None)
    thumb = getattr(d, "thumbnail", None)
    option = PlaybackOption(
        playback_type=PlaybackType.YOUTUBE_EMBED if embeddable else PlaybackType.REFERENCE,
        url=f"https://www.youtube.com/embed/{vid}" if embeddable else f"https://www.youtube.com/watch?v={vid}",
        label="YouTube embedded player" if embeddable else "YouTube page (reference)",
        eligibility=Eligibility.EMBED_ONLY if embeddable else Eligibility.REFERENCE_ONLY,
        iptv_capable=False,
        priority=900 if embeddable else 950,
    )
    private = bool(getattr(d, "is_private", False))
    category = getattr(d, "category", None)
    return MediaRecord(
        provider="youtube", external_id=vid, title=d.title or vid, original_url=f"https://www.youtube.com/watch?v={vid}",
        description=getattr(d, "description", None), creator=getattr(d, "channel", None), creator_id=getattr(d, "channel_id", None),
        publication_date=pub_s, duration=float(d.length_seconds) if getattr(d, "length_seconds", None) else None,
        thumbnail_url=getattr(thumb, "url", thumb) if thumb else None, tags=list(getattr(d, "keywords", None) or [])[:30],
        categories=[category] if category else [], available=not private,
        restrictions={"embeddable": embeddable, "live_broadcast": "live" if live else "none", "private": private,
                      "available_countries": sorted(countries)[:300] or None, "metadata_source": "ytscrape"},
        flags=flags, playback=[option], artwork_in_epg=False, raw={"source": "ytscrape"},
    )


def _unavailable_record(vid: str) -> MediaRecord:
    return MediaRecord(
        provider="youtube",
        external_id=vid,
        title=f"Unavailable YouTube video {vid}",
        original_url=f"https://www.youtube.com/watch?v={vid}",
        available=False,
        restrictions={"privacy_status": "unavailable"},
        flags=["Video is private, deleted or not returned by the API"],
        playback=[],
        artwork_in_epg=False,
    )


def video_to_record(it: dict[str, Any], categories: dict[str, str] | None = None) -> MediaRecord:
    sn = it.get("snippet") or {}
    cd = it.get("contentDetails") or {}
    st = it.get("status") or {}
    vid = it["id"]
    thumbs = sn.get("thumbnails") or {}
    thumb = next((thumbs[k]["url"] for k in ("maxres", "standard", "high", "medium", "default") if k in thumbs), None)
    privacy = st.get("privacyStatus")
    embeddable = bool(st.get("embeddable"))
    live = sn.get("liveBroadcastContent") or "none"
    region = cd.get("regionRestriction") or {}
    age_restricted = (cd.get("contentRating") or {}).get("ytRating") == "ytAgeRestricted"
    available = privacy in ("public", "unlisted") and st.get("uploadStatus", "processed") == "processed"
    restrictions = {
        "privacy_status": privacy,
        "embeddable": embeddable,
        "live_broadcast": live,
        "region_restriction": region or None,
        "age_restricted": age_restricted,
        "made_for_kids": st.get("madeForKids"),
        "licensed_content": cd.get("licensedContent"),
        "youtube_license": st.get("license"),
        "definition": cd.get("definition"),
        "caption": cd.get("caption"),
    }
    flags = [DISCOVERY_ONLY_FLAG]
    if not embeddable:
        flags.append("Embedding disabled by the uploader — reference only")
    if live != "none":
        flags.append(f"Live broadcast status: {live}")
    if age_restricted:
        flags.append("Age-restricted on YouTube")
    if region.get("blocked") and "GB" in region["blocked"]:
        flags.append("Blocked in GB")
    if region.get("allowed") and "GB" not in region["allowed"]:
        flags.append("Not allowed in GB")
    if st.get("license") == "creativeCommon":
        flags.append("Uploader selected Creative Commons (CC BY) on YouTube — this does not authorise stream extraction")

    if available and embeddable:
        playback = [
            PlaybackOption(
                playback_type=PlaybackType.YOUTUBE_EMBED,
                url=f"https://www.youtube.com/embed/{vid}",
                label="YouTube embedded player",
                eligibility=Eligibility.EMBED_ONLY,
                iptv_capable=False,
                priority=900,
            )
        ]
    else:
        playback = [
            PlaybackOption(
                playback_type=PlaybackType.REFERENCE,
                url=f"https://www.youtube.com/watch?v={vid}",
                label="YouTube page (reference)",
                eligibility=Eligibility.REFERENCE_ONLY if available else Eligibility.UNAVAILABLE,
                iptv_capable=False,
                priority=950,
            )
        ]
    cat_id = sn.get("categoryId")
    return MediaRecord(
        provider="youtube",
        external_id=vid,
        title=sn.get("title") or vid,
        original_url=f"https://www.youtube.com/watch?v={vid}",
        description=sn.get("description"),
        creator=sn.get("channelTitle"),
        creator_id=sn.get("channelId"),
        publication_date=(sn.get("publishedAt") or "")[:10] or None,
        duration=parse_iso_duration(cd.get("duration")),
        thumbnail_url=thumb,
        tags=list(sn.get("tags") or [])[:30],
        categories=[(categories or {}).get(cat_id, cat_id)] if cat_id else [],
        available=available,
        restrictions=restrictions,
        flags=flags,
        playback=playback,
        # YouTube thumbnails stay in the dashboard, not the published EPG.
        artwork_in_epg=False,
        raw={"category_id": cat_id},
    )
