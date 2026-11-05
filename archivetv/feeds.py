"""M3U playlist and XMLTV guide generation for Dispatcharr."""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import logos
from .config import settings
from .db import utcnow
from .enums import EntryKind
from .models import Channel, ScheduleEntry
from .scheduler import history_days


def _attr(v: str | None) -> str:
    return (v or "").replace('"', "'").replace("\n", " ").strip()


def published_channels(s: Session) -> list[Channel]:
    return list(s.scalars(select(Channel).where(Channel.enabled.is_(True)).order_by(Channel.number, Channel.name)).all())


def _token_qs() -> str:
    return f"?token={quote(settings.feed_token)}" if settings.feed_token else ""


def build_m3u(s: Session, base_url: str) -> str:
    base = base_url.rstrip("/")
    lines = [f'#EXTM3U x-tvg-url="{base}/epg.xml{_token_qs()}" url-tvg="{base}/epg.xml{_token_qs()}"']
    for ch in published_channels(s):
        attrs = [
            f'tvg-id="{_attr(ch.tvg_id)}"',
            f'tvg-name="{_attr(ch.name)}"',
            f'group-title="{_attr(ch.group_title)}"',
        ]
        if ch.number is not None:
            attrs.append(f'tvg-chno="{ch.number}"')
        logo = logos.url(ch, base)
        if logo:
            attrs.append(f'tvg-logo="{_attr(logo)}"')
        lines.append(f"#EXTINF:-1 {' '.join(attrs)},{_attr(ch.name)}")
        lines.append(f"{base}/live/{quote(ch.slug)}.ts{_token_qs()}")
    return "\n".join(lines) + "\n"


def _xmltv_time(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M%S") + " +0000"


def build_xmltv(s: Session, days: int | None = None, past_hours: float | None = None, base_url: str | None = None) -> str:
    """Programme guide. Past programmes are included for each channel's whole
    history period, so catch-up clients can list and request them."""
    now = utcnow()
    end = now + timedelta(days=days or settings.schedule_days)
    channels = published_channels(s)
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
           '<tv generator-info-name="ArchiveTV Manager" source-info-name="ArchiveTV">']
    for ch in channels:
        out.append(f"  <channel id={quoteattr(ch.tvg_id)}>")
        out.append(f"    <display-name>{escape(ch.name)}</display-name>")
        if ch.number is not None:
            out.append(f"    <display-name>{ch.number}</display-name>")
        logo = logos.url(ch, base_url)
        if logo:
            out.append(f"    <icon src={quoteattr(logo)} />")
        out.append("  </channel>")
    for ch in channels:
        start = now - (timedelta(hours=past_hours) if past_hours is not None else timedelta(days=history_days(ch)))
        entries = s.scalars(
            select(ScheduleEntry)
            .where(ScheduleEntry.channel_id == ch.id, ScheduleEntry.end > start, ScheduleEntry.start < end)
            .order_by(ScheduleEntry.start)
        ).all()
        # Short idents/fillers are folded into the preceding programme's slot
        # so the guide stays gap-free without listing every station ident.
        slots: list[list] = []
        for e in entries:
            short = e.kind in (EntryKind.IDENT, EntryKind.FILLER) and (e.end - e.start).total_seconds() < 120
            if short and slots:
                slots[-1][1] = e.end
            else:
                slots.append([e.start, e.end, e])
        for slot_start, slot_end, e in slots:
            out.append(
                f"  <programme start={quoteattr(_xmltv_time(slot_start))} stop={quoteattr(_xmltv_time(slot_end))} channel={quoteattr(ch.tvg_id)}>"
            )
            item = e.media_item
            title = item.series if item and item.series else e.title
            out.append(f'    <title lang="en">{escape(title)}</title>')
            if item and item.series and item.title != item.series:
                out.append(f'    <sub-title lang="en">{escape(item.title)}</sub-title>')
            desc = e.description or (item.description if item else None)
            if desc:
                out.append(f'    <desc lang="en">{escape(desc[:1500])}</desc>')
            if item and item.publication_date and item.publication_date[:4].isdigit():
                out.append(f"    <date>{item.publication_date[:4]}</date>")
            cats = list(item.categories or [])[:3] if item else []
            if e.playback_source is not None and e.playback_source.live:
                cats.insert(0, "Live")
            if e.block_name:
                cats.insert(0, e.block_name)
            for c in cats:
                out.append(f'    <category lang="en">{escape(str(c))}</category>')
            if item and item.thumbnail_url and item.artwork_in_epg:
                out.append(f"    <icon src={quoteattr(item.thumbnail_url)} />")
            if item and (item.season or item.episode):
                sn = (item.season - 1) if item.season else ""
                ep = (item.episode - 1) if item.episode else ""
                out.append(f'    <episode-num system="xmltv_ns">{sn}.{ep}.</episode-num>')
                if item.season and item.episode:
                    out.append(f'    <episode-num system="onscreen">S{item.season:02d}E{item.episode:02d}</episode-num>')
            out.append("  </programme>")
    out.append("</tv>")
    return "\n".join(out) + "\n"
