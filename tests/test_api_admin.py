"""Admin REST API, driven the way the React frontend uses it (session login)."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from archivetv.config import settings
from archivetv.db import utcnow
from archivetv.enums import Eligibility, PlaybackType, ReviewStatus
from archivetv.main import app
from archivetv.models import MediaItem, MediaSource, PlaybackSource


@pytest.fixture()
def api(session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "admin_password", "pw")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    for k in ("transcode_height", "transcode_preset", "public_base_url", "cache_enabled"):
        monkeypatch.setattr(settings, k, getattr(settings, k))  # restored after the settings test
    c = TestClient(app)
    from archivetv.web import admin

    admin._attempts.clear()
    return c


def _pending(s, n, provider="archive", url=None, ptype=PlaybackType.DIRECT_URL, elig=Eligibility.IPTV_READY):
    it = MediaItem(source=MediaSource(provider=provider, external_id=f"x{n}"), title=f"Film {n}", title_key=f"film {n}",
                   duration=900.0, review_status=ReviewStatus.PENDING_REVIEW)
    it.playback_sources.append(PlaybackSource(playback_type=ptype, url=url or f"https://archive.org/download/x{n}/x{n}.mp4",
                                              eligibility=elig, duration=900.0, health_ok=True, seekable=True))
    s.add(it)
    s.commit()
    return it


def test_login_flow(api):
    assert api.get("/api/v1/auth/me").status_code == 401
    assert api.get("/api/v1/dashboard").status_code == 401
    assert api.post("/api/v1/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401
    assert api.post("/api/v1/auth/login", json={"username": "admin", "password": "pw"}).json() == {"user": "admin"}
    assert api.get("/api/v1/auth/me").json()["user"] == "admin"
    assert api.get("/api/v1/meta").json()["timezone"] == "Europe/London"
    api.post("/api/v1/auth/logout")
    assert api.get("/api/v1/auth/me").status_code == 401
    for _ in range(5):
        api.post("/api/v1/auth/login", json={"username": "admin", "password": "bad"})
    assert api.post("/api/v1/auth/login", json={"username": "admin", "password": "pw"}).status_code == 429


def test_review_to_schedule_via_api(api, session):
    s = session
    a, b = _pending(s, 1), _pending(s, 2)
    yt = _pending(s, 3, provider="youtube", url="https://www.youtube.com/embed/abc", ptype=PlaybackType.YOUTUBE_EMBED,
                  elig=Eligibility.EMBED_ONLY)
    api.post("/api/v1/auth/login", json={"username": "admin", "password": "pw"})
    ch = api.post("/api/v1/channels", json={"slug": "films", "name": "Films"}).json()
    q = api.get("/api/v1/catalogue", params={"status": "PENDING_REVIEW"}).json()
    assert q["total"] == 3

    r = api.post("/api/v1/media/bulk", json={"ids": [a.id, b.id, yt.id], "action": "approve", "channel_id": ch["id"]}).json()
    assert r["count"] == 3
    # Approval alone is not enough to air: nothing is schedulable yet.
    assert all(not i["schedulable"] for i in api.get("/api/v1/catalogue", params={"status": "APPROVED"}).json()["items"])
    r = api.post("/api/v1/media/bulk", json={"ids": [a.id, b.id, yt.id], "action": "authorise"}).json()
    assert r["skipped_no_iptv_source"] == [yt.id]  # YouTube embeds can never be authorised
    yt_src = api.get(f"/api/v1/media/{yt.id}").json()["playback_sources"][0]["id"]
    assert api.post(f"/api/v1/sources/{yt_src}/rights", json={"rights": "AUTHORISED"}).status_code == 400

    detail = api.get(f"/api/v1/channels/{ch['id']}").json()
    assert {i["media_item_id"] for i in detail["items"] if i["schedulable"]} == {a.id, b.id}
    api.post(f"/api/v1/channels/{ch['id']}/schedule/generate")
    day = api.get(f"/api/v1/channels/{ch['id']}/schedule/day").json()
    assert day["entries"] and not day["problems"] and {e["media_item_id"] for e in day["entries"]} <= {a.id, b.id}

    later = [e for e in day["entries"] if e["start"] > (utcnow() + timedelta(hours=1)).isoformat()]
    if later:
        ov = api.post(f"/api/v1/schedule/entries/{later[0]['id']}/override", json={"item_id": a.id})
        assert ov.status_code == 200 and ov.json()["overridden"]
    versions = api.get(f"/api/v1/channels/{ch['id']}/schedule/versions").json()
    assert versions and versions[0]["actor"] == "admin"

    m = api.patch(f"/api/v1/media/{a.id}", json={"title": "Renamed film", "season": 1, "episode": 2}).json()
    assert m["title"] == "Renamed film" and m["episode"] == 2
    hist = api.get(f"/api/v1/media/{a.id}/history").json()["history"]
    assert {h["action"] for h in hist} >= {"edit_metadata"}


def test_channel_editing_blocks_logo_settings(api, session):
    api.post("/api/v1/auth/login", json={"username": "admin", "password": "pw"})
    ch = api.post("/api/v1/channels", json={"slug": "kids", "name": "Kids"}).json()
    cid = ch["id"]
    d = api.post(f"/api/v1/channels/{cid}/blocks", json={"name": "Morning", "start_time": "07:00", "end_time": "09:00",
                                                         "days": "56", "mode": "sequential"}).json()
    assert d["blocks"][0]["days"] == "56"
    assert api.post(f"/api/v1/channels/{cid}/blocks", json={"name": "x", "start_time": "7am", "end_time": "9"}).status_code == 422
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
    d = api.post(f"/api/v1/channels/{cid}/logo", files={"file": ("logo.png", png, "image/png")}).json()
    assert d["logo"].endswith(".png") and "/logos/kids-" in d["logo"]
    assert api.get(d["logo"].replace("http://testserver", "")).content == png
    assert api.post(f"/api/v1/channels/{cid}/logo", files={"file": ("x.svg", b"<svg/>", "image/svg+xml")}).status_code == 400
    p = api.patch(f"/api/v1/channels/{cid}", json={"idents_between": True, "catchup_days": 14}).json()
    assert p["catchup_days"] == 14
    st = api.get("/api/v1/settings").json()
    assert st["xtream"]["username"] == settings.xc_username and "password" not in st["xtream"]
    assert api.patch("/api/v1/settings", json={"transcode_height": 1080}).json()["ok"]
    assert settings.transcode_height == 1080
    assert api.patch("/api/v1/settings", json={"youtube_api_key": "x"}).status_code == 400  # secrets are not tunables
    assert api.delete(f"/api/v1/channels/{cid}", params={"confirm": "wrong"}).status_code == 400
    assert api.delete(f"/api/v1/channels/{cid}", params={"confirm": "kids"}).json() == {"deleted": cid}
    assert "resources" in api.get("/api/v1/dashboard").json()
