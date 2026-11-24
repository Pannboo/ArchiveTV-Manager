"""AI helpers (local OpenAI-compatible backend) and curated YouTube channels."""

import asyncio
import json
import xml.etree.ElementTree as ET
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from archivetv import ai, packs, ytfollow
from archivetv.catalogue import upsert_record
from archivetv.config import settings
from archivetv.db import utcnow
from archivetv.discovery import DiscoveryResult
from archivetv.enums import Eligibility, PlaybackType, ReviewStatus, RuleKind
from archivetv.models import DiscoveryRule, FollowedChannel, MediaItem, MediaSource, PlaybackSource
from archivetv.providers import MediaRecord
from tests.test_packs import SPEC, found, install


@pytest.fixture()
def local_ai(monkeypatch):
    """A fake OpenAI-compatible server. Set ``replies`` to what it answers."""
    monkeypatch.setattr(settings, "ai_backend", "local")
    monkeypatch.setattr(settings, "ai_base_url", "http://ai-box:11434/v1")
    monkeypatch.setattr(settings, "ai_model", "test-model")
    monkeypatch.setattr(ai, "_local_sem", None)
    state = {"requests": [], "replies": []}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        state["requests"].append({"url": str(request.url), "body": body})
        reply = state["replies"].pop(0) if state["replies"] else {"fits": True, "reason": "ok"}
        if isinstance(reply, httpx.Response):
            return reply
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(ai.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return state


ENRICHED = {"title": "Plan 9 from Outer Space", "year": "1959", "series": "", "season": 0, "episode": 0,
            "tags": ["Science Fiction", "horror", "b-movie", "<script>"], "kind": "full programme"}


# ---------------------------------------------------------------- backend


def test_backend_selection(monkeypatch):
    monkeypatch.setattr(settings, "ai_backend", "none")
    assert not ai.available()
    monkeypatch.setattr(settings, "ai_backend", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    assert ai.backend() == "none"  # no key, no Claude
    monkeypatch.setattr(settings, "ai_backend", "local")
    monkeypatch.setattr(settings, "ai_base_url", "http://ai-box:11434/v1")
    assert ai.backend() == "local" and ai.describe()["base_url"] == "http://ai-box:11434/v1"


def test_local_request_asks_for_schema_json(local_ai):
    local_ai["replies"] = [{"fits": False, "reason": "a cooking show"}]
    out = asyncio.run(ai.theme_check("Monster Movies", "monster films", [(1, {"title": "Pasta night"})]))
    assert out == {1: (False, "a cooking show")}
    req = local_ai["requests"][0]
    assert req["url"] == "http://ai-box:11434/v1/chat/completions"
    assert req["body"]["model"] == "test-model" and req["body"]["temperature"] == 0
    assert req["body"]["response_format"]["type"] == "json_schema"
    assert "Pasta night" in req["body"]["messages"][1]["content"]


def test_local_fenced_json_and_json_mode_fallback(local_ai):
    local_ai["replies"] = [httpx.Response(400, text="unsupported response_format json_schema"),
                           '```json\n{"fits": true, "reason": "fine"}\n```']
    out = asyncio.run(ai.json_task("s", "u", ai.VERDICT_SCHEMA))
    assert out == {"fits": True, "reason": "fine"}
    assert local_ai["requests"][1]["body"]["response_format"] == {"type": "json_object"}


def test_local_failures_never_pass_a_theme_check(local_ai):
    local_ai["replies"] = [httpx.Response(500, text="model crashed")]
    out = asyncio.run(ai.theme_check("X", "t", [(1, {"title": "t"})]))
    assert out[1][0] is False and "unavailable" in out[1][1]
    local_ai["replies"] = ["not json at all"]
    out = asyncio.run(ai.theme_check("X", "t", [(2, {"title": "t"})]))
    assert out[2][0] is False


def test_clean_enrichment_bounds_the_answer():
    m = ai.clean_enrichment({**ENRICHED, "year": "3021", "season": -2, "episode": 5000, "kind": "nonsense"}, "src")
    assert m["year"] == "" and m["season"] == 0 and m["episode"] == 0 and m["kind"] == "other"
    assert m["tags"] == ["science fiction", "horror", "b-movie"]  # lower-cased; markup dropped
    assert ai.clean_enrichment({**ENRICHED, "title": "   "}, "Original")["title"] == "Original"


# ------------------------------------------------------------- enrichment


def _yt_item(s, vid="abcdefghijk", title="Plan 9 From Outer Space (1959) [HD] FULL MOVIE", creator_id="UC" + "x" * 22):
    it = MediaItem(source=MediaSource(provider="youtube", external_id=vid, creator="Retro Films", creator_id=creator_id),
                   title=title, duration=4740.0, review_status=ReviewStatus.PENDING_REVIEW, tags=["movie"])
    it.playback_sources.append(PlaybackSource(playback_type=PlaybackType.YOUTUBE_STREAM, url=f"https://www.youtube.com/watch?v={vid}",
                                              eligibility=Eligibility.IPTV_READY, health_ok=True))
    s.add(it)
    s.commit()
    return it


def test_enrichment_cleans_tags_and_spots_cross_source_duplicates(session, local_ai):
    s = session
    archive = MediaItem(source=MediaSource(provider="archive", external_id="Plan9/plan9"), title="Plan 9 from Outer Space",
                        title_key="plan 9 from outer space", duration=4700.0, review_status=ReviewStatus.APPROVED)
    s.add(archive)
    s.commit()
    yt = _yt_item(s)
    local_ai["replies"] = [ENRICHED]
    assert asyncio.run(ai.enrich_items([yt.id])) == 1
    s.expire_all()
    yt = s.get(MediaItem, yt.id)
    assert yt.title == "Plan 9 from Outer Space" and yt.publication_date == "1959"
    assert yt.tags == ["movie", "science fiction", "horror", "b-movie"]
    assert yt.ai_meta["source_title"].startswith("Plan 9 From Outer Space (1959)")
    assert yt.duplicate_of_id == archive.id  # same film on both sources
    # Already done for this title: no second request.
    assert asyncio.run(ai.enrich_items([yt.id])) == 0 and len(local_ai["requests"]) == 1


def test_provider_refresh_keeps_the_clean_up_until_the_title_changes(session, local_ai):
    s = session
    yt = _yt_item(s)
    local_ai["replies"] = [ENRICHED]
    asyncio.run(ai.enrich_items([yt.id]))
    s.expire_all()  # the clean-up was saved by another session
    rec = MediaRecord(provider="youtube", external_id="abcdefghijk", title="Plan 9 From Outer Space (1959) [HD] FULL MOVIE",
                      tags=["movie"])
    upsert_record(s, rec)
    s.commit()
    it = s.get(MediaItem, yt.id)
    assert it.title == "Plan 9 from Outer Space" and "horror" in it.tags
    upsert_record(s, MediaRecord(provider="youtube", external_id="abcdefghijk", title="Plan 9 (re-upload)", tags=[]))
    s.commit()
    assert s.get(MediaItem, yt.id).title == "Plan 9 (re-upload)"  # the uploader renamed it: AI result no longer applies


def test_ai_holds_back_clips_and_trailers(session, monkeypatch):
    s = session
    monkeypatch.setattr(settings, "packs_air_everything", True)
    p = install(s)
    it = found(s, p.rules[0], 1)
    it.ai_meta = {"kind": "trailer or promo"}
    s.commit()
    r = asyncio.run(packs.after_validation(p.rules[0].id, [it.id]))
    s.expire_all()
    assert r["aired"] == 0 and s.get(MediaItem, it.id).review_status == ReviewStatus.PENDING_REVIEW
    assert any("trailer" in f for f in s.get(MediaItem, it.id).flags)


# --------------------------------------------------- curated YouTube channels

YT_SPEC = {**SPEC, "rules": [{"provider": "youtube", "query": "monster movie full film",
                              "params": {"min_duration": 3000, "excluded_keywords": "trailer", "order": "viewCount"}}]}


def test_search_finds_become_suggestions_and_get_vetted(session, local_ai):
    s = session
    p = install(s, YT_SPEC)
    rule = p.rules[0]
    a = _yt_item(s, "aaaaaaaaaaa", "Them! (1954)", creator_id="UC" + "a" * 22)
    b = _yt_item(s, "bbbbbbbbbbb", "Tarantula (1955)", creator_id="UC" + "a" * 22)
    c = _yt_item(s, "ccccccccccc", "Reaction: Them!", creator_id="UC" + "c" * 22)
    for it in (a, b, c):
        it.source.discovered_by_rule_id = rule.id
    s.commit()
    local_ai["replies"] = [{"fits": True, "reason": "uploads 1950s creature features"}, {"fits": False, "reason": "reactions"}]
    new = asyncio.run(ytfollow.note_search_finds(rule.id, [a.id, b.id, c.id]))
    assert len(new) == 2
    s.expire_all()
    good = s.scalar(select(FollowedChannel).where(FollowedChannel.channel_id == "UC" + "a" * 22))
    assert good.status == "suggested" and good.hits == 2 and good.samples == ["Them! (1954)", "Tarantula (1955)"]
    assert good.ai_verdict["fits"] is True and good.title == "Retro Films"
    # Seen again later: hits add up, no second suggestion, no second vetting.
    asyncio.run(ytfollow.note_search_finds(rule.id, [a.id]))
    s.expire_all()
    assert s.get(FollowedChannel, good.id).hits == 3 and len(local_ai["requests"]) == 2


def test_follow_unfollow_and_dismiss(session):
    s = session
    p = install(s, YT_SPEC)
    fc = FollowedChannel(pack_id=p.id, channel_id="UC" + "a" * 22, title="Retro Films", status="suggested", hits=2,
                         samples=[], ai_verdict={})
    s.add(fc)
    s.commit()
    rule = ytfollow.follow(s, fc)
    s.commit()
    assert rule.kind == RuleKind.CHANNEL and rule.query == "UC" + "a" * 22 and rule.pack_id == p.id
    assert rule.params["min_duration"] == 3000 and rule.params["excluded_keywords"] == "trailer"
    assert "order" not in rule.params  # search-only settings aren't carried
    assert rule.channels == [p.channel] and rule.max_results == ytfollow.DEEP_HARVEST
    assert fc.status == "followed" and ytfollow.followed_count(s, p.id) == 1
    ytfollow.unfollow(s, fc, dismiss=True)
    s.commit()
    assert fc.status == "dismissed" and fc.rule_id is None and s.get(DiscoveryRule, rule.id) is None


def test_search_budget_is_shared_and_daily(session, monkeypatch):
    monkeypatch.setattr(settings, "youtube_search_budget", 2)
    assert ytfollow.searches_left() == 2
    assert ytfollow.take_search() and ytfollow.take_search()
    assert not ytfollow.take_search() and ytfollow.searches_left() == 0
    monkeypatch.setattr(ytfollow, "_today", lambda: "2099-01-01")  # midnight Pacific: a new day
    assert ytfollow.searches_left() == 2 and ytfollow.take_search()


def test_searches_back_off_once_curated(session, monkeypatch):
    s = session
    monkeypatch.setattr(packs, "youtube_available", lambda: True)
    monkeypatch.setattr(settings, "youtube_search_budget", 10)
    p = install(s, YT_SPEC)
    search = p.rules[0]
    now = utcnow()
    run, held = packs._plan_rules(s, p, now)
    assert run == [search.id] and ytfollow.searches_used() == 1
    search.last_run_at = now - timedelta(hours=13)
    for n in range(ytfollow.CURATED_ENOUGH):
        fc = FollowedChannel(pack_id=p.id, channel_id=f"UC{n:022d}", status="suggested", hits=1, samples=[], ai_verdict={})
        s.add(fc)
        s.flush()
        ytfollow.follow(s, fc)
    s.commit()
    run, held = packs._plan_rules(s, p, now)
    held_reasons = dict(held)
    assert search.id not in run and "weekly" in held_reasons[search.id]  # enough curated channels
    channel_rules = [r.id for r in p.rules if r.kind == RuleKind.CHANNEL]
    assert set(channel_rules) <= set(run)  # new follows: deep harvest now
    for r in p.rules:
        if r.kind == RuleKind.CHANNEL:
            r.last_run_at = now - timedelta(days=1)
    run, held = packs._plan_rules(s, p, now)
    assert not set(channel_rules) & set(run)  # harvested: RSS until next week
    assert all("RSS" in dict(held)[r] for r in channel_rules)


def test_rss_picks_up_only_new_uploads(session, monkeypatch):
    s = session
    p = install(s, YT_SPEC)
    fc = FollowedChannel(pack_id=p.id, channel_id="UC" + "a" * 22, status="suggested", hits=1, samples=[], ai_verdict={})
    s.add(fc)
    s.flush()
    rule = ytfollow.follow(s, fc)
    s.commit()
    _yt_item(s, "known123456", "Already have it")
    feed = (
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
        "<title>Retro Films</title>"
        "<entry><yt:videoId>known123456</yt:videoId></entry>"
        "<entry><yt:videoId>fresh123456</yt:videoId></entry></feed>"
    )

    async def fake_rss(cid):
        return ET.fromstring(feed)

    calls = []

    async def fake_run_ids(provider, ids, params=None, rule_id=None):
        calls.append((provider, ids, rule_id, params))
        return DiscoveryResult(new=len(ids))

    import archivetv.discovery as disc

    monkeypatch.setattr(ytfollow, "_rss", fake_rss)
    monkeypatch.setattr(disc, "run_ids", fake_run_ids)
    assert asyncio.run(ytfollow.check_feeds(p.id)) == 1
    assert calls == [("youtube", ["fresh123456"], rule.id, rule.params)]
    assert asyncio.run(ytfollow.check_feeds(p.id)) == 0  # checked within the hour: skipped
    s.expire_all()
    assert s.get(FollowedChannel, fc.id).last_rss_at is not None


# ------------------------------------------------------------------- API


def test_api_channels_and_ai(api, session, monkeypatch, local_ai):
    pid = api.post("/api/v1/packs/install", json={"spec": YT_SPEC}).json()["id"]

    async def fake_resolve(ref):
        return "UC" + "z" * 22, "Creature Features Channel"

    monkeypatch.setattr(ytfollow, "resolve", fake_resolve)
    r = api.post(f"/api/v1/packs/{pid}/channels", json={"ref": "@creaturefeatures"})
    assert r.status_code == 201
    ch = r.json()["channels"][0]
    assert ch["status"] == "followed" and ch["added_by"] == "admin" and ch["title"] == "Creature Features Channel"
    r = api.post(f"/api/v1/packs/{pid}/channels/{ch['id']}/unfollow")
    assert r.json()["channels"][0]["status"] == "suggested"
    r = api.post(f"/api/v1/packs/{pid}/channels/{ch['id']}/vet")
    assert r.json()["channels"][0]["ai_verdict"]["fits"] is True
    assert api.post(f"/api/v1/packs/{pid}/channels/{ch['id']}/explode").status_code == 422
    assert api.get("/api/v1/packs").json()["installed"][0]["youtube_channels"] == {"suggested": 1}
    assert api.post("/api/v1/ai/test").json()["ok"] is True
    status = api.get("/api/v1/ai").json()
    assert status["backend"] == "local" and status["model"] == "test-model"
    assert api.post("/api/v1/ai/enrich", json={"limit": 10}).status_code == 202


@pytest.fixture()
def api(session, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from archivetv import api_packs
    from archivetv.main import app
    from archivetv.web import admin

    monkeypatch.setattr(settings, "admin_password", "pw")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    admin._attempts.clear()
    c = TestClient(app)
    c.post("/api/v1/auth/login", json={"username": "admin", "password": "pw"})
    monkeypatch.setattr(api_packs, "_start_fill", lambda pid: None)
    return c
