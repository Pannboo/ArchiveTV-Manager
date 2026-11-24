"""Public, read-only endpoints for viewers: the guide API and HLS output.

The guide API (``/api/public``) is what a web player or app needs to show
a TV guide and tune in. For each programme on air it also says how a browser
can play it straight from where it lives, the spelunkr way, without this
server relaying any video:

* an Internet Archive file: its URL and the offset to seek to
* a YouTube video: the official embed with a start time

Everyone who tunes in at the same moment gets the same offset, so browser
viewers and IPTV viewers watch the same thing at the same second.

HLS (``/hls/<channel>/index.m3u8``) is the relayed stream for players that
want HLS rather than MPEG-TS (phones, Apple TV, web players).

Both follow the feed token (``ATV_FEED_TOKEN``) like the M3U and XMLTV
feeds, and send ``Access-Control-Allow-Origin: *`` so a player on another
site can read them.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import logos
from .config import settings
from .db import session_scope, utcnow
from .enums import GENERATED_KINDS, EntryKind, PlaybackType
from .hls import READY_TIMEOUT, hls_manager
from .models import Channel, ScheduleEntry
from .scheduler import to_local
from .stream import StreamLimitError, manager

router = APIRouter(tags=["public"])

CORS = {"Access-Control-Allow-Origin": "*"}
MAX_GUIDE_HOURS = 48
# Containers browsers play natively (an Archive file in any other format is
# left to the relayed stream).
BROWSER_CONTAINERS = {"mp4", "m4v", "webm", "ogv"}
ARCHIVE_HOSTS = ("archive.org",)


def _token_ok(request: Request) -> None:
    if settings.feed_token and not secrets.compare_digest(request.query_params.get("token", ""), settings.feed_token):
        raise HTTPException(403, "invalid feed token")


def _guard(request: Request) -> None:
    if not settings.public_guide:
        raise HTTPException(404, "the public guide is switched off")
    _token_ok(request)


def _base(request: Request) -> str:
    return settings.public_base_url or str(request.base_url).rstrip("/")


def _tq() -> str:
    return f"?token={quote(settings.feed_token)}" if settings.feed_token else ""


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def _json(data, max_age: int = 15) -> JSONResponse:
    return JSONResponse(data, headers={**CORS, "Cache-Control": f"public, max-age={max_age}"})


# ------------------------------------------------------------------ helpers


def _channels(s: Session) -> list[Channel]:
    return list(s.scalars(select(Channel).where(Channel.enabled.is_(True)).order_by(Channel.number, Channel.name)).all())


def _channel(s: Session, slug: str) -> Channel:
    ch = s.scalar(select(Channel).where(Channel.slug == slug, Channel.enabled.is_(True)))
    if ch is None:
        raise HTTPException(404, "unknown channel", headers=CORS)
    return ch


def _year(item) -> str | None:
    d = (item.publication_date or "") if item else ""
    return d[:4] if d[:4].isdigit() else None


def _host_is(url: str | None, suffixes: tuple[str, ...]) -> bool:
    host = (urlsplit(url or "").hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _browser_playable(ps) -> bool:
    """A file browsers play natively: judged by its extension, or by the
    container FFprobe reported (a list such as "mov,mp4,m4a,...")."""
    path = urlsplit(ps.url or "").path.lower()
    ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
    names = {c.strip() for c in (ps.container or "").lower().split(",") if c.strip()}
    return ext in BROWSER_CONTAINERS or bool(names & BROWSER_CONTAINERS)


def direct_playback(e: ScheduleEntry, at: datetime) -> dict | None:
    """How a browser can play this entry itself at time ``at``, or None when
    only the relayed stream can (a local file, an unusual format)."""
    if e.kind in GENERATED_KINDS:
        return {"type": "card", "title": e.title}
    item = e.media_item
    if item is None:
        return None
    offset = max(0.0, (at - e.start).total_seconds())
    if item.source is not None and item.source.provider == "youtube":
        vid = item.source.external_id
        embeddable = any(p.playback_type == PlaybackType.YOUTUBE_EMBED for p in item.playback_sources)
        if not embeddable or vid.startswith("live:"):
            return None
        start = int(offset)
        return {"type": "youtube", "video_id": vid, "start": start,
                "embed_url": f"https://www.youtube-nocookie.com/embed/{quote(vid)}?start={start}&autoplay=1&playsinline=1"}
    ps = e.playback_source
    if ps is None or ps.playback_type != PlaybackType.DIRECT_URL or ps.live:
        return None
    if not _host_is(ps.url, ARCHIVE_HOSTS) or not _browser_playable(ps):
        return None
    return {"type": "file", "url": ps.url, "offset": round(offset, 2), "container": ps.container,
            "width": ps.width, "height": ps.height}


def programme_out(e: ScheduleEntry, detail: bool = False) -> dict:
    item = e.media_item
    d = {
        "start": _iso(e.start), "end": _iso(e.end), "start_local": to_local(e.start).isoformat(),
        "kind": e.kind, "title": (item.series if item and item.series else e.title),
        "subtitle": item.title if item and item.series and item.title != item.series else None,
        "year": _year(item), "block": e.block_name,
    }
    if item is not None:
        src = item.source
        d["provider"] = src.provider if src else None
        d["page_url"] = src.original_url if src else None  # the tape's own page ("via archive.org")
        d["creator"] = src.creator if src else None
        if item.thumbnail_url and item.artwork_in_epg:
            d["thumbnail_url"] = item.thumbnail_url
        if detail:
            d["description"] = (item.description or "")[:1200] or None
    return d


def fold_breaks(entries: list[ScheduleEntry]) -> list[tuple[datetime, datetime, ScheduleEntry]]:
    """Guide slots: short breaks, idents and fillers fold into the slot before."""
    slots: list[list] = []
    for e in entries:
        short = e.kind in (EntryKind.IDENT, EntryKind.FILLER, EntryKind.BREAK) and (e.end - e.start).total_seconds() < 120
        if short and slots:
            slots[-1][1] = e.end
        else:
            slots.append([e.start, e.end, e])
    return [(a, b, e) for a, b, e in slots]


def _streams(base: str, ch: Channel) -> dict:
    return {"ts": f"{base}/live/{quote(ch.slug)}.ts{_tq()}", "hls": f"{base}/hls/{quote(ch.slug)}/index.m3u8{_tq()}"}


def channel_out(s: Session, ch: Channel, base: str, now: datetime) -> dict:
    rows = s.scalars(
        select(ScheduleEntry).where(ScheduleEntry.channel_id == ch.id, ScheduleEntry.end > now)
        .order_by(ScheduleEntry.start).limit(8)
    ).all()
    slots = fold_breaks(list(rows))
    now_slot = next((x for x in slots if x[0] <= now < x[1]), None)
    nxt = next((x for x in slots if x[0] > now), None)

    def slot(x):
        if x is None:
            return None
        d = programme_out(x[2])
        d["start"], d["end"] = _iso(x[0]), _iso(x[1])
        return d

    return {
        "slug": ch.slug, "number": ch.number, "name": ch.name, "description": ch.description,
        "group": ch.group_title, "logo_url": logos.url(ch, base),
        "now": slot(now_slot), "next": slot(nxt), "streams": _streams(base, ch),
    }


# ------------------------------------------------------------- guide API


@router.get("/api/public/channels")
def public_channels(request: Request):
    """Every channel with what is on now and next."""
    _guard(request)
    now, base = utcnow(), _base(request)
    with session_scope() as s:
        return _json({"server_time": _iso(now), "channels": [channel_out(s, ch, base, now) for ch in _channels(s)]})


@router.get("/api/public/guide")
def public_guide(request: Request, hours: float = Query(6, gt=0, le=MAX_GUIDE_HOURS),
                 start: datetime | None = Query(None, alias="from")):
    """The guide grid: every channel's programmes from ``from`` (default now)
    for ``hours``. Breaks fold into the programme before them."""
    _guard(request)
    now = utcnow()
    frm = (start.replace(tzinfo=None) if start else now) - timedelta(minutes=1)
    to = frm + timedelta(hours=hours)
    base = _base(request)
    with session_scope() as s:
        out = []
        for ch in _channels(s):
            rows = s.scalars(
                select(ScheduleEntry).where(ScheduleEntry.channel_id == ch.id, ScheduleEntry.end > frm, ScheduleEntry.start < to)
                .order_by(ScheduleEntry.start)
            ).all()
            progs = []
            for a, b, e in fold_breaks(list(rows)):
                d = programme_out(e)
                d["start"], d["end"] = _iso(a), _iso(b)
                progs.append(d)
            out.append({"slug": ch.slug, "number": ch.number, "name": ch.name, "logo_url": logos.url(ch, base),
                        "programmes": progs})
        return _json({"server_time": _iso(now), "from": _iso(frm), "to": _iso(to), "timezone": str(settings.timezone),
                      "channels": out}, max_age=60)


@router.get("/api/public/channels/{slug}/now")
def public_now(slug: str, request: Request):
    """Tune in: the entry on air this second, how far into it we are, how to
    play it directly, and what follows. ``playback`` is null when only the
    relayed stream (``streams``) can play it."""
    _guard(request)
    now, base = utcnow(), _base(request)
    with session_scope() as s:
        ch = _channel(s, slug)
        rows = s.scalars(
            select(ScheduleEntry).where(ScheduleEntry.channel_id == ch.id, ScheduleEntry.end > now)
            .order_by(ScheduleEntry.start).limit(6)
        ).all()
        cur = next((e for e in rows if e.start <= now), None)
        body = {
            "server_time": _iso(now),
            "channel": {"slug": ch.slug, "number": ch.number, "name": ch.name, "logo_url": logos.url(ch, base)},
            "streams": _streams(base, ch),
            "on_air": None,
            "up_next": [programme_out(e) for e in rows if e.start > now and e.kind not in GENERATED_KINDS][:3],
        }
        if cur is not None:
            d = programme_out(cur, detail=True)
            d["offset"] = round((now - cur.start).total_seconds(), 2)
            d["remaining"] = round((cur.end - now).total_seconds(), 2)
            d["playback"] = direct_playback(cur, now)
            body["on_air"] = d
        return _json(body, max_age=2)


@router.options("/api/public/{path:path}")
@router.options("/hls/{path:path}")
def public_preflight(path: str):
    return Response(status_code=204, headers={**CORS, "Access-Control-Allow-Methods": "GET, OPTIONS",
                                              "Access-Control-Allow-Headers": "*", "Access-Control-Max-Age": "86400"})


# ------------------------------------------------------------------- HLS


def _segment_uri(slug: str):
    def uri(seq: int) -> str:
        # Root-relative, so it also works when the playlist was reached
        # through the Xtream Codes /live/<user>/<pass>/<id>.m3u8 route.
        return f"/hls/{quote(slug)}/{seq}.ts{_tq()}"
    return uri


async def hls_playlist(slug: str) -> Response:
    with session_scope() as s:
        ch = s.scalar(select(Channel).where(Channel.slug == slug))
        if ch is None or not ch.enabled:
            raise HTTPException(404, "unknown channel")
        cid = ch.id
        if s.scalar(select(ScheduleEntry.id).where(ScheduleEntry.channel_id == cid, ScheduleEntry.end > utcnow()).limit(1)) is None:
            from .scheduler import maintain

            maintain(s, ch)
    try:
        hc = await hls_manager.get(manager, slug, cid)
    except StreamLimitError as e:
        raise HTTPException(503, str(e))
    if not hc.segments:
        try:
            await asyncio.wait_for(hc.ready.wait(), READY_TIMEOUT)
        except asyncio.TimeoutError:
            pass  # an empty live playlist is valid: the player asks again
    return Response(hc.playlist(_segment_uri(slug)), media_type="application/vnd.apple.mpegurl",
                    headers={**CORS, "Cache-Control": "no-cache"})


@router.get("/hls/{slug}/index.m3u8")
async def hls_index(slug: str, request: Request):
    _token_ok(request)
    return await hls_playlist(slug)


@router.get("/hls/{slug}/{seq}.ts")
def hls_segment(slug: str, seq: int, request: Request):
    _token_ok(request)
    hc = hls_manager.existing(slug)
    sg = hc.segment(seq) if hc is not None else None
    if sg is None:
        raise HTTPException(404, "segment not available", headers=CORS)
    return Response(sg.data, media_type="video/mp2t", headers={**CORS, "Cache-Control": "public, max-age=120"})
