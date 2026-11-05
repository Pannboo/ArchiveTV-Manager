from enum import StrEnum


class ProviderType(StrEnum):
    ARCHIVE = "archive"
    YOUTUBE = "youtube"
    LOCAL = "local"


class ReviewStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    VALIDATING = "VALIDATING"
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    QUARANTINED = "QUARANTINED"
    UNAVAILABLE = "UNAVAILABLE"


class Eligibility(StrEnum):
    """Technical playback classification of a PlaybackSource."""

    IPTV_READY = "IPTV_READY"
    IPTV_NEEDS_TRANSCODE = "IPTV_NEEDS_TRANSCODE"
    EMBED_ONLY = "EMBED_ONLY"
    REFERENCE_ONLY = "REFERENCE_ONLY"
    UNAVAILABLE = "UNAVAILABLE"
    UNVERIFIED = "UNVERIFIED"


IPTV_ELIGIBLE = {Eligibility.IPTV_READY, Eligibility.IPTV_NEEDS_TRANSCODE}


class RightsStatus(StrEnum):
    """Administrator's rights review of a PlaybackSource, separate from approval."""

    UNREVIEWED = "UNREVIEWED"
    AUTHORISED = "AUTHORISED"  # cleared for IPTV redistribution on this server
    CATALOGUE_ONLY = "CATALOGUE_ONLY"  # fine to list/preview, not to broadcast
    RESTRICTED = "RESTRICTED"


class PlaybackType(StrEnum):
    DIRECT_URL = "direct_url"  # authorised HTTP(S) file or HLS endpoint
    LOCAL_FILE = "local_file"  # file under the media directory
    YOUTUBE_EMBED = "youtube_embed"  # official embedded player only
    ARCHIVE_EMBED = "archive_embed"
    REFERENCE = "reference"  # link only
    # Experimental, opt-in (ATV_YOUTUBE_STREAMING): resolved by yt-dlp at play time.
    YOUTUBE_STREAM = "youtube_stream"


class ChannelRole(StrEnum):
    PROGRAMME = "programme"
    FILLER = "filler"
    IDENT = "ident"


class ScheduleMode(StrEnum):
    SEQUENTIAL = "sequential"
    SHUFFLE = "shuffle"
    BLOCK = "block"


class PlaybackFormat(StrEnum):
    AUTO = "auto"  # remux IPTV_READY sources, transcode the rest
    COPY = "copy"
    TRANSCODE = "transcode"


class EntryKind(StrEnum):
    PROGRAMME = "programme"
    FILLER = "filler"
    IDENT = "ident"
    FALLBACK = "fallback"
    SLATE = "slate"  # nothing eligible: generated "off air" card


class RuleKind(StrEnum):
    SEARCH = "search"
    COLLECTION = "collection"  # archive
    CHANNEL = "channel"  # youtube channel uploads
    PLAYLIST = "playlist"  # youtube playlist
    VIDEO = "video"  # single item / video import
