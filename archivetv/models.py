"""Unified catalogue data model.

The model deliberately keeps five concerns apart:

1. where content was discovered          -> MediaSource (provider, external id)
2. whether an administrator approved it  -> MediaItem.review_status
3. whether it may legally/technically air -> PlaybackSource.rights_status / eligibility
4. which media actually supplies playback -> PlaybackSource
5. when it airs                           -> ScheduleEntry
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow
from .enums import (
    ChannelRole,
    Eligibility,
    EntryKind,
    PlaybackFormat,
    PlaybackType,
    ReviewStatus,
    RightsStatus,
    RuleKind,
    ScheduleMode,
)

rule_channels = Table(
    "rule_channels",
    Base.metadata,
    Column("rule_id", ForeignKey("discovery_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("channel_id", ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True),
)


class MediaSource(Base):
    __tablename__ = "media_sources"
    __table_args__ = (UniqueConstraint("provider", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(512))
    parent_external_id: Mapped[str | None] = mapped_column(String(256), index=True)
    original_url: Mapped[str | None] = mapped_column(String(1024))
    creator: Mapped[str | None] = mapped_column(String(512))
    creator_id: Mapped[str | None] = mapped_column(String(256))
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    discovered_by_rule_id: Mapped[int | None] = mapped_column(ForeignKey("discovery_rules.id", ondelete="SET NULL"))
    availability_status: Mapped[str] = mapped_column(String(32), default="unknown")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    item: Mapped["MediaItem"] = relationship(back_populates="source", uselist=False)
    rule: Mapped["DiscoveryRule | None"] = relationship()


class MediaItem(Base):
    __tablename__ = "media_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("media_sources.id", ondelete="CASCADE"), unique=True)
    title: Mapped[str] = mapped_column(String(512))
    # Normalised title used for duplicate detection across providers.
    title_key: Mapped[str] = mapped_column(String(512), default="", index=True)
    description: Mapped[str | None] = mapped_column(Text)
    publication_date: Mapped[str | None] = mapped_column(String(32))
    duration: Mapped[float | None] = mapped_column(Float)
    thumbnail_url: Mapped[str | None] = mapped_column(String(1024))
    tags: Mapped[list] = mapped_column(JSON, default=list)
    categories: Mapped[list] = mapped_column(JSON, default=list)
    series: Mapped[str | None] = mapped_column(String(256))
    season: Mapped[int | None] = mapped_column(Integer)
    episode: Mapped[int | None] = mapped_column(Integer)
    review_status: Mapped[str] = mapped_column(String(32), default=ReviewStatus.DISCOVERED, index=True)
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("media_items.id", ondelete="SET NULL"))
    # Warnings for the reviewer: missing rights info, restrictions, etc.
    flags: Mapped[list] = mapped_column(JSON, default=list)
    # Provider facts shown in review (restrictions, embeddable, live status...)
    restrictions: Mapped[dict] = mapped_column(JSON, default=dict)
    artwork_in_epg: Mapped[bool] = mapped_column(Boolean, default=True)
    # Set once an admin edits metadata, so provider refreshes don't clobber it.
    metadata_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    source: Mapped[MediaSource] = relationship(back_populates="item")
    playback_sources: Mapped[list["PlaybackSource"]] = relationship(
        back_populates="media_item", cascade="all, delete-orphan", order_by="PlaybackSource.priority"
    )
    duplicate_of: Mapped["MediaItem | None"] = relationship(remote_side=[id])

    @property
    def provider(self) -> str:
        return self.source.provider if self.source else "?"


class PlaybackSource(Base):
    __tablename__ = "playback_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    media_item_id: Mapped[int] = mapped_column(ForeignKey("media_items.id", ondelete="CASCADE"), index=True)
    playback_type: Mapped[str] = mapped_column(String(32), default=PlaybackType.DIRECT_URL)
    label: Mapped[str | None] = mapped_column(String(256))
    url: Mapped[str | None] = mapped_column(String(2048))
    local_path: Mapped[str | None] = mapped_column(String(1024))
    container: Mapped[str | None] = mapped_column(String(64))
    video_codec: Mapped[str | None] = mapped_column(String(64))
    audio_codec: Mapped[str | None] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[float | None] = mapped_column(Float)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    checksum: Mapped[str | None] = mapped_column(String(80), index=True)  # "sha1:<hex>" / "md5:<hex>"
    seekable: Mapped[bool | None] = mapped_column(Boolean)
    # Live stream (no fixed length): scheduled into blocks / hourly slots,
    # joined at the live edge, never replayed for catch-up.
    live: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    eligibility: Mapped[str] = mapped_column(String(32), default=Eligibility.UNVERIFIED)
    rights_status: Mapped[str] = mapped_column(String(32), default=RightsStatus.UNREVIEWED)
    rights_note: Mapped[str | None] = mapped_column(Text)
    license_url: Mapped[str | None] = mapped_column(String(1024))
    health_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    allow_cache: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    media_item: Mapped[MediaItem] = relationship(back_populates="playback_sources")


class DiscoveryRule(Base):
    __tablename__ = "discovery_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    provider: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32), default=RuleKind.SEARCH)
    query: Mapped[str | None] = mapped_column(String(1024))
    # Filters: collection, creator, subject, year_from, year_to, min_duration,
    # max_duration, categories, excluded_channels, excluded_keywords,
    # published_after, published_before, channel, playlist, mediatype...
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    scan_interval_minutes: Mapped[int] = mapped_column(Integer, default=24 * 60)
    max_results: Mapped[int] = mapped_column(Integer, default=50)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(String(32))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_new_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    channels: Mapped[list["Channel"]] = relationship(secondary=rule_channels, back_populates="rules")


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)  # stable channel id used in URLs
    number: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)
    logo_url: Mapped[str | None] = mapped_column(String(1024))
    logo_file: Mapped[str | None] = mapped_column(String(128))  # uploaded logo in <data>/logos
    tvg_id: Mapped[str] = mapped_column(String(128), unique=True)
    group_title: Mapped[str] = mapped_column(String(128), default="ArchiveTV")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    mode: Mapped[str] = mapped_column(String(32), default=ScheduleMode.SEQUENTIAL)
    seed: Mapped[str] = mapped_column(String(64), default="archivetv")
    repeat_avoid_count: Mapped[int] = mapped_column(Integer, default=10)
    idents_between: Mapped[bool] = mapped_column(Boolean, default=False)
    # Pad up to block boundaries with filler clips where they fit.
    filler_pad: Mapped[bool] = mapped_column(Boolean, default=True)
    playback_format: Mapped[str] = mapped_column(String(32), default=PlaybackFormat.AUTO)
    fallback_item_id: Mapped[int | None] = mapped_column(ForeignKey("media_items.id", ondelete="SET NULL"))
    # Schedule-based catch-up: past programmes are replayed from their original
    # source. History is kept catchup_days whether or not catch-up is enabled.
    catchup_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    catchup_days: Mapped[int] = mapped_column(Integer, default=7)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    items: Mapped[list["ChannelItem"]] = relationship(
        back_populates="channel", cascade="all, delete-orphan", order_by="ChannelItem.position"
    )
    blocks: Mapped[list["ChannelBlock"]] = relationship(
        back_populates="channel", cascade="all, delete-orphan", order_by="ChannelBlock.start_time"
    )
    rules: Mapped[list[DiscoveryRule]] = relationship(secondary=rule_channels, back_populates="channels")
    fallback_item: Mapped[MediaItem | None] = relationship()


class ChannelItem(Base):
    __tablename__ = "channel_items"
    __table_args__ = (UniqueConstraint("channel_id", "media_item_id", "role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    media_item_id: Mapped[int] = mapped_column(ForeignKey("media_items.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16), default=ChannelRole.PROGRAMME)
    position: Mapped[int] = mapped_column(Integer, default=0)

    channel: Mapped[Channel] = relationship(back_populates="items")
    media_item: Mapped[MediaItem] = relationship()


class ChannelBlock(Base):
    """A recurring time block (display-timezone wall clock) with its own content filter."""

    __tablename__ = "channel_blocks"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    days: Mapped[str] = mapped_column(String(7), default="0123456")  # Mon=0 .. Sun=6
    start_time: Mapped[str] = mapped_column(String(5))  # "HH:MM"
    end_time: Mapped[str] = mapped_column(String(5))  # "HH:MM"; <= start means past midnight
    # Comma-separated tags/categories; an item matches if it has any of them.
    tag_filter: Mapped[str | None] = mapped_column(String(512))
    mode: Mapped[str] = mapped_column(String(32), default=ScheduleMode.SHUFFLE)

    channel: Mapped[Channel] = relationship(back_populates="blocks")


class ScheduleEntry(Base):
    __tablename__ = "schedule_entries"
    __table_args__ = (Index("ix_schedule_channel_start", "channel_id", "start"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    start: Mapped[datetime] = mapped_column(DateTime)  # UTC
    end: Mapped[datetime] = mapped_column(DateTime)  # UTC
    kind: Mapped[str] = mapped_column(String(16), default=EntryKind.PROGRAMME)
    media_item_id: Mapped[int | None] = mapped_column(ForeignKey("media_items.id", ondelete="SET NULL"))
    playback_source_id: Mapped[int | None] = mapped_column(ForeignKey("playback_sources.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(Text)
    block_name: Mapped[str | None] = mapped_column(String(128))
    overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    # Generator state after this entry, so the schedule can be resumed or
    # rebuilt from any point deterministically.
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    media_item: Mapped[MediaItem | None] = relationship()
    playback_source: Mapped[PlaybackSource | None] = relationship()


class AuditLog(Base):
    """Persistent review/rejection history and other admin actions."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    media_item_id: Mapped[int | None] = mapped_column(ForeignKey("media_items.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(64))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)


class ProviderStatus(Base):
    __tablename__ = "provider_status"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    quota_day: Mapped[str | None] = mapped_column(String(10))
    quota_used: Mapped[int] = mapped_column(Integer, default=0)
    quota_exhausted_until: Mapped[datetime | None] = mapped_column(DateTime)


class AppSetting(Base):
    """Admin-tunable, non-secret settings overriding environment defaults."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(1024))


class PlaybackEvent(Base):
    """Stream errors and notable playback events for the dashboard."""

    __tablename__ = "playback_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    playback_source_id: Mapped[int | None] = mapped_column(ForeignKey("playback_sources.id", ondelete="SET NULL"))
    level: Mapped[str] = mapped_column(String(16), default="error")
    message: Mapped[str] = mapped_column(Text)


class CatchupSession(Base):
    """A historical playback session: an independent playhead replaying the
    published schedule from a past timestamp. Nothing is recorded."""

    __tablename__ = "catchup_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    entry_id: Mapped[int | None] = mapped_column(ForeignKey("schedule_entries.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(512), default="")
    requested_start: Mapped[datetime] = mapped_column(DateTime)  # UTC schedule time
    duration: Mapped[float] = mapped_column(Float)  # seconds of schedule time requested
    protocol: Mapped[str] = mapped_column(String(16), default="direct")  # direct | xc
    client: Mapped[str | None] = mapped_column(String(256))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | completed | stopped | failed
    position: Mapped[datetime | None] = mapped_column(DateTime)  # last schedule time emitted
    bytes_out: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class StreamSession(Base):
    """One run of a shared live channel stream (first viewer to idle stop)."""

    __tablename__ = "stream_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    peak_viewers: Mapped[int] = mapped_column(Integer, default=0)
    bytes_out: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)


class ScheduleVersion(Base):
    """Publication history: one row per change to a channel's published
    timeline (routine extensions are merged into one row per day)."""

    __tablename__ = "schedule_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reason: Mapped[str] = mapped_column(String(32))  # extend | rebuild | regenerate | override | failover | off_air_ended
    actor: Mapped[str] = mapped_column(String(64), default="system")
    changed_from: Mapped[datetime | None] = mapped_column(DateTime)  # earliest entry start affected (UTC)
    horizon: Mapped[datetime | None] = mapped_column(DateTime)  # schedule end after the change (UTC)
    entries_added: Mapped[int] = mapped_column(Integer, default=0)
    entries_removed: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text)
