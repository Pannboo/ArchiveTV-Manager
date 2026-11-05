"""Duplicate detection: identical files (checksums, URLs), then title,
episode and duration. Duplicates are flagged, never removed."""

from archivetv.catalogue import file_sha1, find_duplicate, upsert_record
from archivetv.enums import PlaybackType
from archivetv.models import MediaItem, MediaSource, PlaybackSource
from archivetv.providers.archive import file_checksum
from archivetv.providers.base import MediaRecord, PlaybackOption


def _rec(ext_id, title, url, checksum=None, duration=600.0, provider="archive", episode=None):
    return MediaRecord(provider=provider, external_id=ext_id, title=title, duration=duration, episode=episode, season=1 if episode else None,
                       playback=[PlaybackOption(PlaybackType.DIRECT_URL, url, checksum=checksum, iptv_capable=True)])


def test_archive_checksum_prefers_sha1():
    assert file_checksum({"md5": "AB12", "sha1": "cd34"}) == "sha1:cd34"
    assert file_checksum({"md5": "ab12"}) == "md5:ab12"
    assert file_checksum({"md5": "not-hex"}) is None


def test_identical_file_under_a_different_title(session):
    s = session
    a, _ = upsert_record(s, _rec("x/a", "Duck and Cover", "https://archive.org/download/x/a.mp4", "sha1:aa"))
    b, _ = upsert_record(s, _rec("y/b", "Completely different name", "https://archive.org/download/y/b.mp4", "sha1:aa"))
    assert b.duplicate_of_id == a.id and "identical file (sha1 match)" in b.flags[-1]
    c, _ = upsert_record(s, _rec("z/c", "Another name", "https://archive.org/download/x/a.mp4"))
    assert c.duplicate_of_id == a.id and "same media URL" in c.flags[-1]


def test_title_match_respects_episode_and_duration(session):
    s = session
    a, _ = upsert_record(s, _rec("s/1", "Space Patrol", "https://archive.org/download/s/1.mp4", episode=1))
    b, _ = upsert_record(s, _rec("s/2", "Space Patrol", "https://archive.org/download/s/2.mp4", episode=2))
    assert b.duplicate_of_id is None  # different episode of the same series
    c, _ = upsert_record(s, _rec("s/1b", "Space Patrol", "https://archive.org/download/s/1b.mp4", episode=1, duration=605))
    assert c.duplicate_of_id == a.id and "same title and duration" in c.flags[-1]
    d, _ = upsert_record(s, _rec("s/1c", "Space Patrol", "https://archive.org/download/s/1c.mp4", episode=1, duration=1800))
    assert d.duplicate_of_id is None


def test_local_file_matches_remote_by_hash(session, tmp_path):
    f = tmp_path / "clip.bin"
    f.write_bytes(b"same bytes" * 1000)
    s = session
    remote, _ = upsert_record(s, _rec("r/1", "Remote copy", "https://archive.org/download/r/1.mp4", file_sha1(f)))
    local = MediaItem(source=MediaSource(provider="local", external_id="clip.bin"), title="Local copy", title_key="local copy")
    local.playback_sources.append(PlaybackSource(playback_type=PlaybackType.LOCAL_FILE, local_path="clip.bin", checksum=file_sha1(f)))
    s.add(local)
    s.flush()
    dup, why = find_duplicate(s, local)
    assert dup.id == remote.id and "sha1" in why


def test_rule_preferences_rerank_sources():
    from archivetv.discovery import apply_preferences

    rec = MediaRecord(provider="archive", external_id="p/1", title="T", playback=[
        PlaybackOption(PlaybackType.DIRECT_URL, "u1", label="a.mp4 (h.264)", container="mp4", height=480, priority=10, iptv_capable=True),
        PlaybackOption(PlaybackType.DIRECT_URL, "u2", label="a.mkv (Matroska)", container="mkv", height=1080, priority=40, iptv_capable=True),
        PlaybackOption(PlaybackType.DIRECT_URL, "u3", label="a_512kb.mp4 (512Kb MPEG4)", container="mp4", height=240, priority=20, iptv_capable=True),
        PlaybackOption(PlaybackType.ARCHIVE_EMBED, "e", priority=200),
    ])
    apply_preferences(rec, {"preferred_formats": "matroska, h.264"})
    assert min(rec.playback[:3], key=lambda o: o.priority).url == "u2"
    rec2 = MediaRecord(provider="archive", external_id="p/2", title="T", playback=[
        PlaybackOption(PlaybackType.DIRECT_URL, "lo", label="lo.mp4 (h.264)", height=240, priority=10, iptv_capable=True),
        PlaybackOption(PlaybackType.DIRECT_URL, "hi", label="hi.mp4 (h.264)", height=720, priority=10, iptv_capable=True),
    ])
    apply_preferences(rec2, {"preferred_height": "720"})
    assert min(rec2.playback, key=lambda o: o.priority).url == "hi"
    assert rec.playback[3].priority == 200  # embed options untouched
