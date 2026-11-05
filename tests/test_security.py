import asyncio

import pytest

from archivetv.catalogue import set_rights
from archivetv.enums import PlaybackType, RightsStatus
from archivetv.models import MediaItem, MediaSource, PlaybackSource
from archivetv.security import UnsafeURLError, check_url_syntax, safe_local_path, validate_media_url


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://rr3---sn-abc.googlevideo.com/videoplayback?x=1",
        "file:///etc/passwd",
        "ftp://archive.org/x.mp4",
        "https://user:pw@archive.org/x.mp4",
        "https://evil.example.com/x.mp4",
        "https://archive.org.evil.com/x.mp4",
    ],
)
def test_rejected_media_urls(url):
    with pytest.raises(UnsafeURLError):
        check_url_syntax(url)


def test_allowed_archive_url():
    check_url_syntax("https://archive.org/download/AboutBan1935/AboutBan1935.mp4")
    check_url_syntax("https://ia800300.us.archive.org/0/items/x/y.mp4")


def test_private_address_blocked():
    with pytest.raises(UnsafeURLError):
        asyncio.run(validate_media_url("http://127.0.0.1/x.mp4"))


def test_local_path_escape(tmp_path):
    with pytest.raises(UnsafeURLError):
        safe_local_path("../../etc/passwd")


def test_youtube_source_cannot_be_authorised(session):
    item = MediaItem(source=MediaSource(provider="youtube", external_id="dQw4w9WgXcQ"), title="x")
    emb = PlaybackSource(playback_type=PlaybackType.YOUTUBE_EMBED, url="https://www.youtube.com/embed/dQw4w9WgXcQ")
    sneaky = PlaybackSource(playback_type=PlaybackType.DIRECT_URL, url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    item.playback_sources += [emb, sneaky]
    session.add(item)
    session.flush()
    with pytest.raises(ValueError):
        set_rights(session, emb, RightsStatus.AUTHORISED)
    with pytest.raises(ValueError):
        set_rights(session, sneaky, RightsStatus.AUTHORISED)
    set_rights(session, emb, RightsStatus.CATALOGUE_ONLY)
