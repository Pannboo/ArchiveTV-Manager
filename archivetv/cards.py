"""Generated break cards: the short "UP NEXT" cards aired between programmes.

A card is drawn live by FFmpeg (lavfi sources and filters); nothing is
recorded. The design is a broadcast continuity card:

* a light panel listing the next three programmes, each with its start time
  in a box, its thumbnail and its title in bold condensed capitals
* diagonal bars down the right-hand side (black, the channel's accent colour,
  grey) with "UP NEXT" running up the black bar
* a footer with the channel, where the tapes come from ("via archive.org")
  and the music credit

The accent colour belongs to the channel (derived from its slug), so every
break on a channel looks like the same station. Rows fade in one after the
other; a viewer joining mid-break sees the card as it stands at that moment.

Thumbnails are the programmes' own artwork (Internet Archive or YouTube),
fetched over HTTPS from those hosts only, held in a small RAM cache, and
handed to FFmpeg from a private temp directory that is removed when the
card ends. A missing thumbnail becomes a plain tile; it never fails a card.

Music is a Creative Commons / public-domain track from the music folder,
credited on screen, or, when there is none, a soft chord generated on the
spot, which needs no licence.
"""

from __future__ import annotations

import asyncio
import logging
import random
import shutil
import tempfile
import textwrap
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import themes
from .config import settings
from .enums import EntryKind
from .models import Channel, ScheduleEntry
from .themes import Theme

log = logging.getLogger(__name__)

WIDTH, HEIGHT, FPS = 1280, 720, 25
MUSIC_EXTS = {".mp3", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac", ".wav"}
DEFAULT_FONT = themes.SANS  # fonts-dejavu-extra; fontconfig falls back to a bold sans
ACCENTS = themes.ACCENTS

# Layout (1280×720). Bars lean: a boundary at x=a on the top row is at
# a - SLANT*y further down.
SLANT = 0.1028
BAR_BLACK, BAR_ACCENT, BAR_GREY = 946, 1113, 1205
ROW_Y = (56, 262, 468)
ROW_H = 176
TIME_X, TIME_W = 92, 104
THUMB_X, THUMB_W = 200, 270
TITLE_X = 500
TITLE_CHARS = 13  # per line: stays clear of the black bar even in a wide bold face

# Soft chords (Hz) for the generated music bed.
CHORDS: list[tuple[float, float, float]] = [
    (130.81, 164.81, 196.00), (146.83, 174.61, 220.00), (110.00, 138.59, 164.81),
    (98.00, 123.47, 146.83), (116.54, 146.83, 174.61), (87.31, 110.00, 130.81),
]
THUMB_HOSTS = ("archive.org", "ytimg.com", "youtube.com")
THUMB_MAX_BYTES = 1_500_000
THUMB_CACHE = 64


@dataclass
class MusicTrack:
    path: Path
    credit: str  # shown on screen, e.g. "Song — Artist (CC BY 4.0)"


@dataclass
class LineupRow:
    time: str  # "6.30"
    ampm: str  # "PM" (empty for a 24-hour clock)
    title: str
    thumb_url: str | None = None


@dataclass
class ClipSource:
    """A few seconds of the next programme, shown inside an animated bumper."""

    url: str  # http(s) URL or local file path
    offset: float  # where in the programme the preview starts


@dataclass
class CardSpec:
    seed: int  # per break: picks the music
    channel: str
    number: int | None
    headline: str  # "UP NEXT"
    rows: list[LineupRow] = field(default_factory=list)
    credit: str = ""  # where the tapes come from, e.g. "via archive.org"
    music: MusicTrack | None = None
    accent: str = ACCENTS[0]
    theme: Theme = themes.THEMES[themes.DEFAULT]
    thumbs: dict[str, bytes] = field(default_factory=dict)  # url -> image, filled by prepare()
    style: str = "lineup"  # "lineup" (static UP NEXT card) or "bumper" (animated, bumpers.py)
    clip: ClipSource | None = None  # bumper preview of the next programme
    logo_path: str | None = None  # the channel's logo file, for the bumper's corner bug


# ------------------------------------------------------------------ music


def music_library() -> list[MusicTrack]:
    """Tracks in the music folder, sorted by name. A sidecar ``<file>.txt``
    holds the on-screen credit (first line); without one the file name is
    used. Only put Creative Commons or public-domain music here."""
    try:
        files = sorted(p for p in settings.music_dir.iterdir() if p.is_file() and p.suffix.lower() in MUSIC_EXTS)
    except OSError:
        return []
    out = []
    for p in files:
        credit = p.stem.replace("_", " ")
        side = p.with_name(p.name + ".txt")
        if not side.exists():
            side = p.with_suffix(".txt")
        try:
            credit = side.read_text(encoding="utf-8").strip().splitlines()[0].strip() or credit
        except (OSError, IndexError):
            pass
        out.append(MusicTrack(p, credit[:120]))
    return out


def pick_music(seed: int) -> MusicTrack | None:
    lib = music_library()
    return lib[seed % len(lib)] if lib else None


# --------------------------------------------------------------- content


def channel_accent(ch: Channel, theme: Theme | None = None) -> str:
    return themes.accent(theme or themes.THEMES[themes.DEFAULT], ch)


def _clock(dt) -> tuple[str, str]:
    h = dt.hour % 12 or 12
    return f"{h}.{dt.minute:02d}", "AM" if dt.hour < 12 else "PM"


def _credit(items) -> str:
    providers = {i.source.provider for i in items if i is not None and i.source is not None}
    parts = []
    if "archive" in providers:
        parts.append("via archive.org")
    if "youtube" in providers:
        parts.append("via YouTube")
    return " · ".join(parts)


def _title(e: ScheduleEntry) -> str:
    item = e.media_item
    if item and item.series:
        return f"{item.series}: {item.title}" if item.title != item.series else item.series
    return e.title


def break_card(s: Session, ch: Channel, e: ScheduleEntry) -> CardSpec:
    """Card for a BREAK entry: the next three programmes, read from the
    schedule now (so a rebuilt schedule never shows a stale lineup)."""
    from .scheduler import to_local

    upcoming = s.scalars(
        select(ScheduleEntry)
        .where(ScheduleEntry.channel_id == ch.id, ScheduleEntry.start >= e.end,
               ScheduleEntry.kind.not_in([EntryKind.BREAK, EntryKind.IDENT, EntryKind.FILLER]))
        .order_by(ScheduleEntry.start)
        .limit(3)
    ).all()
    rows, items = [], []
    for nxt in upcoming:
        if nxt.kind == EntryKind.SLATE:
            break
        item = nxt.media_item
        items.append(item)
        t, ap = _clock(to_local(nxt.start))
        thumb = item.thumbnail_url if item is not None and item.artwork_in_epg else None
        if item is not None and item.source is not None and item.source.provider == "youtube" and not thumb:
            vid = item.source.external_id
            thumb = None if vid.startswith("live:") else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        rows.append(LineupRow(t, ap, _title(nxt), thumb))
    seed = e.id or 0
    look = themes.for_channel(s, ch)
    card = CardSpec(seed, ch.name, ch.number, "UP NEXT", rows, _credit(items), pick_music(seed),
                    themes.accent(look, ch), look, style=themes.break_style(s, ch, look))
    if card.style == "bumper":
        from . import logos

        if ch.logo_file and (p := logos.path_for(ch.logo_file)) is not None:
            card.logo_path = str(p)
        if upcoming and upcoming[0].kind != EntryKind.SLATE:
            card.clip = preview_clip(upcoming[0])
    return card


def preview_clip(e: ScheduleEntry) -> ClipSource | None:
    """A point a quarter of the way into the programme, from the source it
    will air from. Live and YouTube-stream sources are skipped (no seek, or
    an address that must be resolved first)."""
    from .catalogue import source_is_iptv_ok
    from .enums import PlaybackType

    ps = e.playback_source
    if ps is None or ps.live or not source_is_iptv_ok(ps) or ps.seekable is False:
        return None
    if ps.playback_type == PlaybackType.DIRECT_URL and ps.url:
        target = ps.url
    elif ps.playback_type == PlaybackType.LOCAL_FILE and ps.local_path:
        from .security import UnsafeURLError, safe_local_path

        try:
            target = str(safe_local_path(ps.local_path))  # stored relative to the media folder
        except (UnsafeURLError, ValueError, OSError):
            return None
    else:
        return None
    dur = (e.end - e.start).total_seconds()
    off = dur * 0.25 if dur < 240 else min(max(60.0, dur * 0.25), dur - 60)
    return ClipSource(target, round(off, 1))


# ------------------------------------------------------------ thumbnails

_thumb_cache: "OrderedDict[str, bytes | None]" = OrderedDict()


def _thumb_host_ok(url: str) -> bool:
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host == h or host.endswith("." + h) for h in THUMB_HOSTS)


async def _fetch_thumb(client: httpx.AsyncClient, url: str) -> bytes | None:
    if url in _thumb_cache:
        _thumb_cache.move_to_end(url)
        return _thumb_cache[url]
    data = None
    try:
        current = url
        for _ in range(4):  # archive.org's image service redirects to a mirror
            if not _thumb_host_ok(current):
                break
            r = await client.get(current)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                current = str(r.url.join(r.headers["location"]))
                continue
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/") and len(r.content) <= THUMB_MAX_BYTES:
                data = r.content
            break
    except httpx.HTTPError:
        data = None
    _thumb_cache[url] = data
    while len(_thumb_cache) > THUMB_CACHE:
        _thumb_cache.popitem(last=False)
    return data


async def prepare(card: CardSpec, timeout: float = 4.0) -> None:
    """Fetch the lineup's thumbnails (best effort, a few seconds at most)."""
    urls = [r.thumb_url for r in card.rows if r.thumb_url and _thumb_host_ok(r.thumb_url)]
    if not urls:
        return
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers={"User-Agent": settings.http_user_agent}) as c:
        try:
            got = await asyncio.wait_for(asyncio.gather(*(_fetch_thumb(c, u) for u in urls)), timeout + 1)
        except asyncio.TimeoutError:
            return
    for u, data in zip(urls, got):
        if data:
            card.thumbs[u] = data


# ----------------------------------------------------------------- render


def _clean(text: str) -> str:
    # drawtext draws control characters as boxes; keep printable text only.
    return "".join(c for c in (text or "") if c.isprintable()).strip()


def _wrap(text: str, width: int, lines: int) -> str:
    out = textwrap.wrap(_clean(text), width=width, break_on_hyphens=False) or [""]
    if len(out) > lines:
        out = out[:lines]
        out[-1] = out[-1][: max(0, width - 1)].rstrip() + "…"
    return "\n".join(out)


# Bold faces tried when fontconfig can't be asked (first that exists wins).
SERIF_FALLBACKS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSerifCondensed-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
)
FONT_FALLBACKS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSansCondensed-Bold.ttf",
)
_font_cache: dict[str, str | None] = {}


def font_file(pattern: str) -> str | None:
    """Resolve a fontconfig pattern to a file. drawtext's own ``font=``
    ignores the style part (":bold"), so cards always pass a file."""
    if pattern in _font_cache:
        return _font_cache[pattern]
    found = None
    if "/" in pattern and Path(pattern).is_file():
        found = pattern
    else:
        try:
            import subprocess

            out = subprocess.run(["fc-match", "-f", "%{file}", pattern], capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip() and Path(out.stdout.strip()).is_file():
                found = out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        if found is None:
            options = (SERIF_FALLBACKS if "serif" in pattern.lower() and "sans" not in pattern.lower() else ()) + FONT_FALLBACKS
            found = next((f for f in options if Path(f).is_file()), None)
    _font_cache[pattern] = found
    return found


def _font(theme_font: str | None = None) -> str:
    """drawtext font option: ATV_CARD_FONT if set (a font file or fontconfig
    pattern), else the look's face."""
    pattern = settings.card_font or theme_font or DEFAULT_FONT
    f = font_file(pattern)
    if f:
        return "fontfile='" + _esc_path(f) + "'"
    return "font='" + _esc_path(pattern.split(":")[0].replace("'", "")) + "'"


def _esc_path(p: str) -> str:
    return p.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


@dataclass
class CardRender:
    inputs: list[str]  # FFmpeg input arguments
    filter_complex: str  # graph producing [v] and [a]
    workdir: Path  # text files and thumbnails; remove with cleanup()

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


def _rgb(hexcol: str) -> tuple[int, int, int]:
    v = int(hexcol, 16)
    return (v >> 16) & 255, (v >> 8) & 255, v & 255


def bars_layer(theme: Theme, accent: str, w: int, h: int, edges: tuple[float, float, float], slant: float,
               loop: bool = True) -> str:
    """A single-frame RGBA layer with the look's three leaning bars (the
    main bar, the accent, then a vertical gradient), optionally looped."""
    (br, bg_, bb), (ar, ag, ab) = _rgb(theme.bar), _rgb(accent)
    (gr1, gg1, gb1), (gr2, gg2, gb2) = _rgb(theme.grey_top), _rgb(theme.grey_bottom)
    e1, e2, e3 = edges
    u = f"(X+{slant}*Y)"
    f1, f2, f3 = f"clip({u}-{e1},0,1)", f"clip({u}-{e2},0,1)", f"clip({u}-{e3},0,1)"

    def chan(bar: int, acc: int, g1: int, g2: int) -> str:
        return f"'{bar}*(1-{f2})+{acc}*({f2}-{f3})+({g1}+({g2}-{g1})*Y/H)*{f3}'"

    out = (f"color=c=black@0.0:s={w}x{h}:r={FPS}:d=0.04,format=rgba,"
           f"geq=r={chan(br, ar, gr1, gr2)}:g={chan(bg_, ag, gg1, gg2)}:b={chan(bb, ab, gb1, gb2)}:a='255*{f1}'")
    return out + (f",loop=loop=-1:size=1,setpts=N/{FPS}/TB" if loop else "")


def _finish(theme: Theme, moving: bool = True) -> list[str]:
    """The look's surface: scanlines, grain, vignette, colour treatment.
    Still images (logos) keep the scanlines and colour, not grain or vignette."""
    out = []
    if theme.scanlines:
        out.append("drawgrid=w=iw:h=4:t=1:c=black@0.22")
    if moving and theme.grain:
        out.append(f"noise=alls={theme.grain}:allf=t")
    if moving and theme.vignette:
        out.append("vignette=PI/4.5")
    if theme.post:
        out.append(theme.post)
    return out


def render(card: CardSpec, duration: float, offset: float = 0.0, *, drawtext: bool = True,
           gradients: bool = True, rate: list[str] | None = None) -> CardRender:
    """FFmpeg inputs and filtergraph for a card ``duration`` seconds long,
    starting ``offset`` seconds into it (a viewer joining mid-break)."""
    if card.style == "bumper":
        from .bumpers import render_bumper

        return render_bumper(card, duration, offset, drawtext=drawtext, gradients=gradients, rate=rate)
    th = card.theme
    rng = random.Random(f"card:{card.seed}")
    rate = rate or []
    d = max(0.1, duration)
    full = d + offset
    work = Path(tempfile.mkdtemp(prefix="atv-card-"))
    font = _font(th.font)
    o = f"{offset:.3f}"

    def text(name: str, content: str, opts: str, appear: float = 0.0) -> str:
        p = work / f"{name}.txt"
        p.write_text(content, encoding="utf-8")
        alpha = f"alpha='if(lt(t+{o},{appear:.2f}),0,if(lt(t+{o},{appear + 0.4:.2f}),(t+{o}-{appear:.2f})/0.4,1))'"
        return f"drawtext=textfile='{_esc_path(str(p))}':expansion=none:{font}:{opts}:{alpha}"

    # -- inputs: 0 background, 1 audio, 2.. thumbnails
    if gradients:
        bg = (f"gradients=s={WIDTH}x{HEIGHT}:r={FPS}:c0={th.panel_top}:c1={th.panel_bottom}:nb_colors=2:"
              f"x0=0:y0=0:x1={WIDTH}:y1={HEIGHT}:speed=0.0001:d={d:.3f}")
    else:
        bg = f"color=c={th.panel_bottom}:s={WIDTH}x{HEIGHT}:r={FPS}:d={d:.3f}"
    inputs = rate + ["-f", "lavfi", "-i", bg]
    if card.music is not None:
        inputs += ["-stream_loop", "-1", "-ss", o] + rate + ["-i", str(card.music.path)]
        apre = "[1:a]aresample=48000,aformat=channel_layouts=stereo,volume=0.8"
    else:
        f1, f2, f3 = rng.choice(CHORDS)
        t = f"(t+{o})"
        swell = f"(0.65+0.35*sin(2*PI*0.2*{t}))"
        left = f"0.05*(sin(2*PI*{f1}*{t})+sin(2*PI*{f2}*{t})+0.7*sin(2*PI*{f3}*{t})+0.4*sin(2*PI*{f1 * 2}*{t}))*{swell}"
        right = f"0.05*(sin(2*PI*{f1 * 1.003:.3f}*{t})+sin(2*PI*{f2 * 0.998:.3f}*{t})+0.7*sin(2*PI*{f3}*{t}))*{swell}"
        inputs += ["-f", "lavfi", "-i", f"aevalsrc=exprs={left}|{right}:s=48000:d={d:.3f}"]
        apre = "[1:a]aecho=0.8:0.6:180|330:0.25|0.15,lowpass=f=2400"
    afade = []
    if offset < 0.75:
        afade.append(f"afade=t=in:d={0.8 - offset:.2f}")
    afade.append(f"afade=t=out:st={max(0.0, full - 1.2 - offset):.2f}:d=1.2")
    graph = [f"{apre},{','.join(afade)},atrim=0:{d:.3f}[a]"]

    thumb_idx: dict[int, int] = {}
    n_in = 2
    for i, row in enumerate(card.rows[:3]):
        data = card.thumbs.get(row.thumb_url or "")
        if data:
            p = work / f"thumb{i}.img"
            p.write_bytes(data)
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", str(p)]
            thumb_idx[i] = n_in
            n_in += 1

    # -- picture
    graph.append(bars_layer(th, card.accent, WIDTH, HEIGHT, (BAR_BLACK, BAR_ACCENT, BAR_GREY), SLANT) + "[bars]")
    v = ["[0:v]format=yuv420p"]
    rows = card.rows[:3] or [LineupRow("", "", card.channel)]
    for i, row in enumerate(rows):
        y = ROW_Y[i]
        appear = 0.15 + 0.35 * i
        last = i == len(rows) - 1 and len(rows) == 3
        v.append(f"drawbox=x={TIME_X}:y={y}:w={TIME_W}:h={ROW_H}:color={th.box_bg}@1:t=fill")
        v.append(f"drawbox=x={TIME_X}:y={y}:w={TIME_W}:h={ROW_H}:color={th.box_edge}@1:t=2")
        v.append(f"drawbox=x={THUMB_X - 2}:y={y - 2}:w={THUMB_W + 4}:h={ROW_H + 4}:color={th.box_edge}@1:t=fill")
        if i not in thumb_idx:
            v.append(f"drawbox=x={THUMB_X}:y={y}:w={THUMB_W}:h={ROW_H}:color={th.box_bg}@1:t=fill")
            v.append(f"drawbox=x={THUMB_X}:y={y}:w={THUMB_W}:h={ROW_H}:color={card.accent}@0.35:t=fill")
        if drawtext:
            time_col = th.ink_dim if last else th.ink
            if row.time:
                v.append(text(f"time{i}", row.time, f"fontsize=40:fontcolor={time_col}:x={TIME_X}+({TIME_W}-tw)/2:y={y + 42}", appear))
            if row.ampm:
                v.append(text(f"ampm{i}", row.ampm, f"fontsize=40:fontcolor={time_col}:x={TIME_X}+({TIME_W}-tw)/2:y={y + 98}", appear))
            title = _wrap(row.title.upper(), TITLE_CHARS, 3)
            lines = title.count("\n") + 1
            size = 42 if lines <= 2 else 34
            ty = y + ROW_H // 2 - (lines * (size + 8)) // 2
            v.append(text(f"title{i}", title, f"fontsize={size}:fontcolor={th.ink}:line_spacing=8:x={TITLE_X}:y={ty}", appear + 0.1))
    if drawtext:
        foot = (f"{card.number}  " if card.number is not None else "") + card.channel.upper()
        if card.credit:
            foot += "   ·   " + card.credit
        v.append(text("foot", _wrap(foot, 70, 1), f"fontsize=22:fontcolor={th.ink_dim}:x={TIME_X}:y=674"))
        if card.music is not None:
            v.append(text("music", _wrap("♪ " + card.music.credit, 60, 1), f"fontsize=20:fontcolor={th.ink_dim}:x={TIME_X}:y=24"))
    graph.append(",".join(v) + "[panel]")
    cur = "panel"
    for i, idx in thumb_idx.items():
        appear = 0.15 + 0.35 * i
        y = ROW_Y[i]
        graph.append(f"[{idx}:v]scale={THUMB_W}:{ROW_H}:force_original_aspect_ratio=increase,crop={THUMB_W}:{ROW_H},"
                     f"format=yuva420p,fade=t=in:st={max(0.0, appear - offset):.2f}:d=0.4:alpha=1[th{i}]")
        graph.append(f"[{cur}][th{i}]overlay=x={THUMB_X}:y={y}:eof_action=pass[p{i}]")
        cur = f"p{i}"
    graph.append(f"[{cur}][bars]overlay=0:0:shortest=1[barred]")
    cur = "barred"
    if drawtext:
        angle = "-1.6734"  # 90° anticlockwise plus the bars' lean
        p = work / "headline.txt"
        p.write_text(_wrap(card.headline.upper(), 20, 1), encoding="utf-8")
        graph.append(
            f"color=c=black@0.0:s=640x150:r={FPS}:d=0.04,format=rgba,"
            f"drawtext=textfile='{_esc_path(str(p))}':expansion=none:{font}:fontsize=118:fontcolor={th.headline}:x=(w-tw)/2:y=(h-th)/2,"
            f"rotate=a={angle}:c=none:ow=rotw({angle}):oh=roth({angle}),loop=loop=-1:size=1,setpts=N/{FPS}/TB[head]"
        )
        cy = 400
        cx = (BAR_BLACK + BAR_ACCENT) / 2 - SLANT * cy
        graph.append(f"[{cur}][head]overlay=x={cx:.0f}-overlay_w/2:y={cy}-overlay_h/2:shortest=1[headed]")
        cur = "headed"
    finish = _finish(th) + [f"trim=0:{d:.3f}", "setpts=PTS-STARTPTS", "format=yuv420p"]
    graph.append(f"[{cur}]" + ",".join(finish) + "[v]")
    return CardRender(inputs, ";".join(graph), work)
