"""Channel looks: the colours, type and finish of a channel's break cards and
generated logo.

Every look shares one layout (see cards.py): a panel listing what's on next,
and leaning bars down the right with "UP NEXT". A look changes everything
else, so a horror channel's breaks feel like a late-night creature feature
and a newsreel channel's like a 1940s cinema reel.

A channel's look is its ``card_theme``; "auto" (the default) takes the look
its channel pack asks for, else one picked from the channel's name and
description, else "broadcast".
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

SANS = "DejaVu Sans Condensed:bold"
SANS_WIDE = "DejaVu Sans:bold"
SERIF = "DejaVu Serif Condensed:bold"

# Per-channel accents for looks that don't fix their own.
ACCENTS = ["0x1f8a8a", "0xd9822b", "0x7a3fb0", "0xc0392b", "0x2e86de", "0x3c9d4e", "0xb8860b", "0xd6336c"]


@dataclass(frozen=True)
class Theme:
    key: str
    label: str
    panel_top: str
    panel_bottom: str
    ink: str  # titles and times
    ink_dim: str  # the furthest-off time, the footer
    box_bg: str  # time box fill
    box_edge: str  # time box and thumbnail edge
    bar: str  # the bar "UP NEXT" runs up
    headline: str  # "UP NEXT" colour, contrasting with ``bar``
    accent: str | None  # middle bar; None = per channel
    grey_top: str  # outer bar, top
    grey_bottom: str  # outer bar, bottom
    font: str = SANS
    grain: int = 3  # temporal noise strength (0 = clean)
    vignette: bool = False
    scanlines: bool = False
    post: str = ""  # extra FFmpeg filters applied last (e.g. monochrome)
    bumper: bool = False  # "auto" break style: animated bumper rather than the lineup card


THEMES: dict[str, Theme] = {t.key: t for t in [
    Theme("broadcast", "Broadcast (light, clean)", "0xf1f1f1", "0xd2d2d2", "0x151515", "0x8c8c8c", "0xfafafa", "0x9a9a9a",
          "0x111111", "0xffffff", None, "0x969696", "0x6e6e6e"),
    Theme("creature", "Creature Feature (horror, sci-fi)", "0x1d1528", "0x07050b", "0xd8f5c9", "0x6f8a66", "0x241b30", "0x5b8c3a",
          "0x050505", "0xd8f5c9", "0x7cdf3c", "0x4b2a6b", "0x1e0f30", grain=8, vignette=True, bumper=True),
    Theme("toon", "Toon Town (cartoons)", "0xffd23f", "0xffb703", "0x1d1a4b", "0x6b5f9a", "0xffffff", "0x1d1a4b",
          "0x1d1a4b", "0xffffff", "0xff3d7f", "0x00b4d8", "0x0077b6", font=SANS_WIDE, grain=0, bumper=True),
    Theme("noir", "Noir (monochrome, high contrast)", "0x2b2b2b", "0x0b0b0b", "0xf2f2f2", "0x8a8a8a", "0x141414", "0xbdbdbd",
          "0xe8e8e8", "0x0b0b0b", "0x6e6e6e", "0x3a3a3a", "0x1a1a1a", grain=6, vignette=True, post="hue=s=0"),
    Theme("frontier", "Frontier (westerns, sepia)", "0xefe0c2", "0xd6bf91", "0x3b2412", "0x8a6a48", "0xf7ecd6", "0x7a5230",
          "0x3b2412", "0xf7ecd6", "0xb5541c", "0xa77b4a", "0x7a5230", font=SERIF, grain=6, vignette=True),
    Theme("newsreel", "Newsreel (cinema reel)", "0xdedede", "0x9e9e9e", "0x111111", "0x5a5a5a", "0xf0f0f0", "0x444444",
          "0x111111", "0xf0f0f0", "0x555555", "0x9a9a9a", "0x6a6a6a", font=SERIF, grain=12, vignette=True,
          post="hue=s=0,eq=brightness=0.025*sin(2*PI*t*7):eval=frame"),
    Theme("classroom", "Classroom (educational films)", "0xe6f2e6", "0xbcd9c4", "0x1f3b2c", "0x6f8a7d", "0xfbfdf8", "0x4f7a61",
          "0x1f3b2c", "0xfbfdf8", "0xe07a2f", "0xf2e2b6", "0xd9c48f", font=SANS_WIDE, grain=4),
    Theme("vhs", "VHS (commercials, home tapes)", "0x1b1140", "0x0a0620", "0xf4f1ff", "0x8a80b8", "0x2a1f5c", "0xff4fd8",
          "0xff4fd8", "0x1b1140", "0x25e6ff", "0x7b2cbf", "0x3c096c", grain=7, scanlines=True, bumper=True),
    Theme("concert", "Concert (music)", "0x101935", "0x04060f", "0xffffff", "0x7b86b0", "0x1a2550", "0xff2e93",
          "0xff2e93", "0xffffff", "0xffd166", "0x3a0ca3", "0x240046", grain=4, vignette=True, bumper=True),
]}
DEFAULT = "broadcast"

# Picking a look from a channel's name and description ("auto").
_KEYWORDS: list[tuple[str, str]] = [
    ("creature", r"horror|monster|creature|sci-?fi|science fiction|zombie|vampire|b-movie|late night"),
    ("noir", r"noir|crime|detective|gangster|mystery"),
    ("frontier", r"western|cowboy|frontier|outlaw"),
    ("newsreel", r"newsreel|news|war|history|documentar"),
    ("classroom", r"classroom|educational|prelinger|industrial|guidance|school"),
    ("toon", r"cartoon|toon|animation|animated|saturday morning|kids|children"),
    ("concert", r"music|concert|live session|band"),
    ("vhs", r"commercial|advert|vhs|sitcom|tape|tv|television"),
]


def resolve(channel, pack_look: str | None = None) -> Theme:
    """The look a channel uses: its own choice, its pack's, or a guess."""
    key = (getattr(channel, "card_theme", None) or "auto").lower()
    if key in THEMES:
        return THEMES[key]
    if pack_look and pack_look in THEMES:
        return THEMES[pack_look]
    text = f"{getattr(channel, 'name', '')} {getattr(channel, 'description', '') or ''}".lower()
    for look, pattern in _KEYWORDS:
        if re.search(pattern, text):
            return THEMES[look]
    return THEMES[DEFAULT]


def pack_spec(s, channel) -> dict | None:
    """The spec of the pack the channel came from, if any."""
    from sqlalchemy import select

    from .models import ChannelPack

    if channel.id is None:
        return None
    return s.scalar(select(ChannelPack.spec).where(ChannelPack.channel_id == channel.id).limit(1))


def pack_look(s, channel) -> str | None:
    """The look named by the channel's pack, if it came from one."""
    return (pack_spec(s, channel) or {}).get("look")


def for_channel(s, channel) -> Theme:
    return resolve(channel, pack_look(s, channel))


def accent(theme: Theme, channel) -> str:
    if theme.accent:
        return theme.accent
    h = int(hashlib.sha1((getattr(channel, "slug", None) or str(getattr(channel, "id", ""))).encode()).hexdigest(), 16)
    return ACCENTS[h % len(ACCENTS)]


def choices() -> list[dict]:
    return [{"key": "auto", "label": "Automatic (pack's look, else from the channel name)"}] + [
        {"key": t.key, "label": t.label} for t in THEMES.values()]


BREAK_STYLES = ("auto", "lineup", "bumper")


def break_style(s, channel, look: Theme | None = None) -> str:
    """Channel's choice, else its pack's, else the look's default."""
    style = (getattr(channel, "break_style", None) or "auto").lower()
    if style in ("lineup", "bumper"):
        return style
    spec_style = (pack_spec(s, channel) or {}).get("break_style")
    if spec_style in ("lineup", "bumper"):
        return spec_style
    look = look or for_channel(s, channel)
    return "bumper" if look.bumper else "lineup"
