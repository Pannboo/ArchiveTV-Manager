"""One-step "put on air": a single action replaces approve + authorise +
assign + regenerate, without bypassing the technical gates."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from archivetv import api_admin, discovery
from archivetv.config import settings
from archivetv.db import utcnow
from archivetv.enums import Eligibility, PlaybackType, ReviewStatus, RightsStatus
from archivetv.main import app
from archivetv.models import ChannelItem, DiscoveryRule, MediaItem, MediaSource, PlaybackSource, ScheduleEntry
from archivetv.providers import _registry
from archivetv.providers.base import MediaProvider, MediaRecord, PlaybackOption


@pytest.fixture()
def api(session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "admin_password", "pw")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from archivetv.web import admin

    admin._attempts.clear()
    c = TestClient(app)
    c.post("/api/v1/auth/login", json={"username": "admin", "password": "pw"})
    return c


def _item(s, n, provider="archive", ptype=PlaybackType.DIRECT_URL, elig=Eligibility.IPTV_READY,
          rights=RightsStatus.UNREVIEWED, status=ReviewStatus.PENDING_REVIEW):
    url = (f"https://www.youtube.com/embed/v{n}" if ptype == PlaybackType.YOUTUBE_EMBED
           else f"https://archive.org/download/x{n}/x{n}.mp4")
    it = MediaItem(source=MediaSource(provider=provider, external_id=f"x{n}"), title=f"Film {n}", title_key=f"film {n}",
                   duration=900.0, review_status=status)
    it.playback_sources.append(PlaybackSource(playback_type=ptype, url=url, eligibility=elig, rights_status=rights,
                                              duration=900.0, health_ok=True, seekable=True))
    s.add(it)
    s.commit()
    return it


def _entries(s, cid):
    s.expire_all()
    return s.query(ScheduleEntry).filter(ScheduleEntry.channel_id == cid, ScheduleEntry.media_item_id.isnot(None)).count()


def test_air_one_item_puts_it_on_the_schedule(api, session):
    it = _item(session, 1)
    ch = api.post("/api/v1/channels", json={"slug": "films", "name": "Films"}).json()
    d = api.post(f"/api/v1/media/{it.id}/air", json={"channel_id": ch["id"]}).json()
    assert d["air"] == {"id": it.id, "title": "Film 1", "on_air": True, "problem": None}
    assert d["review_status"] == "APPROVED" and d["playback_sources"][0]["rights_status"] == "AUTHORISED"
    # No separate "regenerate" step: the schedule already has it.
    assert _entries(session, ch["id"]) > 0
    detail = api.get(f"/api/v1/channels/{ch['id']}").json()
    assert detail["items"][0]["schedulable"] and detail["items"][0]["problem"] is None


def test_air_keeps_technical_and_restricted_gates(api, session, monkeypatch):
    monkeypatch.setattr(settings, "youtube_streaming", False)
    broken = _item(session, 1, elig=Eligibility.UNAVAILABLE)
    restricted = _item(session, 2, rights=RightsStatus.RESTRICTED)
    yt = _item(session, 3, provider="youtube", ptype=PlaybackType.YOUTUBE_EMBED, elig=Eligibility.EMBED_ONLY)
    ok = _item(session, 4)
    ch = api.post("/api/v1/channels", json={"slug": "mix", "name": "Mix"}).json()
    r = api.post("/api/v1/media/bulk", json={"ids": [broken.id, restricted.id, yt.id, ok.id], "action": "air",
                                             "channel_id": ch["id"]}).json()
    assert r["on_air"] == 1
    why = {x["id"]: x["problem"] for x in r["not_airing"]}
    assert "failed validation" in why[broken.id]
    assert "restricted" in why[restricted.id]
    assert "YouTube playback is switched off" in why[yt.id]
    session.expire_all()
    assert session.get(PlaybackSource, restricted.playback_sources[0].id).rights_status == RightsStatus.RESTRICTED
    assert session.get(PlaybackSource, yt.playback_sources[0].id).rights_status == RightsStatus.UNREVIEWED
    # All four are on the channel (so they're visible there with their problem), one airs.
    assert session.query(ChannelItem).filter_by(channel_id=ch["id"]).count() == 4


def test_paste_links_into_a_channel(api, session, monkeypatch):
    known = _item(session, 1)
    fresh = _item(session, 2, status=ReviewStatus.VALIDATING)

    async def fake_import(url):
        if "bad" in url:
            raise ValueError(api_admin.UNRECOGNISED_URL)
        res = discovery.DiscoveryResult(found=1)
        if url.endswith("x1"):
            res.skipped_known, res.known_item_ids = 1, [known.id]
        elif url.endswith("x2"):
            res.new, res.new_item_ids = 1, [fresh.id]
        else:
            res.errors.append("provider said no")
        return res

    monkeypatch.setattr(api_admin, "_import", fake_import)
    ch = api.post("/api/v1/channels", json={"slug": "links", "name": "Links"}).json()
    r = api.post(f"/api/v1/channels/{ch['id']}/links", json={"urls": [
        "https://archive.org/details/x1", "https://archive.org/details/x2", "https://bad.example/x", "https://archive.org/details/x3", "  "]})
    body = r.json()
    res = {x["url"]: x for x in body["results"]}
    assert len(res) == 4
    assert res["https://archive.org/details/x1"]["items"][0]["on_air"]
    assert res["https://archive.org/details/x2"]["items"][0]["on_air"]
    assert "unrecognised link" in res["https://bad.example/x"]["error"]
    assert res["https://archive.org/details/x3"]["error"] == "provider said no"
    assert [i["media_item_id"] for i in body["channel"]["items"]] == [known.id, fresh.id]  # pasted order
    assert _entries(session, ch["id"]) > 0


class _FakeProvider(MediaProvider):
    name = "fakeprov"
    label = "Fake"

    async def search(self, query, filters, kind, max_results):
        return [MediaRecord(provider=self.name, external_id=f"f{i}", title=f"Found {i}") for i in range(2)]

    async def get_metadata(self, media_id):
        return None

    async def expand(self, stubs):
        for st in stubs:
            st.duration = 600.0
            st.playback = [PlaybackOption(PlaybackType.DIRECT_URL, f"https://archive.org/download/{st.external_id}.mp4",
                                                  iptv_capable=True)]
        return stubs

    async def get_playback_info(self, media_id):
        return []

    async def validate(self, media_id):
        raise NotImplementedError

    async def check_availability(self, media_id):
        return True


@pytest.mark.parametrize("auto", [False, True])
def test_rule_auto_air(session, monkeypatch, auto):
    async def fake_validate(ps, *a, **k):
        broken = ps.url.endswith("f1.mp4")  # the second find fails its playback check
        ps.eligibility = Eligibility.UNAVAILABLE if broken else Eligibility.IPTV_READY
        ps.duration, ps.health_ok = 600.0, not broken

    async def no_notify(msg):
        pass

    monkeypatch.setitem(_registry, "fakeprov", _FakeProvider())
    monkeypatch.setattr(discovery, "validate_source", fake_validate)
    monkeypatch.setattr(discovery, "notify", no_notify)
    from archivetv.models import Channel

    ch = Channel(slug="auto", name="Auto", tvg_id="auto.atv")
    rule = DiscoveryRule(name="r", provider="fakeprov", kind="search", query="q", params={"auto_air": auto}, channels=[ch])
    session.add(rule)
    session.commit()
    res = asyncio.run(discovery.run_rule(rule.id))
    assert res.new == 2 and res.aired == (1 if auto else 0)
    session.expire_all()
    statuses = {i.title: i.review_status for i in session.query(MediaItem).all()}
    assert statuses["Found 0"] == (ReviewStatus.APPROVED if auto else ReviewStatus.PENDING_REVIEW)
    assert statuses["Found 1"] == ReviewStatus.PENDING_REVIEW  # broken finds always wait in the inbox
    assert (_entries(session, ch.id) > 0) is auto


def test_air_without_a_channel_says_so(api, session):
    it = _item(session, 1)
    r = api.post("/api/v1/media/bulk", json={"ids": [it.id], "action": "air"}).json()
    assert r["on_air"] == 0 and "not on any channel" in r["not_airing"][0]["problem"]
    session.expire_all()
    assert session.get(MediaItem, it.id).review_status == ReviewStatus.APPROVED  # approved + authorised all the same


def test_playlist_changes_reach_the_schedule(api, session):
    a, b = _item(session, 1), _item(session, 2)
    ch = api.post("/api/v1/channels", json={"slug": "order", "name": "Order", "mode": "sequential"}).json()
    api.post("/api/v1/media/bulk", json={"ids": [a.id, b.id], "action": "air", "channel_id": ch["id"]})

    def upcoming():
        # Only what starts after now: past entries are catch-up history and are never rewritten,
        # and the programme on air now is always kept.
        session.expire_all()
        rows = (session.query(ScheduleEntry).filter(ScheduleEntry.channel_id == ch["id"], ScheduleEntry.media_item_id.isnot(None),
                                                    ScheduleEntry.start > utcnow()).all())
        return [e.media_item_id for e in rows]

    assert set(upcoming()) == {a.id, b.id}
    items = api.get(f"/api/v1/channels/{ch['id']}").json()["items"]
    d = api.post(f"/api/v1/channels/{ch['id']}/items/order", json={"item_ids": [items[1]["id"], items[0]["id"]]}).json()
    assert [i["media_item_id"] for i in d["items"]] == [b.id, a.id]
    assert api.post(f"/api/v1/channels/{ch['id']}/items/order", json={"item_ids": [999]}).status_code == 400
    # Removing an item takes it out of the upcoming schedule without a manual rebuild.
    gone = next(i for i in d["items"] if i["media_item_id"] == a.id)
    api.delete(f"/api/v1/channels/{ch['id']}/items/{gone['id']}")
    assert set(upcoming()) == {b.id}
