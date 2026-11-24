"""Break cards, HLS segmenting and the public guide API."""

import shutil
import subprocess
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from archivetv import cards
from archivetv.catalogue import assign_to_channel
from archivetv.catchup import resolve_request, start_over_time
from archivetv.config import settings
from archivetv.enums import EntryKind, PlaybackType
from archivetv.feeds import build_xmltv
from archivetv.hls import HlsChannel
from archivetv.models import PlaybackSource
from archivetv.scheduler import check_continuity, entry_still_valid, extend, maintain
from tests.test_scheduler import NOW, entries, make_channel, make_item

FFMPEG = shutil.which("ffmpeg")


def breaks_channel(s, n=3, secs=10):
    ch = make_channel(s)
    ch.breaks_enabled, ch.break_seconds = True, secs
    items = [make_item(s, i, dur=1200) for i in range(n)]
    for it in items:
        assign_to_channel(s, it, ch.id)
    s.flush()
    s.refresh(ch)
    return ch, items


# ------------------------------------------------------------------ breaks


def test_breaks_sit_before_every_programme(session):
    s = session
    ch, _ = breaks_channel(s)
    extend(s, ch, NOW, days=1)
    es = entries(s, ch)
    assert check_continuity(es) == []
    kinds = [e.kind for e in es[:6]]
    assert kinds == [EntryKind.BREAK, EntryKind.PROGRAMME] * 3
    assert all((e.end - e.start).total_seconds() == 10 for e in es if e.kind == EntryKind.BREAK)
    assert es[0].title == f"Coming up: {es[1].title}"
    assert all(entry_still_valid(e, s) for e in es[:6])
    # Maintenance leaves valid breaks alone.
    assert maintain(s, ch, NOW)["rebuilt_from"] is None


def test_no_breaks_when_off(session):
    s = session
    ch, _ = breaks_channel(s)
    ch.breaks_enabled = False
    extend(s, ch, NOW, days=1)
    assert EntryKind.BREAK not in {e.kind for e in entries(s, ch)}


def test_guide_folds_breaks(session):
    s = session
    ch, _ = breaks_channel(s)
    extend(s, ch, NOW, days=1)
    xml = build_xmltv(s, days=1, past_hours=0)
    assert "Coming up" not in xml


def test_catchup_on_a_break_means_the_next_programme(session):
    s = session
    ch, _ = breaks_channel(s)
    extend(s, ch, NOW - timedelta(hours=3), days=1)
    es = entries(s, ch)
    brk, prog = es[2], es[3]
    req = resolve_request(s, ch, brk.start + timedelta(seconds=3), now=NOW)
    assert req.entry_id == prog.id and req.offset == 0
    # Start Over during a break restarts the programme that just ended.
    assert start_over_time(s, ch, now=brk.start + timedelta(seconds=2)) == es[1].start


def test_break_card_lineup(session):
    s = session
    ch, items = breaks_channel(s)
    items[0].publication_date = "1958-03-01"
    items[0].thumbnail_url = "https://archive.org/services/img/item0"
    extend(s, ch, NOW, days=1)
    es = entries(s, ch)
    card = cards.break_card(s, ch, es[0])
    assert card.headline == "UP NEXT"
    assert [r.title for r in card.rows] == [es[1].title, es[3].title, es[5].title]
    assert card.credit == "via archive.org"
    assert card.accent == cards.channel_accent(ch)  # one look per channel
    assert card.rows[0].ampm in ("AM", "PM") and "." in card.rows[0].time


def test_thumbnail_hosts_are_restricted():
    assert cards._thumb_host_ok("https://archive.org/services/img/x")
    assert cards._thumb_host_ok("https://i.ytimg.com/vi/abc/hqdefault.jpg")
    assert not cards._thumb_host_ok("http://archive.org/services/img/x")
    assert not cards._thumb_host_ok("https://evil.example/x.jpg")


@pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg")
@pytest.mark.parametrize("offset,music", [(0.0, False), (1.5, False), (0.0, True)])
def test_card_renders_with_ffmpeg(tmp_path, monkeypatch, offset, music):
    track = None
    if music:
        p = tmp_path / "song.wav"
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=1", str(p)], check=True)
        track = cards.MusicTrack(p, "Song — Artist (CC BY 4.0)")
    rows = [cards.LineupRow("6.30", "PM", "It's a: \"Test\" 100% {title}"), cards.LineupRow("7.00", "PM", "Two")]
    card = cards.CardSpec(1, "Retro", 4, "UP NEXT", rows, "via archive.org", track)
    r = cards.render(card, 2.0, offset)
    out = tmp_path / "card.ts"
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *r.inputs, "-filter_complex", r.filter_complex,
           "-map", "[v]", "-map", "[a]", "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-f", "mpegts", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    r.cleanup()
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert not r.workdir.exists()
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True)
    assert abs(float(probe.stdout) - 2.0) < 0.3


def test_music_library_reads_credits(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "music_dir", tmp_path)
    (tmp_path / "b_tune.mp3").write_bytes(b"x")
    (tmp_path / "a.ogg").write_bytes(b"x")
    (tmp_path / "a.ogg.txt").write_text("Morning — Someone (CC0)\nmore")
    (tmp_path / "notes.txt").write_text("ignored")
    lib = cards.music_library()
    assert [t.credit for t in lib] == ["Morning — Someone (CC0)", "b tune"]
    assert cards.pick_music(3).credit == "b tune"


# --------------------------------------------------------------------- HLS


def _ts(path, seconds, size="320x180", rate=25):
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=s={size}:r={rate}:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=d={seconds}", "-c:v", "libx264", "-g", "25", "-c:a", "aac",
                    "-mpegts_flags", "+initial_discontinuity+resend_headers", "-mpegts_pmt_start_pid", "4096",
                    "-mpegts_start_pid", "256", "-output_ts_offset", "0" if "a" in path.name else str(seconds + 0.2),
                    "-f", "mpegts", str(path)], check=True)
    return path.read_bytes()


@pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg")
def test_hls_segments_at_keyframes_and_marks_programme_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "hls_segment_seconds", 2.0)
    monkeypatch.setattr(settings, "hls_window", 4)
    a = _ts(tmp_path / "a.ts", 7)
    b = _ts(tmp_path / "b.ts", 5, size="640x360")  # a second FFmpeg run: new programme
    hc = HlsChannel(None, "retro", 1)
    for blob in (a, b):
        for i in range(0, len(blob), 188 * 100):
            hc.feed(blob[i: i + 188 * 100])
    hc._close()
    segs = list(hc.segments)
    assert len(segs) >= 5
    assert all(sg.data[:1] == b"\x47" and ((sg.data[1] & 0x1F) << 8 | sg.data[2]) == 0 for sg in segs)  # PAT first
    assert sum(sg.discontinuity for sg in segs) == 1
    # Cut on the 1 s GOPs once 2 s is reached; the segment before a
    # programme change ends early, at the change.
    assert segs[0].duration == pytest.approx(2.0, abs=0.05)
    assert max(sg.duration for sg in segs) <= 2.05
    pl = hc.playlist(lambda n: f"/hls/retro/{n}.ts")
    assert pl.startswith("#EXTM3U") and "#EXT-X-DISCONTINUITY\n" in pl
    assert pl.count("#EXTINF") == 4  # the window
    assert f"#EXT-X-MEDIA-SEQUENCE:{segs[-4].seq}" in pl
    assert hc.segment(segs[-1].seq) is segs[-1] and hc.segment(10_000) is None


def test_hls_discontinuity_sequence_counts_dropped_ones():
    from archivetv.hls import Segment

    hc = HlsChannel(None, "x", 1)
    hc.segments.extend([Segment(5, b"", 2.0, True, 2), Segment(6, b"", 2.0, False, 2), Segment(7, b"", 2.0, True, 3)])
    pl = hc.playlist(str)
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:1" in pl and "#EXT-X-MEDIA-SEQUENCE:5" in pl


# ----------------------------------------------------------- public guide


@pytest.fixture()
def public(session, monkeypatch):
    from archivetv.main import app

    monkeypatch.setattr(settings, "feed_token", None)
    monkeypatch.setattr(settings, "public_guide", True)
    monkeypatch.setattr(settings, "public_base_url", "http://tv.test")
    return TestClient(app)


def _on_air(s, provider="archive"):
    ch = make_channel(s)
    ch.breaks_enabled = False
    it = make_item(s, 1, dur=7200, provider=provider)
    if provider == "youtube":
        it.source.external_id = "dQw4w9WgXcQ"
        it.playback_sources[0].playback_type = PlaybackType.YOUTUBE_STREAM
        it.playback_sources.append(PlaybackSource(playback_type=PlaybackType.YOUTUBE_EMBED,
                                                  url="https://www.youtube.com/embed/dQw4w9WgXcQ"))
    else:
        it.playback_sources[0].container = "mov,mp4,m4a,3gp,3g2,mj2"  # as FFprobe reports it
    assign_to_channel(s, it, ch.id)
    s.flush()
    s.refresh(ch)
    from archivetv.db import utcnow

    extend(s, ch, utcnow() - timedelta(minutes=30), days=1)
    s.commit()
    return ch


def test_public_now_gives_direct_archive_playback(public, session):
    _on_air(session)
    r = public.get("/api/public/channels/retro/now")
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
    on = r.json()["on_air"]
    assert on["playback"]["type"] == "file" and on["playback"]["url"].startswith("https://archive.org/")
    assert 0 < on["playback"]["offset"] <= 7200 and on["offset"] == pytest.approx(on["playback"]["offset"], abs=1)
    assert r.json()["streams"]["hls"] == "http://tv.test/hls/retro/index.m3u8"


def test_public_now_gives_youtube_embed(public, session, monkeypatch):
    monkeypatch.setattr(settings, "youtube_streaming", True)  # how a YouTube item airs on IPTV
    _on_air(session, provider="youtube")
    pb = public.get("/api/public/channels/retro/now").json()["on_air"]["playback"]
    assert pb["type"] == "youtube" and pb["video_id"] == "dQw4w9WgXcQ" and pb["start"] > 0
    assert f"start={pb['start']}" in pb["embed_url"]


def test_public_channels_and_guide(public, session):
    _on_air(session)
    chans = public.get("/api/public/channels").json()["channels"]
    assert chans[0]["slug"] == "retro" and chans[0]["now"]["title"] == "Programme 1"
    g = public.get("/api/public/guide?hours=3").json()
    assert g["channels"][0]["programmes"]
    assert public.get("/api/public/guide?hours=500").status_code == 422
    assert public.get("/api/public/channels/nope/now").status_code == 404


def test_public_guide_follows_token_and_switch(public, session, monkeypatch):
    _on_air(session)
    monkeypatch.setattr(settings, "feed_token", "sekrit")
    assert public.get("/api/public/channels").status_code == 403
    assert public.get("/api/public/channels?token=sekrit").status_code == 200
    monkeypatch.setattr(settings, "public_guide", False)
    assert public.get("/api/public/channels?token=sekrit").status_code == 404


def test_m3u_can_list_hls(public, session):
    _on_air(session)
    body = public.get("/playlist.m3u?format=hls").text
    assert "/hls/retro/index.m3u8" in body
    assert public.get("/hls/retro/0.ts").status_code == 404  # nothing segmented yet


# ------------------------------------------------------------ looks & logos


def test_look_resolution(session):
    from archivetv import themes
    from archivetv.models import Channel

    ch = Channel(slug="x", tvg_id="x.t", name="Midnight Monster Movies", seed="s")
    assert themes.resolve(ch).key == "creature"
    assert themes.resolve(ch, pack_look="noir").key == "noir"  # a pack's look beats a guess
    ch.card_theme = "toon"
    assert themes.resolve(ch, pack_look="noir").key == "toon"  # the admin's choice beats both
    assert themes.resolve(Channel(slug="y", tvg_id="y.t", name="Channel Seven", seed="s")).key == "broadcast"
    assert {c["key"] for c in themes.choices()} >= {"auto", "broadcast", "creature", "newsreel"}


def test_pack_look_must_exist():
    from archivetv import packs
    from tests.test_packs import SPEC

    assert packs.parse_spec({**SPEC, "look": "creature"}).look == "creature"
    with pytest.raises(packs.PackError):
        packs.parse_spec({**SPEC, "look": "glitter"})


@pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg")
@pytest.mark.parametrize("look", ["broadcast", "frontier", "vhs", "newsreel"])
def test_every_look_renders_card_and_logo(tmp_path, look):
    from archivetv import themes

    from archivetv import marks

    th = themes.THEMES[look]
    png = marks.render_mark("Monster & Sci-Fi Late Show", 13, th, "0x1f8a8a")
    assert png.startswith(b"\x89PNG")
    card = cards.CardSpec(1, "Retro", 4, "UP NEXT", [cards.LineupRow("6.30", "PM", "One")], theme=th, accent="0x1f8a8a")
    r = cards.render(card, 1.0, 0.0)
    proc = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", *r.inputs, "-filter_complex", r.filter_complex,
                           "-map", "[v]", "-map", "[a]", "-f", "null", "-"], capture_output=True, text=True, timeout=60)
    r.cleanup()
    assert proc.returncode == 0, proc.stderr[-1500:]


@pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg")
def test_logos_inherit_or_generate(session, monkeypatch, tmp_path):
    from archivetv import logos

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    s = session
    ch = make_channel(s)
    ch.logo_url = "https://example.org/logo.png"
    assert logos.generate(s, ch) is None  # a linked logo is inherited
    ch.logo_url = None
    name = logos.generate(s, ch)
    assert name and ch.logo_generated and logos.path_for(name).read_bytes().startswith(b"\x89PNG")
    ch.name = "Renamed"
    again = logos.generate(s, ch)
    assert again != name and logos.path_for(name) is None  # redrawn, old file removed
    ch.logo_file, ch.logo_generated = "uploaded-0123456789ab.png", False
    assert logos.generate(s, ch) is None  # an upload is never replaced unless forced


@pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg")
def test_api_logo_generate_and_card_preview(session, monkeypatch, tmp_path):
    from archivetv.main import app
    from archivetv.web import admin

    monkeypatch.setattr(settings, "admin_password", "pw")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    admin._attempts.clear()
    c = TestClient(app)
    c.post("/api/v1/auth/login", json={"username": "admin", "password": "pw"})
    ch, _ = breaks_channel(session)
    extend(session, ch, NOW, days=1)
    session.commit()
    r = c.post(f"/api/v1/channels/{ch.id}/logo/generate")
    assert r.status_code == 200 and r.json()["logo_generated"] is True
    assert c.patch(f"/api/v1/channels/{ch.id}", json={"card_theme": "nope"}).status_code == 400
    assert c.patch(f"/api/v1/channels/{ch.id}", json={"card_theme": "noir"}).json()["card_theme"] == "noir"
    assert c.get(f"/api/v1/channels/{ch.id}").json()["look"] == "noir"
    monkeypatch.setattr(cards, "THUMB_HOSTS", ())  # no network in tests
    r = c.get(f"/api/v1/channels/{ch.id}/card-preview.png")
    assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
    assert any(x["key"] == "noir" for x in c.get("/api/v1/meta").json()["looks"])


@pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg")
@pytest.mark.parametrize("look", ["broadcast", "creature", "toon", "noir", "frontier", "newsreel", "classroom", "vhs", "concert"])
def test_every_mark_design_renders_with_alpha(look):
    from archivetv import marks, themes

    th = themes.THEMES[look]
    for number in (7, None):
        png = marks.render_mark("Classroom Films", number, th, "0x1f8a8a", None)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height,pix_fmt", "-of", "csv=p=0", "-"],
                               input=png, capture_output=True)
        assert probe.stdout.decode().strip() == "512,512,rgba"


def test_wordmarks():
    from archivetv.marks import wordmark

    assert wordmark("Classroom Films") == "CF"
    assert wordmark("Cartoons") == "CARTOONS"
    assert wordmark("Retro TV") == "RETRO"
    assert wordmark("The Monster & Sci-Fi Channel") == "MSF"
    assert wordmark("Film Noir", "noir") == "NOIR"


def test_break_style_resolution(session):
    from archivetv import themes

    s = session
    ch = make_channel(s)
    assert themes.break_style(s, ch) == "lineup"  # broadcast look
    ch.card_theme = "toon"
    assert themes.break_style(s, ch) == "bumper"
    ch.break_style = "lineup"
    assert themes.break_style(s, ch) == "lineup"


def test_bumper_card_previews_the_next_programme(session):
    s = session
    ch, items = breaks_channel(s)
    ch.card_theme, ch.break_style = "toon", "auto"
    extend(s, ch, NOW, days=1)
    es = entries(s, ch)
    card = cards.break_card(s, ch, es[0])
    assert card.style == "bumper"
    assert card.clip is not None and card.clip.url == es[1].playback_source.url
    assert 60 <= card.clip.offset <= 1200 - 60  # a quarter in, clear of the ends
    es[1].playback_source.live = True
    assert cards.break_card(s, ch, es[0]).clip is None  # live sources can't be previewed


@pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg")
@pytest.mark.parametrize("offset,with_clip", [(0.0, True), (4.5, True), (0.0, False)])
def test_bumper_renders(tmp_path, offset, with_clip):
    from archivetv import marks, themes

    th = themes.THEMES["toon"]
    clip = tmp_path / "next.mp4"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=640x480:r=25:d=20",
                    "-c:v", "libx264", "-preset", "ultrafast", str(clip)], check=True)
    logo = tmp_path / "logo.png"
    logo.write_bytes(marks.render_mark("Cartoons", 21, th, th.accent, "TOONS"))
    rows = [cards.LineupRow("6.30", "PM", "It's: \"Quoted\" 100%"), cards.LineupRow("7.00", "PM", "Two"), cards.LineupRow("7.30", "PM", "Three")]
    card = cards.CardSpec(1, "Cartoons", 21, "UP NEXT", rows, "via archive.org", None, th.accent, th, style="bumper",
                          clip=cards.ClipSource(str(clip), 5.0) if with_clip else None, logo_path=str(logo))
    r = cards.render(card, 6.0, offset)
    out = tmp_path / "b.ts"
    proc = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *r.inputs, "-filter_complex", r.filter_complex,
                           "-map", "[v]", "-map", "[a]", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                           "-f", "mpegts", str(out)], capture_output=True, text=True, timeout=120)
    r.cleanup()
    assert proc.returncode == 0, proc.stderr[-2000:]
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True)
    assert abs(float(probe.stdout) - 6.0) < 0.3
