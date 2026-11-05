from datetime import datetime, timedelta

from sqlalchemy import select

from archivetv.catalogue import assign_to_channel, is_schedulable, iptv_status, set_review_status, upsert_record
from archivetv.enums import ChannelRole, Eligibility, EntryKind, PlaybackType, ReviewStatus, RightsStatus, ScheduleMode
from archivetv.feeds import build_m3u, build_xmltv
from archivetv.models import Channel, ChannelBlock, MediaItem, MediaSource, PlaybackSource, ScheduleEntry
from archivetv.providers.youtube import video_to_record
from archivetv.scheduler import active_block, check_continuity, extend, maintain, override, regenerate

NOW = datetime(2026, 11, 4, 12, 17, 0)


def make_item(s, n, dur=1800.0, approved=True, authorised=True, eligible=Eligibility.IPTV_READY, provider="archive", tags=None):
    item = MediaItem(
        source=MediaSource(provider=provider, external_id=f"item{n}"),
        title=f"Programme {n}",
        duration=dur,
        tags=tags or [],
        review_status=ReviewStatus.APPROVED if approved else ReviewStatus.PENDING_REVIEW,
    )
    item.playback_sources.append(
        PlaybackSource(
            playback_type=PlaybackType.DIRECT_URL,
            url=f"https://archive.org/download/item{n}/item{n}.mp4",
            eligibility=eligible,
            rights_status=RightsStatus.AUTHORISED if authorised else RightsStatus.UNREVIEWED,
            duration=dur,
        )
    )
    s.add(item)
    s.flush()
    return item


def make_channel(s, mode=ScheduleMode.SEQUENTIAL, slug="retro"):
    ch = Channel(slug=slug, tvg_id=f"{slug}.archivetv", name=slug.title(), mode=mode, seed="seed", repeat_avoid_count=2, number=1)
    s.add(ch)
    s.flush()
    return ch


def entries(s, ch):
    return list(s.scalars(select(ScheduleEntry).where(ScheduleEntry.channel_id == ch.id).order_by(ScheduleEntry.start)).all())


def test_sequential_gap_free_seven_days(session):
    s = session
    ch = make_channel(s)
    items = [make_item(s, i, dur=1500 + i * 60) for i in range(3)]
    for it in items:
        assign_to_channel(s, it, ch.id)
    s.flush()
    s.refresh(ch)
    n = extend(s, ch, NOW)
    es = entries(s, ch)
    assert n == len(es) > 0
    assert es[0].start == datetime(2026, 11, 4, 12, 0)
    assert es[-1].end >= NOW + timedelta(days=7)
    assert check_continuity(es) == []
    assert [e.media_item_id for e in es[:6]] == [items[0].id, items[1].id, items[2].id] * 2


def test_unapproved_or_unauthorised_never_scheduled(session):
    s = session
    ch = make_channel(s)
    ok = make_item(s, 1)
    unapproved = make_item(s, 2, approved=False)
    unauthorised = make_item(s, 3, authorised=False)
    embed_only = make_item(s, 4, eligible=Eligibility.EMBED_ONLY)
    for it in (ok, unapproved, unauthorised, embed_only):
        assign_to_channel(s, it, ch.id)
    s.flush()
    s.refresh(ch)
    extend(s, ch, NOW, days=1)
    ids = {e.media_item_id for e in entries(s, ch)}
    assert ids == {ok.id}
    assert iptv_status(unauthorised) == "rights not authorised"
    assert iptv_status(unapproved) == "not approved"


def test_youtube_approved_without_authorised_source_excluded(session):
    s = session
    ch = make_channel(s)
    rec = video_to_record({"id": "dQw4w9WgXcQ", "snippet": {"title": "YT"}, "contentDetails": {"duration": "PT20M"},
                           "status": {"privacyStatus": "public", "embeddable": True}})
    yt, _ = upsert_record(s, rec)
    set_review_status(s, yt, ReviewStatus.APPROVED)
    assign_to_channel(s, yt, ch.id)
    s.flush()
    s.refresh(ch)
    assert not is_schedulable(yt)
    assert "YouTube is discovery only" in iptv_status(yt)
    extend(s, ch, NOW, days=1)
    es = entries(s, ch)
    assert all(e.kind == EntryKind.SLATE for e in es) and check_continuity(es) == []
    # Attaching an independently authorised file makes it eligible; the next
    # maintenance pass replaces the off-air slates.
    yt.playback_sources.append(PlaybackSource(playback_type=PlaybackType.DIRECT_URL, url="https://archive.org/download/x/x.mp4",
                                              eligibility=Eligibility.IPTV_READY, rights_status=RightsStatus.AUTHORISED, duration=1200))
    s.flush()
    assert is_schedulable(yt)
    maintain(s, ch, NOW)
    es = entries(s, ch)
    assert any(e.media_item_id == yt.id for e in es)
    assert check_continuity(es) == []


def test_schedule_is_stable_across_restart(session):
    s = session
    ch = make_channel(s, mode=ScheduleMode.SHUFFLE)
    for i in range(6):
        assign_to_channel(s, make_item(s, i, dur=600 + i * 37), ch.id)
    s.flush()
    s.refresh(ch)
    extend(s, ch, NOW, days=2)
    before = [(e.start, e.media_item_id) for e in entries(s, ch)]
    # "Restart": maintenance a few hours later must not alter published entries.
    maintain(s, ch, NOW + timedelta(hours=3))
    after = [(e.start, e.media_item_id) for e in entries(s, ch)]
    assert after[: len(before)] == before
    # Determinism: rebuilding from the same state reproduces the same order.
    ids = [m for _, m in before[:20]]
    regenerate(s, ch, NOW - timedelta(seconds=1))
    assert [e.media_item_id for e in entries(s, ch)][1:20] == ids[1:20]


def test_shuffle_repeat_avoidance(session):
    s = session
    ch = make_channel(s, mode=ScheduleMode.SHUFFLE)
    ch.repeat_avoid_count = 3
    for i in range(5):
        assign_to_channel(s, make_item(s, i, dur=300), ch.id)
    s.flush()
    s.refresh(ch)
    extend(s, ch, NOW, days=1)
    seq = [e.media_item_id for e in entries(s, ch)]
    for i in range(3, len(seq)):
        assert seq[i] not in seq[i - 3 : i]


def test_ineligible_future_entries_are_replaced(session):
    s = session
    ch = make_channel(s)
    a, b = make_item(s, 1), make_item(s, 2)
    assign_to_channel(s, a, ch.id)
    assign_to_channel(s, b, ch.id)
    s.flush()
    s.refresh(ch)
    extend(s, ch, NOW, days=1)
    set_review_status(s, b, ReviewStatus.REJECTED)
    s.flush()
    res = maintain(s, ch, NOW)
    assert res["rebuilt_from"] is not None
    future = [e for e in entries(s, ch) if e.start > NOW + timedelta(minutes=2)]
    assert future and all(e.media_item_id == a.id for e in future)
    assert check_continuity(entries(s, ch)) == []


def test_unhealthy_source_excluded(session):
    s = session
    ch = make_channel(s)
    a, b = make_item(s, 1), make_item(s, 2)
    for it in (a, b):
        assign_to_channel(s, it, ch.id)
    b.playback_sources[0].health_ok = False
    s.flush()
    s.refresh(ch)
    extend(s, ch, NOW, days=1)
    assert {e.media_item_id for e in entries(s, ch)} == {a.id}


def test_block_scheduling_and_filler(session):
    s = session
    ch = make_channel(s, mode=ScheduleMode.BLOCK)
    cartoons = [make_item(s, i, dur=420, tags=["cartoons"]) for i in range(3)]
    news = [make_item(s, 10 + i, dur=900, tags=["newsreel"]) for i in range(3)]
    filler = make_item(s, 99, dur=60)
    for it in cartoons + news:
        assign_to_channel(s, it, ch.id)
    assign_to_channel(s, filler, ch.id, ChannelRole.FILLER)
    s.add(ChannelBlock(channel_id=ch.id, name="Cartoons", start_time="07:00", end_time="09:00", tag_filter="cartoons", mode=ScheduleMode.SEQUENTIAL))
    s.add(ChannelBlock(channel_id=ch.id, name="Newsreels", start_time="18:00", end_time="19:00", tag_filter="newsreel", mode=ScheduleMode.SHUFFLE))
    s.flush()
    s.refresh(ch)
    extend(s, ch, NOW, days=2)
    es = entries(s, ch)
    assert check_continuity(es) == []
    cartoon_ids = {c.id for c in cartoons}
    in_block = [e for e in es if e.block_name == "Cartoons" and e.kind == EntryKind.PROGRAMME]
    assert in_block and all(e.media_item_id in cartoon_ids for e in in_block)
    assert any(e.kind == EntryKind.FILLER for e in es)
    # 07:00 London on 5 Nov 2026 is 07:00 UTC (GMT).
    blk, _ = active_block(ch.blocks, datetime(2026, 11, 5, 7, 30))
    assert blk is not None and blk.name == "Cartoons"
    blk, _ = active_block(ch.blocks, datetime(2026, 7, 5, 6, 30))  # 07:30 BST
    assert blk is not None and blk.name == "Cartoons"


def test_override(session):
    s = session
    ch = make_channel(s)
    a, b = make_item(s, 1, dur=1000), make_item(s, 2, dur=1000)
    spot = make_item(s, 3, dur=500)
    for it in (a, b, spot):
        assign_to_channel(s, it, ch.id)
    s.flush()
    s.refresh(ch)
    extend(s, ch, NOW, days=1)
    target = entries(s, ch)[5]
    new = override(s, ch, target.id, spot.id, NOW)
    es = entries(s, ch)
    assert new.overridden and new.media_item_id == spot.id
    assert check_continuity(es) == []


def test_feeds(session):
    s = session
    ch = make_channel(s)
    it = make_item(s, 1)
    it.season, it.episode, it.series = 1, 2, "Show & Tell"
    assign_to_channel(s, it, ch.id)
    s.flush()
    s.refresh(ch)
    from archivetv import feeds

    extend(s, ch, feeds.utcnow(), days=1)
    m3u = build_m3u(s, "http://archivetv:8090")
    assert m3u.startswith("#EXTM3U")
    assert 'tvg-id="retro.archivetv"' in m3u and "http://archivetv:8090/live/retro.ts" in m3u
    xml = build_xmltv(s, days=1)
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml)
    assert root.find("channel").get("id") == "retro.archivetv"
    progs = root.findall("programme")
    assert progs and progs[0].find("title").text == "Show & Tell"
    assert progs[0].find("episode-num").text == "0.1."


def test_migrations_match_models(tmp_path):
    """The Alembic baseline builds exactly the schema the models describe."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    from archivetv.db import Base, migrate

    eng = create_engine(f"sqlite:///{tmp_path / 'm.sqlite3'}")
    migrate(eng)
    migrate(eng)  # idempotent on restart
    with eng.connect() as c:
        diff = compare_metadata(MigrationContext.configure(c, opts={"render_as_batch": True}), Base.metadata)
    assert diff == []


def test_schedule_versions_record_changes(session):
    from archivetv.models import ScheduleVersion
    from archivetv.scheduler import maintain, override, regenerate

    s = session
    from archivetv.db import utcnow

    ch = make_channel(s, slug="ver")
    items = [make_item(s, f"v{i}", 600) for i in range(3)]
    for it in items:
        assign_to_channel(s, it, ch.id, ChannelRole.PROGRAMME)
    s.commit()
    s.refresh(ch)
    maintain(s, ch)
    maintain(s, ch)  # nothing new: no extra row
    regenerate(s, ch, actor="tester")
    later = s.query(ScheduleEntry).filter(ScheduleEntry.channel_id == ch.id, ScheduleEntry.start > utcnow() + timedelta(hours=2)).first()
    override(s, ch, later.id, items[0].id, actor="tester")
    s.commit()
    vs = s.query(ScheduleVersion).filter_by(channel_id=ch.id).order_by(ScheduleVersion.id).all()
    assert [v.reason for v in vs] == ["extend", "regenerate", "override"]
    assert vs[1].actor == "tester" and vs[1].entries_removed > 0 and vs[1].entries_added > 0
    assert "replaced by" in vs[2].note and vs[2].horizon is not None


def test_logo_validation(tmp_path, monkeypatch):
    import pytest as _pytest

    from archivetv import logos
    from archivetv.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    name = logos.save("retro", png)
    assert logos.NAME.match(name) and logos.path_for(name).read_bytes() == png
    name2 = logos.save("retro", png + b"1", previous=name)
    assert logos.path_for(name) is None and logos.path_for(name2) is not None
    for bad in (b"<svg onload=alert(1)>", b"", b"GIF89a" + b"0" * 10, b"\xff\xd8\xff" + b"0" * (logos.MAX_BYTES + 1)):
        with _pytest.raises(logos.LogoError):
            logos.save("retro", bad)
    assert logos.path_for("../../etc/passwd") is None


def test_dst_changeover_nights(session):
    """Clocks go back (25 Oct 2026) and forward (29 Mar 2026): schedules stay
    gap-free in UTC and a 00:30–02:30 local block covers 3h / 1h of real time."""
    from archivetv.scheduler import block_intervals

    s = session
    ch = make_channel(s, mode=ScheduleMode.BLOCK, slug="dst")
    for i in range(4):
        assign_to_channel(s, make_item(s, f"dst{i}", dur=1500, tags=["night"]), ch.id)
    s.add(ChannelBlock(channel_id=ch.id, name="Night", start_time="00:30", end_time="02:30", tag_filter="night",
                       mode=ScheduleMode.SEQUENTIAL))
    s.flush()
    s.refresh(ch)
    for start, expect_hours in ((datetime(2026, 10, 24, 12), 3.0), (datetime(2026, 3, 28, 12), 1.0)):
        extend(s, ch, start, days=2)
        es = [e for e in entries(s, ch) if start <= e.start < start + timedelta(days=2)]
        assert check_continuity(es) == [], start
        night = [iv for iv in block_intervals(ch.blocks, start + timedelta(hours=12)) if iv[0].date() in
                 ((start + timedelta(hours=12)).date(), start.date())]
        changeover = [iv for iv in night if abs((iv[1] - iv[0]).total_seconds() / 3600 - expect_hours) < 0.01]
        assert changeover, [(a, b) for a, b, _ in night]
        s.query(ScheduleEntry).filter(ScheduleEntry.channel_id == ch.id).delete()
        s.flush()


def test_discovery_respects_max_results(session, monkeypatch):
    """A scan takes at most max_results candidates from the provider. An
    Archive item holding several videos is one candidate and is imported
    whole, so rescans skip it rather than leaving videos behind."""
    import asyncio

    from archivetv import discovery
    from archivetv.providers.base import MediaRecord, PlaybackOption

    class Fake:
        name, label = "archive", "Internet Archive"

        async def search(self, query, params, kind, max_results):
            return [MediaRecord(provider="archive", external_id=f"item{i}", title=f"Item {i}") for i in range(max_results)]

        async def expand(self, stubs):  # every item holds three videos
            return [MediaRecord(provider="archive", external_id=f"{st.external_id}/v{j}", title=f"{st.title} part {j}",
                                parent_external_id=st.external_id,
                                duration=600.0, playback=[PlaybackOption(PlaybackType.DIRECT_URL, f"https://archive.org/download/{st.external_id}/v{j}.mp4")])
                    for st in stubs for j in range(3)]

    async def no_validate(ids, concurrency=3):
        return None

    async def no_notify(msg):
        return None

    monkeypatch.setattr(discovery, "get_provider", lambda name: Fake())
    monkeypatch.setattr(discovery, "validate_items", no_validate)
    monkeypatch.setattr(discovery, "notify", no_notify)
    res = asyncio.run(discovery.run_query("archive", "search", "x", {}, max_results=4))
    assert res.found == 4 and res.new == 12  # 4 candidates, every video of each
    again = asyncio.run(discovery.run_query("archive", "search", "x", {}, max_results=4))
    assert again.new == 0 and again.skipped_known == 4  # already known items are not re-queued
