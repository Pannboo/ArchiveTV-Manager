"""Channel packs: format, install, rights policy, theme check, self-filling."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from archivetv import packs
from archivetv.config import settings
from archivetv.discovery import DiscoveryResult, due_rules
from archivetv.enums import Eligibility, PackPolicy, PlaybackType, ReviewStatus, RightsStatus
from archivetv.models import Channel, ChannelItem, ChannelPack, DiscoveryRule, MediaItem, MediaSource, PlaybackSource

SPEC = {
    "id": "monster-movies", "name": "Monster Movies", "number": 13, "order": "shuffle", "policy": "auto_air",
    "target_hours": 2, "theme": "Monster movies", "public_domain_collections": ["prelinger"],
    "rules": [{"query": "monster OR creature", "params": {"collection": "feature_films", "min_duration": 600}}],
    "blocks": [{"name": "Creature Feature", "days": "45", "start": "22:00", "end": "02:00", "tag_filter": "horror"}],
}


def found(s, rule, n, *, licence=None, collections=None, provider="archive", yt_licence=None, dur=1800.0,
          ptype=PlaybackType.DIRECT_URL, elig=Eligibility.IPTV_READY):
    """A freshly discovered, validated item as discovery leaves it."""
    restr = {"license_url": licence, "access_restricted": False}
    if yt_licence:
        restr = {"youtube_license": yt_licence}
    it = MediaItem(source=MediaSource(provider=provider, external_id=f"f{n}", discovered_by_rule_id=rule.id, creator="Studio"),
                   title=f"Find {n}", duration=dur, review_status=ReviewStatus.PENDING_REVIEW, restrictions=restr,
                   categories=collections or ["feature_films"], tags=["horror"])
    it.playback_sources.append(PlaybackSource(playback_type=ptype, url=f"https://archive.org/download/f{n}/f{n}.mp4",
                                              eligibility=elig, duration=dur, health_ok=True, license_url=licence))
    s.add(it)
    s.commit()
    return it


def install(s, spec=SPEC, **kw):
    p = packs.install(s, packs.parse_spec(spec), **kw)
    s.commit()
    return p


def test_starter_packs_are_valid():
    starters = packs.starter_packs()
    assert len(starters) >= 10
    assert len({p.id for p in starters}) == len(starters)
    assert {"archive", "youtube"} <= {r.provider for p in starters for r in p.rules}


def test_invalid_specs_are_refused():
    with pytest.raises(packs.PackError):
        packs.parse_spec("{nope")
    with pytest.raises(packs.PackError):
        packs.parse_spec({**SPEC, "id": "Bad Id!"})
    with pytest.raises(packs.PackError):
        packs.parse_spec({**SPEC, "rules": []})
    with pytest.raises(packs.PackError):
        packs.parse_spec({**SPEC, "blocks": [{"name": "x", "start": "25:00", "end": "02:00"}]})


def test_install_creates_channel_rules_and_blocks(session):
    s = session
    p = install(s)
    ch = p.channel
    assert ch.slug == "monster-movies" and ch.number == 13 and ch.mode == "shuffle"
    assert ch.breaks_enabled and ch.break_seconds == 12
    assert [b.name for b in ch.blocks] == ["Creature Feature"]
    assert len(p.rules) == 1 and p.rules[0].channels == [ch]
    assert p.rules[0].params["sort"] == "downloads desc" and p.rules[0].params["page"] == 1
    with pytest.raises(packs.PackError):
        install(s)
    # An upgrade keeps the channel and replaces rules and blocks.
    up = install(s, {**SPEC, "version": 2, "blocks": [], "rules": SPEC["rules"] * 2}, upgrade=True)
    assert up.id == p.id and up.channel_id == ch.id and up.version == 2
    assert len(up.rules) == 2 and ch.blocks == []


def test_install_avoids_taken_slug_and_number(session):
    s = session
    s.add(Channel(slug="monster-movies", tvg_id="monster-movies.archivetv", name="Mine", number=13, seed="x"))
    s.commit()
    p = install(s)
    assert p.channel.slug == "monster-movies-2" and p.channel.number is None


def test_pack_rules_are_left_to_the_fill_job(session):
    s = session
    p = install(s)
    s.add(DiscoveryRule(name="manual", provider="archive", query="x"))
    s.commit()
    ids = due_rules()
    assert p.rules[0].id not in ids and len(ids) == 1


def test_auto_air_evidence(session):
    s = session
    p = install(s)
    rule = p.rules[0]
    pd = found(s, rule, 1, licence="http://creativecommons.org/publicdomain/mark/1.0/")
    none = found(s, rule, 2)
    coll = found(s, rule, 3, collections=["prelinger"])
    yt_cc = found(s, rule, 4, provider="youtube", yt_licence="creativeCommon", ptype=PlaybackType.YOUTUBE_STREAM)
    yt_std = found(s, rule, 5, provider="youtube", yt_licence="youtube", ptype=PlaybackType.YOUTUBE_STREAM)
    yt_off = found(s, rule, 6, provider="youtube", yt_licence="creativeCommon")
    ev = lambda it: packs.auto_air_evidence(it, packs._candidate_source(it), ["prelinger"])  # noqa: E731
    assert ev(pd)[0] and "publicdomain" in ev(pd)[1]
    assert not ev(none)[0]
    assert ev(coll)[0] and "prelinger" in ev(coll)[1]
    assert ev(yt_cc)[0]
    assert not ev(yt_std)[0]
    assert not ev(yt_off)[0] and "switched off" in ev(yt_off)[1]


def test_after_validation_airs_with_evidence_and_queues_the_rest(session):
    s = session
    p = install(s)
    rule = p.rules[0]
    ok = found(s, rule, 1, licence="https://creativecommons.org/licenses/by/4.0/")
    no = found(s, rule, 2)
    r = asyncio.run(packs.after_validation(rule.id, [ok.id, no.id]))
    assert r == {"aired": 1, "inbox": 1}
    s.expire_all()
    ok, no = s.get(MediaItem, ok.id), s.get(MediaItem, no.id)
    assert ok.review_status == ReviewStatus.APPROVED
    assert ok.playback_sources[0].rights_status == RightsStatus.AUTHORISED
    assert s.scalar(select(ChannelItem).where(ChannelItem.media_item_id == ok.id)).channel_id == p.channel_id
    assert no.review_status == ReviewStatus.PENDING_REVIEW
    assert any(f.startswith("Pack: ") and "licence" in f for f in no.flags)
    pk = s.get(ChannelPack, p.id)
    assert (pk.aired_count, pk.inbox_count) == (1, 1)


def test_inbox_policy_never_airs(session):
    s = session
    p = install(s, {**SPEC, "policy": "inbox"})
    it = found(s, p.rules[0], 1, licence="http://creativecommons.org/publicdomain/mark/1.0/")
    asyncio.run(packs.after_validation(p.rules[0].id, [it.id]))
    s.expire_all()
    assert s.get(MediaItem, it.id).review_status == ReviewStatus.PENDING_REVIEW


def test_rule_policy_overrides_pack_policy(session):
    s = session
    spec = {**SPEC, "rules": [{**SPEC["rules"][0], "policy": "inbox"}]}
    p = install(s, spec)
    assert p.rules[0].params["pack_policy"] == PackPolicy.INBOX
    it = found(s, p.rules[0], 1, licence="http://creativecommons.org/publicdomain/mark/1.0/")
    asyncio.run(packs.after_validation(p.rules[0].id, [it.id]))
    s.expire_all()
    assert s.get(MediaItem, it.id).review_status == ReviewStatus.PENDING_REVIEW


def test_theme_check_sends_off_theme_finds_to_review(session, monkeypatch):
    s = session
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_backend", "anthropic")
    p = install(s, theme_check=True)
    assert p.theme_check
    rule = p.rules[0]
    fits = found(s, rule, 1, licence="http://creativecommons.org/publicdomain/mark/1.0/")
    off = found(s, rule, 2, licence="http://creativecommons.org/publicdomain/mark/1.0/")
    seen = {}

    async def fake(name, theme, items):
        seen["theme"] = theme
        seen["titles"] = [i["title"] for _, i in items]
        return {fits.id: (True, "a monster movie"), off.id: (False, "a cooking show")}

    monkeypatch.setattr(packs, "theme_check", fake)
    r = asyncio.run(packs.after_validation(rule.id, [fits.id, off.id]))
    assert r == {"aired": 1, "inbox": 1}
    assert seen == {"theme": "Monster movies", "titles": ["Find 1", "Find 2"]}
    s.expire_all()
    assert s.get(MediaItem, fits.id).review_status == ReviewStatus.APPROVED
    off = s.get(MediaItem, off.id)
    assert off.review_status == ReviewStatus.PENDING_REVIEW and any("off-theme" in f for f in off.flags)


def test_theme_check_failures_never_pass(monkeypatch):
    """An API error counts as "doesn't fit": the find waits for review."""
    import anthropic
    import httpx2

    class Boom:
        def __init__(self, *a, **k):
            self.beta = self
            self.messages = self

        async def create(self, **kw):
            req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.InternalServerError("down", response=httpx2.Response(500, request=req), body=None)

        async def close(self):
            pass

    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    monkeypatch.setattr(settings, "ai_backend", "anthropic")
    monkeypatch.setattr(anthropic, "AsyncAnthropic", Boom)
    out = asyncio.run(packs.theme_check("X", "theme", [(1, {"title": "t"})]))
    assert out[1][0] is False


def test_fill_pages_forward_and_stops_at_target(session, monkeypatch):
    s = session
    p = install(s)
    rid = p.rules[0].id
    results = iter([DiscoveryResult(found=50, new=0), DiscoveryResult(found=0, new=0)])

    async def fake_run(rule_id, interactive=False):
        return next(results)

    import archivetv.discovery as disc

    monkeypatch.setattr(disc, "run_rule", fake_run)
    r = asyncio.run(packs.fill_pack(p.id, force=True))
    assert r["status"] == "filling"
    s.expire_all()
    assert s.get(DiscoveryRule, rid).params["page"] == 2  # nothing new on page 1: look further
    asyncio.run(packs.fill_pack(p.id, force=True))
    s.expire_all()
    assert s.get(DiscoveryRule, rid).params["page"] == 1  # past the last page: start over
    assert s.get(ChannelPack, p.id).last_fill_status == "exhausted"

    # Enough playable hours: the pack rests.
    for n in range(5):
        it = found(s, s.get(DiscoveryRule, rid), 10 + n, licence="x", dur=3600)
        it.review_status = ReviewStatus.APPROVED
        it.playback_sources[0].rights_status = RightsStatus.AUTHORISED
        s.add(ChannelItem(channel_id=p.channel_id, media_item_id=it.id, role="programme", position=n))
    s.commit()
    assert packs.playable_hours(s, s.get(ChannelPack, p.id).channel) == pytest.approx(5.0)
    r = asyncio.run(packs.fill_pack(p.id, force=True))
    assert r["status"] == "full"


def test_export_round_trips(session):
    s = session
    p = install(s)
    p.target_hours = 99
    s.commit()
    out = packs.export(p)
    assert out["target_hours"] == 99 and out["blocks"][0]["name"] == "Creature Feature"
    again = packs.parse_spec(json.dumps(out))
    assert again.id == "monster-movies" and again.rules[0].query == "monster OR creature"


def test_uninstall_keeps_channel_and_items(session):
    s = session
    p = install(s)
    it = found(s, p.rules[0], 1)
    ch_id = p.channel_id
    packs.uninstall(s, p, disable_channel=True)
    s.commit()
    assert s.scalars(select(DiscoveryRule)).all() == []
    assert s.get(Channel, ch_id).enabled is False
    assert s.get(MediaItem, it.id) is not None


# ------------------------------------------------------------------ API


@pytest.fixture()
def api(session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "admin_password", "pw")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from archivetv.web import admin
    from archivetv.main import app

    admin._attempts.clear()
    c = TestClient(app)
    c.post("/api/v1/auth/login", json={"username": "admin", "password": "pw"})
    from archivetv import api_packs

    monkeypatch.setattr(api_packs, "_start_fill", lambda pid: None)
    return c


def test_api_install_starter_patch_export_delete(api, session):
    r = api.get("/api/v1/packs")
    assert r.status_code == 200 and len(r.json()["starters"]) >= 10
    r = api.post("/api/v1/packs/install", json={"starter": "classroom-films"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["channel"]["slug"] == "classroom-films"
    assert api.post("/api/v1/packs/install", json={"starter": "classroom-films"}).status_code == 409
    assert api.get("/api/v1/packs").json()["starters"][1]["installed"] in (True, False)
    r = api.patch(f"/api/v1/packs/{pid}", json={"target_hours": 10, "policy": "inbox"})
    assert r.json()["target_hours"] == 10 and r.json()["policy"] == "inbox"
    assert api.patch(f"/api/v1/packs/{pid}", json={"theme_check": True}).status_code == 400  # no API key
    r = api.get(f"/api/v1/packs/{pid}/export")
    assert r.headers["content-disposition"].endswith('.archivetv-pack.json"') and r.json()["policy"] == "inbox"
    assert api.delete(f"/api/v1/packs/{pid}").json() == {"ok": True}


def test_api_install_pasted_spec_and_preview(api):
    r = api.post("/api/v1/packs/preview", json={"spec": SPEC})
    assert r.status_code == 200 and r.json()["installed"] is False
    assert api.post("/api/v1/packs/preview", json={"spec": {"id": "x"}}).status_code == 400
    assert api.post("/api/v1/packs/install", json={"spec": SPEC, "starter": "cartoons"}).status_code == 400
    assert api.post("/api/v1/packs/install", json={"spec": SPEC}).status_code == 201


def test_api_pack_url_must_be_public_https(api):
    assert api.post("/api/v1/packs/preview", json={"url": "http://example.com/p.json"}).status_code == 400
    assert api.post("/api/v1/packs/preview", json={"url": "https://127.0.0.1/p.json"}).status_code == 400


def test_api_requires_login(session):
    from archivetv.main import app

    assert TestClient(app).get("/api/v1/packs").status_code == 401


# ----------------------------------------------------------- weekly freshness

from datetime import timedelta  # noqa: E402

from archivetv.db import utcnow  # noqa: E402
from archivetv.models import AuditLog, ScheduleEntry  # noqa: E402
from archivetv.providers import MediaRecord, PlaybackOption, set_provider  # noqa: E402


class FakeArchive:
    name, label = "archive", "Internet Archive"

    def __init__(self, licences: dict[str, str | None], gone=()):
        self.licences, self.gone, self.searches = licences, set(gone), []

    async def get_item_records(self, identifier):
        if identifier in self.gone:
            return []
        lic = self.licences.get(identifier)
        return [MediaRecord(provider="archive", external_id=identifier, parent_external_id=identifier, title=f"Find {identifier}",
                            duration=1800.0, restrictions={"license_url": lic, "access_restricted": False},
                            categories=["feature_films"],
                            playback=[PlaybackOption(PlaybackType.DIRECT_URL, f"https://archive.org/download/{identifier}/{identifier}.mp4",
                                                     license_url=lic, iptv_capable=True, priority=10)])]

    async def search(self, query, filters, kind, max_results):
        self.searches.append(dict(filters))
        return []

    async def expand(self, stubs):
        return []


class FakeYouTube:
    name, label = "youtube", "YouTube"

    def __init__(self):
        self.expanded, self.searches = [], []

    async def expand(self, stubs):
        self.expanded += [s.external_id for s in stubs]
        return [MediaRecord(provider="youtube", external_id=s.external_id, title="Live set", duration=1800.0,
                            restrictions={"youtube_license": "creativeCommon"},
                            playback=[PlaybackOption(PlaybackType.YOUTUBE_STREAM, f"https://www.youtube.com/watch?v={s.external_id}",
                                                     iptv_capable=True, priority=500)]) for s in stubs]

    async def search(self, query, filters, kind, max_results):
        self.searches.append(dict(filters))
        return []


def aired(s, pack, n, *, licence="http://creativecommons.org/publicdomain/mark/1.0/", provider="archive"):
    """A find the pack itself put on air (approved and authorised as pack:<key>)."""
    it = found(s, pack.rules[0], n, licence=licence, provider=provider, yt_licence="creativeCommon" if provider == "youtube" else None)
    it.source.external_id = it.source.parent_external_id = f"f{n}"
    asyncio.run(packs.after_validation(pack.rules[0].id, [it.id]))
    s.expire_all()
    return s.get(MediaItem, it.id)


def test_refresh_is_weekly(session):
    s = session
    p = install(s)
    assert packs.refresh_due_at(p) == p.installed_at + timedelta(days=7)
    assert asyncio.run(packs.refresh_pack(p.id)) == {"skipped": "not due"}


def test_refresh_rechecks_tapes_and_catches_removals_and_licence_changes(session):
    s = session
    p = install(s)
    keep, gone, relicensed = aired(s, p, 1), aired(s, p, 2), aired(s, p, 3)
    assert {i.review_status for i in (keep, gone, relicensed)} == {ReviewStatus.APPROVED}
    archive = FakeArchive({"f1": "http://creativecommons.org/publicdomain/mark/1.0/", "f3": None}, gone={"f2"})
    set_provider("archive", archive)
    summary = asyncio.run(packs.refresh_pack(p.id, force=True))
    s.expire_all()
    assert summary["gone"] == 1 and summary["back_to_review"] == 1
    assert s.get(MediaItem, keep.id).review_status == ReviewStatus.APPROVED
    assert s.get(MediaItem, gone.id).review_status == ReviewStatus.UNAVAILABLE
    re = s.get(MediaItem, relicensed.id)
    assert re.review_status == ReviewStatus.PENDING_REVIEW
    assert re.playback_sources[0].rights_status == RightsStatus.UNREVIEWED
    assert any("weekly check" in f for f in re.flags)
    # Looked for new uploads with the newest first, whatever the channel's size.
    assert archive.searches[-1]["sort"] == "addeddate desc" and archive.searches[-1]["page"] == 1
    pk = s.get(ChannelPack, p.id)
    assert pk.last_refresh_at is not None and pk.last_refresh["checked"] >= 2


def test_refresh_leaves_human_approvals_alone(session):
    from archivetv.catalogue import set_review_status

    s = session
    p = install(s)
    it = found(s, p.rules[0], 7)  # no licence: a person approved it
    it.source.external_id = it.source.parent_external_id = "f7"
    set_review_status(s, it, ReviewStatus.APPROVED, actor="admin")
    it.playback_sources[0].rights_status = RightsStatus.AUTHORISED
    s.commit()
    set_provider("archive", FakeArchive({"f7": None}))
    assert asyncio.run(packs.refresh_pack(p.id, force=True))["back_to_review"] == 0
    s.expire_all()
    assert s.get(MediaItem, it.id).review_status == ReviewStatus.APPROVED


def test_refresh_brings_youtube_finds_on_air(session, monkeypatch):
    """A Creative Commons YouTube find waiting for review (e.g. found while
    streaming was off) gets its stream source on refresh and the pack airs it."""
    from archivetv import discovery

    s = session
    spec = {**SPEC, "rules": [{"provider": "youtube", "query": "live set", "params": {"creative_commons_only": True}}]}
    p = install(s, spec)
    it = MediaItem(source=MediaSource(provider="youtube", external_id="abcdefghijk", discovered_by_rule_id=p.rules[0].id),
                   title="Live set", duration=1800.0, review_status=ReviewStatus.PENDING_REVIEW,
                   restrictions={"youtube_license": "creativeCommon"})
    s.add(it)
    s.commit()
    yt = FakeYouTube()
    set_provider("youtube", yt)
    set_provider("archive", FakeArchive({}))

    async def fake_validate(ids, concurrency=3):
        with discovery.session_scope() as ss:
            for i in ids:
                for ps in ss.get(MediaItem, i).playback_sources:
                    ps.eligibility, ps.health_ok = Eligibility.IPTV_READY, True

    monkeypatch.setattr(discovery, "validate_items", fake_validate)
    monkeypatch.setattr(packs, "youtube_available", lambda: True)
    summary = asyncio.run(packs.refresh_pack(p.id, force=True))
    s.expire_all()
    assert yt.expanded == ["abcdefghijk"] and yt.searches[-1]["order"] == "date"
    assert summary["waiting_aired"] == 1
    assert s.get(MediaItem, it.id).review_status == ReviewStatus.APPROVED


def test_rotation_rests_the_most_aired_and_brings_them_back(session):
    from archivetv.scheduler import extend

    s = session
    p = install(s, {**SPEC, "target_hours": 1, "blocks": []})
    items = [aired(s, p, n) for n in range(1, 11)]  # 10 × 30 min
    ch = s.get(ChannelPack, p.id).channel
    ch.mode = "sequential"
    s.commit()
    s.refresh(ch)
    now = utcnow()
    extend(s, ch, now - timedelta(days=7), days=8)
    s.commit()
    favourite = items[0].id  # make one clearly the most aired
    for e in s.scalars(select(ScheduleEntry).where(ScheduleEntry.channel_id == ch.id, ScheduleEntry.kind == "programme",
                                                   ScheduleEntry.start < now)).all()[:40]:
        e.media_item_id = favourite
    s.commit()
    pk = s.get(ChannelPack, p.id)
    pk.rotate_percent = 20
    out = packs._rotate(s, pk, now)
    s.commit()
    assert out["rotated_out"] == 2 and out["rotated_back"] == 0
    rested = {int(k) for k in pk.state["rested"]}
    assert favourite in rested
    on_channel = set(s.scalars(select(ChannelItem.media_item_id).where(ChannelItem.channel_id == ch.id)).all())
    assert not on_channel & rested
    # Nothing rested is scheduled from the rebuild point on.
    future = s.scalars(select(ScheduleEntry).where(ScheduleEntry.channel_id == ch.id, ScheduleEntry.start > now + timedelta(minutes=5))).all()
    assert future and not {e.media_item_id for e in future} & rested
    assert s.scalar(select(AuditLog).where(AuditLog.action == "pack_install")) is not None
    # Four refreshes later they come back.
    later = now + timedelta(days=7 * packs.REST_CYCLES + 1)
    pk.rotate_percent = 0
    out = packs._rotate(s, pk, later)
    s.commit()
    assert out["rotated_back"] == 2 and not pk.state["rested"]


def test_rotation_keeps_half_the_target(session):
    s = session
    p = install(s, {**SPEC, "target_hours": 4, "blocks": []})
    items = [aired(s, p, n) for n in range(1, 5)]  # 2 hours, half the 4 h target
    pk = s.get(ChannelPack, p.id)
    pk.rotate_percent = 50
    now = utcnow()
    for i, it in enumerate(items):
        s.add(ScheduleEntry(channel_id=pk.channel_id, start=now - timedelta(hours=i + 1), end=now - timedelta(hours=i),
                            kind="programme", media_item_id=it.id, title=it.title))
    s.commit()
    assert packs._rotate(s, pk, now)["rotated_out"] == 0  # resting any would drop below 2 h


def test_api_refresh_and_settings(api, session, monkeypatch):
    r = api.post("/api/v1/packs/install", json={"spec": SPEC})
    pid = r.json()["id"]
    assert r.json()["refresh_days"] == 7 and r.json()["rotate_percent"] == 15 and r.json()["next_refresh_at"]
    r = api.patch(f"/api/v1/packs/{pid}", json={"refresh_days": 3, "rotate_percent": 25})
    assert (r.json()["refresh_days"], r.json()["rotate_percent"]) == (3, 25)
    assert api.patch(f"/api/v1/packs/{pid}", json={"rotate_percent": 90}).status_code == 422
    calls = []

    async def fake_refresh(pack_id, force=False):
        calls.append((pack_id, force))
        return {}

    monkeypatch.setattr(packs, "refresh_pack", fake_refresh)
    assert api.post(f"/api/v1/packs/{pid}/refresh").status_code == 202
    fills = []

    async def fake_fill(pack_id, force=False):
        fills.append(pack_id)
        return {}

    monkeypatch.setattr(packs, "fill_pack", fake_fill)
    from archivetv import api_packs

    monkeypatch.setattr(api_packs, "_start_fill", lambda pid: asyncio.get_running_loop().create_task(packs.fill_pack(pid)))
    assert api.post(f"/api/v1/packs/{pid}/fill").status_code == 202  # was a sync endpoint with no event loop
    assert api.get(f"/api/v1/packs/{pid}/export").json()["refresh_days"] == 3


# ------------------------------------------------------------------ lab mode


def test_lab_mode_airs_every_playable_find(session, monkeypatch):
    s = session
    monkeypatch.setattr(settings, "packs_air_everything", True)
    p = install(s)
    rule = p.rules[0]
    unlicensed = found(s, rule, 1)
    yt_standard = found(s, rule, 2, provider="youtube", yt_licence="youtube", ptype=PlaybackType.YOUTUBE_STREAM)
    restricted = found(s, rule, 3)
    restricted.restrictions = {"access_restricted": True}
    yt_embed_only = found(s, rule, 4, provider="youtube", yt_licence="youtube")  # streaming off: no stream source
    s.commit()
    ev = lambda it: packs.auto_air_evidence(it, packs._candidate_source(it), [])  # noqa: E731
    assert ev(unlicensed) == (True, packs.LAB_REASON) and ev(yt_standard)[0]
    assert not ev(restricted)[0] and not ev(yt_embed_only)[0]
    r = asyncio.run(packs.after_validation(rule.id, [unlicensed.id, yt_standard.id, restricted.id]))
    assert r == {"aired": 2, "inbox": 1}
    s.expire_all()
    assert s.get(MediaItem, yt_standard.id).review_status == ReviewStatus.APPROVED


def test_lab_mode_respects_review_first_packs(session, monkeypatch):
    s = session
    monkeypatch.setattr(settings, "packs_air_everything", True)
    p = install(s, {**SPEC, "policy": "inbox"})
    it = found(s, p.rules[0], 1)
    asyncio.run(packs.after_validation(p.rules[0].id, [it.id]))
    s.expire_all()
    assert s.get(MediaItem, it.id).review_status == ReviewStatus.PENDING_REVIEW


def test_lab_mode_weekly_check_keeps_unlicensed_tapes(session, monkeypatch):
    s = session
    monkeypatch.setattr(settings, "packs_air_everything", True)
    p = install(s)
    it = aired(s, p, 1, licence=None)
    assert it.review_status == ReviewStatus.APPROVED
    set_provider("archive", FakeArchive({"f1": None}))
    assert asyncio.run(packs.refresh_pack(p.id, force=True))["back_to_review"] == 0
    s.expire_all()
    assert s.get(MediaItem, it.id).review_status == ReviewStatus.APPROVED


def test_switching_lab_mode_on_airs_waiting_finds(api, session, monkeypatch):
    monkeypatch.setattr(settings, "packs_air_everything", False)
    from archivetv import appsettings

    monkeypatch.setattr(appsettings, "apply_overrides", lambda: None)  # keep the test's settings object in charge
    monkeypatch.setattr(appsettings, "save", lambda values: setattr(settings, "packs_air_everything",
                                                                    bool(values.get("packs_air_everything"))) or [])
    calls = []

    async def fake_reapply_all():
        calls.append(True)
        return 0

    monkeypatch.setattr(packs, "reapply_all", fake_reapply_all)
    assert api.patch("/api/v1/settings", json={"packs_air_everything": True}).status_code == 200
    assert calls == [True]
    assert api.get("/api/v1/packs").json()["lab_mode"] is True
    calls.clear()
    api.patch("/api/v1/settings", json={"packs_air_everything": True})
    assert calls == []  # only when it is switched on


def test_reapply_all_airs_what_was_held_back(session, monkeypatch):
    s = session
    p = install(s)
    it = found(s, p.rules[0], 1)  # no licence: held for review
    asyncio.run(packs.after_validation(p.rules[0].id, [it.id]))
    monkeypatch.setattr(settings, "packs_air_everything", True)
    assert asyncio.run(packs.reapply_all()) == 1
    s.expire_all()
    assert s.get(MediaItem, it.id).review_status == ReviewStatus.APPROVED


# ------------------------------------------------- mixing Archive and YouTube


def test_starter_packs_mix_archive_and_youtube():
    mixed = [p for p in packs.starter_packs() if {r.provider for r in p.rules} == {"archive", "youtube"}]
    assert len(mixed) >= 9


def test_youtube_rules_are_skipped_without_a_key(session, monkeypatch):
    import archivetv.discovery as disc

    s = session
    spec = {**SPEC, "rules": SPEC["rules"] + [{"provider": "youtube", "query": "monster movie full film"}]}
    p = install(s, spec)
    ran = []

    async def fake_run(rule_id, interactive=False):
        ran.append(rule_id)
        return DiscoveryResult(found=1, new=1)

    monkeypatch.setattr(disc, "run_rule", fake_run)
    monkeypatch.setattr(packs, "youtube_available", lambda: False)
    asyncio.run(packs.fill_pack(p.id, force=True))
    s.expire_all()
    archive_rule, yt_rule = (s.get(DiscoveryRule, r.id) for r in p.rules)
    assert ran == [archive_rule.id]
    assert yt_rule.last_status == "skipped" and "API key" in yt_rule.last_error
    monkeypatch.setattr(packs, "youtube_available", lambda: True)
    ran.clear()
    asyncio.run(packs.fill_pack(p.id, force=True))
    assert ran == [archive_rule.id, yt_rule.id]


def test_both_providers_share_one_channel_pool(session):
    """Archive and YouTube finds of one pack land on the same channel and the
    shuffle draws from both."""
    from archivetv.scheduler import extend

    s = session
    spec = {**SPEC, "rules": SPEC["rules"] + [{"provider": "youtube", "query": "x"}], "blocks": [], "breaks": False}
    p = install(s, spec)
    archive_rule, yt_rule = p.rules
    for n in range(4):
        it = found(s, archive_rule, n, licence="http://creativecommons.org/publicdomain/mark/1.0/")
        asyncio.run(packs.after_validation(archive_rule.id, [it.id]))
    for n in range(4, 8):
        it = found(s, yt_rule, n, provider="youtube", yt_licence="creativeCommon", ptype=PlaybackType.YOUTUBE_STREAM)
        asyncio.run(packs.after_validation(yt_rule.id, [it.id]))
    s.expire_all()
    ch = s.get(ChannelPack, p.id).channel
    extend(s, ch, utcnow(), days=1)
    providers = {e.media_item.source.provider for e in s.scalars(select(ScheduleEntry).where(ScheduleEntry.channel_id == ch.id)).all()}
    assert providers == {"archive", "youtube"}


def test_upgrade_keeps_admin_choices_and_waiting_finds(session):
    s = session
    p = install(s)
    waiting = found(s, p.rules[0], 1)
    pk = s.get(ChannelPack, p.id)
    pk.policy, pk.target_hours, pk.rotate_percent = "inbox", 42, 5
    s.commit()
    up = install(s, {**SPEC, "version": 2, "rules": SPEC["rules"] + [{"provider": "youtube", "query": "x"}]}, upgrade=True)
    assert (up.policy, up.target_hours, up.rotate_percent, up.version) == ("inbox", 42, 5, 2)
    s.expire_all()
    src = s.get(MediaItem, waiting.id).source
    assert src.discovered_by_rule_id == next(r.id for r in s.get(ChannelPack, p.id).rules if r.provider == "archive")
