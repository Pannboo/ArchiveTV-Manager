import asyncio
import json

import httpx
import pytest

from archivetv.enums import Eligibility, PlaybackType, RuleKind
from archivetv.providers.archive import build_query, normalise_stem, parse_length, records_from_metadata
from archivetv.providers.youtube import YouTubeProvider, parse_iso_duration, parse_youtube_url, video_to_record


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", ("video", "dQw4w9WgXcQ")),
        ("https://youtu.be/dQw4w9WgXcQ?t=10", ("video", "dQw4w9WgXcQ")),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=PLx", ("video", "dQw4w9WgXcQ")),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", ("video", "dQw4w9WgXcQ")),
        ("https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf", ("playlist", "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf")),
        ("https://www.youtube.com/@BritishPathe", ("handle", "@BritishPathe")),
        ("https://www.youtube.com/@BritishPathe/videos", ("handle", "@BritishPathe")),
        ("https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw", ("channel", "UC_x5XG1OV2P6uZZ5FSM9Ttw")),
        ("@BritishPathe", ("handle", "@BritishPathe")),
        ("https://example.com/watch?v=dQw4w9WgXcQ", None),
        ("https://www.youtube.com/watch?v=short", None),
    ],
)
def test_parse_youtube_url(url, expected):
    assert parse_youtube_url(url) == expected


def test_parse_iso_duration():
    assert parse_iso_duration("PT1H2M3S") == 3723
    assert parse_iso_duration("PT45S") == 45
    assert parse_iso_duration("P1DT1M") == 86460
    assert parse_iso_duration("bogus") is None


def test_youtube_record_is_never_iptv():
    rec = video_to_record(
        {
            "id": "dQw4w9WgXcQ",
            "snippet": {"title": "T", "channelTitle": "C", "channelId": "UCx", "publishedAt": "2009-10-25T06:57:33Z", "categoryId": "10",
                        "liveBroadcastContent": "none", "thumbnails": {"high": {"url": "https://i.ytimg.com/x.jpg"}}},
            "contentDetails": {"duration": "PT3M33S"},
            "status": {"privacyStatus": "public", "embeddable": True, "license": "creativeCommon", "uploadStatus": "processed"},
        },
        {"10": "Music"},
    )
    assert rec.duration == 213
    assert rec.categories == ["Music"]
    assert rec.artwork_in_epg is False
    assert [p.playback_type for p in rec.playback] == [PlaybackType.YOUTUBE_EMBED]
    assert all(not p.iptv_capable and p.eligibility == Eligibility.EMBED_ONLY for p in rec.playback)
    assert any("does not authorise stream extraction" in f for f in rec.flags)


def test_youtube_not_embeddable_is_reference_only():
    rec = video_to_record({"id": "dQw4w9WgXcQ", "snippet": {"title": "T"}, "contentDetails": {"duration": "PT1M"},
                           "status": {"privacyStatus": "public", "embeddable": False}})
    assert rec.playback[0].eligibility == Eligibility.REFERENCE_ONLY


def test_youtube_channel_rule_uses_cheap_enumeration(session):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        ep = request.url.path.rsplit("/", 1)[-1]
        calls.append(ep)
        assert "key" in request.url.params
        if ep == "channels":
            assert request.url.params["forHandle"] == "@Example"
            return httpx.Response(200, json={"items": [{"id": "UC" + "a" * 22, "snippet": {"title": "Ex"},
                                                        "contentDetails": {"relatedPlaylists": {"uploads": "UU" + "a" * 22}}}]})
        if ep == "playlistItems":
            return httpx.Response(200, json={"items": [{"contentDetails": {"videoId": "aaaaaaaaaaa"}}, {"contentDetails": {"videoId": "bbbbbbbbbbb"}}]})
        raise AssertionError(ep)

    from archivetv.quota import QuotaTracker

    yt = YouTubeProvider(api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                         quota=QuotaTracker("youtube", 10000, 0))
    stubs = asyncio.run(yt.search("@Example", {}, RuleKind.CHANNEL, 10))
    assert [s.external_id for s in stubs] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert "search" not in calls
    assert yt.quota.used() == 2


def test_youtube_quota_exhaustion_is_graceful(session):
    from archivetv.providers import QuotaExhausted
    from archivetv.quota import QuotaTracker

    def handler(request):
        return httpx.Response(403, json={"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}})

    yt = YouTubeProvider(api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), quota=QuotaTracker("youtube", 10000, 0))
    with pytest.raises(QuotaExhausted):
        asyncio.run(yt.search("cats", {}, RuleKind.SEARCH, 5))
    assert not yt.quota.can_spend(1)


def test_archive_grouping(fixtures_dir):
    data = json.loads((fixtures_dir / "ia_AboutBan1935.json").read_text())
    recs = records_from_metadata(data)
    assert len(recs) == 1, [r.external_id for r in recs]
    r = recs[0]
    assert r.external_id == "AboutBan1935/aboutban1935"
    assert r.title == "About Bananas"
    assert 660 < r.duration < 670
    direct = [p for p in r.playback if p.playback_type == PlaybackType.DIRECT_URL]
    assert direct[0].url.endswith("/AboutBan1935.mp4")  # h.264 derivative preferred
    assert all(p.url.startswith("https://archive.org/download/AboutBan1935/") for p in direct)
    assert not any(p.url.endswith(".rm") for p in direct)
    assert any("Licence declared" in f for f in r.flags)


def test_archive_multi_video_item():
    data = {
        "metadata": {"identifier": "show", "title": "The Show"},
        "files": [
            {"name": "The Show S01E01.mp4", "format": "h.264", "length": "1500"},
            {"name": "The Show S01E01.ogv", "format": "Ogg Video", "original": "The Show S01E01.mp4", "length": "1500"},
            {"name": "The Show S01E02.mp4", "format": "h.264", "length": "1490"},
        ],
    }
    recs = records_from_metadata(data)
    assert len(recs) == 2
    assert [(r.season, r.episode) for r in recs] == [(1, 1), (1, 2)]
    assert any("No licence information" in f for f in recs[0].flags)


def test_archive_helpers():
    assert parse_length("11:03") == 663
    assert parse_length("664.03") == 664.03
    assert normalise_stem("AboutBan1935_512kb.mp4") == "aboutban1935"
    q = build_query("cartoon", {"collection": "classic_tv;prelinger", "year_from": 1950, "excluded_keywords": "trailer"}, RuleKind.SEARCH)
    assert 'collection:("classic_tv" OR "prelinger")' in q and "year:[1950 TO *]" in q and 'NOT "trailer"' in q


def test_youtube_keyless_import_uses_oembed(session):
    """Without an API key a single video is imported via official oEmbed:
    preview-only, flagged, and never IPTV-eligible."""
    import asyncio

    import httpx

    from archivetv.enums import Eligibility, PlaybackType
    from archivetv.providers.youtube import KEYLESS_FLAG, YouTubeProvider

    def handler(req: httpx.Request) -> httpx.Response:
        assert "googleapis" not in req.url.host  # never the Data API without a key
        vid = req.url.params["url"].rsplit("=", 1)[-1]
        if vid == "public00001":
            return httpx.Response(200, json={"title": "Me at the zoo", "author_name": "jawed",
                                             "author_url": "https://www.youtube.com/@jawed",
                                             "thumbnail_url": "https://i.ytimg.com/vi/x/hqdefault.jpg"})
        return httpx.Response({"noembed00001": 401, "gone0000001": 404}[vid])

    async def run():
        yt = YouTubeProvider(api_key="", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        stubs = await yt.search("https://youtu.be/public00001", {}, "video", 1)
        recs = await yt.expand(stubs) + await yt.fetch_videos(["noembed00001", "gone0000001"])
        assert yt.status()["keyless_import"] is True
        try:
            await yt.search("https://www.youtube.com/@jawed", {}, "video", 10)
        except Exception as e:  # channels and search still need the Data API
            return recs, str(e)
        return recs, None

    (pub, noembed, gone), err = asyncio.run(run())
    assert pub.title == "Me at the zoo" and pub.creator == "jawed" and pub.creator_id == "@jawed"
    assert pub.playback[0].playback_type == PlaybackType.YOUTUBE_EMBED and pub.playback[0].eligibility == Eligibility.EMBED_ONLY
    assert KEYLESS_FLAG in pub.flags and pub.duration is None and not pub.artwork_in_epg
    assert noembed.playback[0].eligibility == Eligibility.REFERENCE_ONLY
    assert not gone.available
    assert all(not o.iptv_capable for r in (pub, noembed, gone) for o in r.playback)
    assert err and "API key" in err


def test_youtube_unofficial_backend(session, monkeypatch):
    """Opt-in ytscrape discovery: used only without a key, metadata only,
    preview-only records, and a clean stop when YouTube pushes back."""
    import asyncio
    from datetime import datetime as dt
    from types import SimpleNamespace as NS

    import pytest as _pytest
    import ytscrape

    from archivetv.config import settings
    from archivetv.enums import Eligibility, PlaybackType
    from archivetv.providers.base import ProviderError
    from archivetv.providers.youtube import UNOFFICIAL_FLAG, YouTubeProvider

    class Results:
        def __init__(self, ids):
            self.ids = ids

        def __aiter__(self):
            async def gen():
                for i in self.ids:
                    yield NS(video_id=i)
            return gen()

    calls = []

    class FakeScraper:
        async def search(self, q, filter=None, max_results=None):
            calls.append(("search", q, filter, max_results))
            if filter == ytscrape.SearchFilter.CHANNELS:
                chans = [NS(handle="@somechannelfan", channel_id="UCwrong"), NS(handle="@SomeChannel", channel_id="UCright")]

                class C:
                    def __aiter__(self):
                        async def gen():
                            for c in chans:
                                yield c
                        return gen()
                return C()
            return Results(["vidA0000001", "vidB0000001", "vidA0000001"])

        async def channel_videos(self, ch, max_results=None):
            calls.append(("channel", ch, max_results))
            return Results(["vidC0000001"])

        async def video(self, vid):
            if vid == "gone0000001":
                raise ytscrape.VideoUnavailable(vid, "removed")
            if vid == "botcheck001":
                raise ytscrape.BotDetected("not a bot")
            return NS(video_id=vid, title=f"Title {vid}", description="d", channel="Chan", channel_id="UCchan",
                      length_seconds=754, keywords=["cartoon"], is_live=False, thumbnail=NS(url="https://i.ytimg.com/x.jpg"),
                      published_at=dt(2020, 5, 1), uploaded_at=None, upload_date=None, category="Film & Animation",
                      embed_url=None if vid == "noembed0001" else f"https://www.youtube.com/embed/{vid}", is_private=False,
                      is_family_safe=True, available_countries=["GB", "US"])

        async def close(self):
            pass

    async def run():
        monkeypatch.setattr(settings, "youtube_unofficial", True)
        keyed = YouTubeProvider(api_key="real-key")
        assert not keyed.unofficial  # the official API always wins once a key exists
        await keyed.aclose()
        yt = YouTubeProvider(api_key="")
        yt._scraper = FakeScraper()
        assert yt.status()["unofficial"] and yt.status()["configured"]
        found = await yt.search("public domain cartoons", {}, "search", 5)
        chan = await yt.search("https://www.youtube.com/@somechannel", {}, "channel", 5)
        recs = await yt.expand(found + chan) + await yt.fetch_videos(["noembed0001", "gone0000001"])
        with _pytest.raises(ProviderError) as e1:
            await yt.fetch_videos(["botcheck001"])
        with _pytest.raises(ProviderError) as e2:
            await yt.search("https://www.youtube.com/playlist?list=PL123", {}, "playlist", 5)
        return found, chan, recs, str(e1.value), str(e2.value)

    found, chan, recs, bot_err, pl_err = asyncio.run(run())
    assert [r.external_id for r in found] == ["vidA0000001", "vidB0000001"]  # de-duplicated
    assert calls[0][0] == "search" and calls[0][2] == ytscrape.SearchFilter.VIDEOS
    assert calls[1][2] == ytscrape.SearchFilter.CHANNELS and calls[2] == ("channel", "UCright", 5)  # exact handle match only
    a = recs[0]
    assert a.duration == 754 and a.creator == "Chan" and a.publication_date == "2020-05-01" and UNOFFICIAL_FLAG in a.flags
    assert a.playback[0].playback_type == PlaybackType.YOUTUBE_EMBED and not a.playback[0].iptv_capable and not a.artwork_in_epg
    noembed, gone = recs[-2], recs[-1]
    assert noembed.playback[0].eligibility == Eligibility.REFERENCE_ONLY and not gone.available
    assert "refused unofficial access" in bot_err and "API key" in bot_err
    assert "playlists need the official API key" in pl_err
