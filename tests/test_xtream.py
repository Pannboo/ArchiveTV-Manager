"""Xtream Codes upstream interface, exercised with the requests Dispatcharr
0.33 makes (core/xtream_codes.py, apps/timeshift/helpers.py)."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from archivetv.config import settings
from archivetv.db import utcnow
from archivetv.main import app
from archivetv.models import Channel
from archivetv.xtream import category_id

from .test_catchup import _channel_with_history

U, P = "archivetv", "xc-test-pass"


@pytest.fixture()
def client(session, monkeypatch):
    monkeypatch.setattr(settings, "xc_username", U)
    monkeypatch.setattr(settings, "xc_password", P)
    monkeypatch.setattr(settings, "api_token", "api-test-token")
    return TestClient(app)


def test_account_ingestion(client, session):
    ch, _ = _channel_with_history(session, minutes_ago=10)
    r = client.get("/player_api.php", params={"username": U, "password": P})
    body = r.json()
    assert r.status_code == 200 and body["user_info"]["auth"] == 1
    # UTC means Dispatcharr passes catch-up timestamps through unconverted.
    assert body["server_info"]["timezone"] == "UTC"

    cats = client.get("/player_api.php", params={"username": U, "password": P, "action": "get_live_categories"}).json()
    assert cats == [{"category_id": category_id("ArchiveTV"), "category_name": "ArchiveTV", "parent_id": 0}]
    streams = client.get("/player_api.php", params={"username": U, "password": P, "action": "get_live_streams"}).json()
    (st,) = streams
    assert st["stream_id"] == ch.id and st["epg_channel_id"] == "hist" and st["category_id"] == cats[0]["category_id"]
    assert st["tv_archive"] == 1 and st["tv_archive_duration"] == 7
    assert client.get("/player_api.php", params={"username": U, "password": P, "action": "get_vod_streams"}).json() == []

    ch.catchup_enabled = False
    session.commit()
    st = client.get("/player_api.php", params={"username": U, "password": P, "action": "get_live_streams"}).json()[0]
    assert st["tv_archive"] == 0 and st["tv_archive_duration"] == 0

    bad = client.get("/player_api.php", params={"username": U, "password": "wrong"})
    assert bad.status_code == 401 and bad.json()["user_info"]["auth"] == 0
    assert client.get("/xmltv.php", params={"username": U, "password": P}).text.count("<programme") > 3


def test_timeshift_error_mapping(client, session):
    ch, _ = _channel_with_history(session, minutes_ago=10)
    old = (utcnow() - timedelta(days=9)).strftime("%Y-%m-%d:%H-%M")
    # Outside the window: 404 so Dispatcharr reports "not available".
    assert client.get(f"/timeshift/{U}/{P}/45/{old}/{ch.id}.ts").status_code == 404
    assert client.get(f"/timeshift/{U}/{P}/45/{old}/999.ts").status_code == 404
    assert client.get(f"/timeshift/{U}/wrong/45/{old}/{ch.id}.ts").status_code == 401
    ch.catchup_enabled = False
    session.commit()
    recent = (utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%d:%H-%M")
    assert client.get(f"/timeshift/{U}/{P}/45/{recent}/{ch.id}.ts").status_code == 403
    q = client.get("/streaming/timeshift.php", params={"username": U, "password": P, "stream": ch.id, "start": recent, "duration": 45})
    assert q.status_code == 403


def test_api_requires_auth_and_lists_catchup(client, session):
    _channel_with_history(session, minutes_ago=10)
    assert client.get("/api/v1/channels").status_code == 401
    h = {"Authorization": "Bearer api-test-token"}
    chans = client.get("/api/v1/channels", headers=h).json()
    assert chans[0]["catchup_days"] == 7 and chans[0]["catchup_enabled"]
    progs = client.get("/api/v1/catchup/programmes", headers=h).json()
    # Tiny 4s test programmes: those that began in the last 30s count as live.
    edge = (utcnow() - timedelta(seconds=35)).isoformat()
    old = [p for p in progs if p["start"][:-1] < edge]
    assert old and all(p["available"] for p in old)
    assert all(p["available"] or "live" in p["reason"] for p in progs)
    start = progs[-1]["start"]
    made = client.post("/api/v1/catchup/sessions", headers=h, json={"channel_id": chans[0]["id"], "start": start})
    assert made.status_code == 201 and "/catchup/hist.ts?start=" in made.json()["url"]
    r = client.patch(f"/api/v1/channels/{chans[0]['id']}", headers=h, json={"catchup_days": 3})
    assert r.json()["catchup_days"] == 3
    assert client.patch(f"/api/v1/channels/{chans[0]['id']}", headers=h, json={"catchup_days": 5}).status_code == 422
    assert client.get("/api/v1/openapi.json", headers=h).json()["info"]["title"] == "ArchiveTV Manager"
    assert client.get("/api/v1/system/status", headers=h).json()["resources"]["minimal_disk"] is True
    assert session.get(Channel, chans[0]["id"]) is not None
