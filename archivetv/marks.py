"""Channel logo marks, drawn by FFmpeg in the channel's look.

Real station logos are marks, not cards: a strong shape, a short wordmark
(or monogram) and often the channel number. Each look has its own
construction, all original:

* broadcast: leaning slabs (the bars from the break cards) with the wordmark
* toon: a chunky rounded block with an offset shadow and tilted, outlined type
* creature: a glowing ring around a dark disc
* noir: a black plate with a fine inset frame and serif type
* frontier: a double-ringed badge with stars, serif type
* newsreel: a film countdown leader (crosshair and ring) round the number
* classroom: a rounded pill with a numbered dot
* vhs: sheared chrome type over 80s stripes
* concert: a record with grooves and a numbered label

Shapes are signed-distance masks evaluated once by ``geq`` on a transparent
512×512 canvas; the PNG keeps its alpha so it sits on any guide background.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .cards import _esc_path, _font, _rgb
from .config import settings
from .themes import Theme

S = 512
_STOP = {"the", "of", "and", "&", "a", "an", "channel", "tv", "network"}


def wordmark(name: str, override: str | None = None) -> str:
    """Short text for the mark: the pack's ``logo_text``, a short single
    word, else initials (``Classroom Films`` -> ``CF``)."""
    if override:
        return override.strip().upper()[:10]
    words = [w for w in re.split(r"[\s/&+-]+", name) if w]
    if len(words) == 1 and len(words[0]) <= 8:
        return words[0].upper()
    keep = [w for w in words if w.lower() not in _STOP] or words
    if len(keep) == 1 and len(keep[0]) <= 8:
        return keep[0].upper()
    return "".join(w[0] for w in keep[:3]).upper()


# ---------------------------------------------------------------- shapes


def _rrect(cx, cy, hw, hh, r):
    return (f"clip({r}-(hypot(max(abs(X-{cx})-{hw}+{r},0),max(abs(Y-{cy})-{hh}+{r},0))"
            f"+min(max(abs(X-{cx})-{hw},abs(Y-{cy})-{hh}),0)),0,1)")


def _circle(cx, cy, r):
    return f"clip({r}-hypot(X-{cx},Y-{cy}),0,1)"


def _ring(cx, cy, r, w):
    return f"clip({w / 2}-abs(hypot(X-{cx},Y-{cy})-{r}),0,1)"


def _diamond(cx, cy, r):
    return f"clip({r}-(abs(X-{cx})+abs(Y-{cy})),0,1)"


def _slab(x0, y0, w, h, skew):
    u = f"(X+{skew}*(Y-{y0 + h}))"
    return f"min(min(clip({u}-{x0},0,1),clip({x0 + w}-{u},0,1)),min(clip(Y-{y0},0,1),clip({y0 + h}-Y,0,1)))"


def _layer(color: str, mask: str, label: str) -> str:
    r, g, b = _rgb(color)
    return (f"color=c=black@0.0:s={S}x{S}:d=0.04,format=rgba,"
            f"geq=r={r}:g={g}:b={b}:a='255*({mask})'[{label}]")


class _Mark:
    """Builds a filtergraph: layers composited in order, then text."""

    def __init__(self, theme: Theme, work: Path):
        self.t, self.work = theme, work
        self.graph: list[str] = [f"color=c=black@0.0:s={S}x{S}:d=0.04,format=rgba[c0]"]
        self.cur, self.n = "c0", 0
        self.font = _font(theme.font)

    def shape(self, color: str, mask: str, blur: float = 0) -> "_Mark":
        self.n += 1
        lab = f"s{self.n}"
        layer = _layer(color, mask, lab)
        if blur:
            layer = layer.replace(f"[{lab}]", f",gblur=sigma={blur}[{lab}]")
        self.graph.append(layer)
        self._over(lab)
        return self

    def _over(self, lab: str, x: str = "0", y: str = "0") -> None:
        nxt = f"c{self.n}x"
        self.graph.append(f"[{self.cur}][{lab}]overlay={x}:{y}:format=auto[{nxt}]")
        self.cur = nxt

    def text(self, content: str, size: int, color: str, x: str, y: str, *, border: tuple[int, str] | None = None,
             shadow: tuple[int, int, str] | None = None, rotate: float = 0, shear: float = 0, font: str | None = None) -> "_Mark":
        self.n += 1
        p = self.work / f"t{self.n}.txt"
        p.write_text(content, encoding="utf-8")
        opts = [f"textfile='{_esc_path(str(p))}'", "expansion=none", font or self.font, f"fontsize={size}",
                f"fontcolor={color}", "x=(w-tw)/2", "y=(h-th)/2"]
        if border:
            opts += [f"borderw={border[0]}", f"bordercolor={border[1]}"]
        if shadow:
            opts += [f"shadowx={shadow[0]}", f"shadowy={shadow[1]}", f"shadowcolor={shadow[2]}"]
        lab = f"t{self.n}"
        chain = f"color=c=black@0.0:s={S}x{S}:d=0.04,format=rgba,drawtext=" + ":".join(opts)
        if shear:
            chain += f",shear=shx={shear}:fillcolor=none"
        if rotate:
            chain += f",rotate=a={rotate}:c=none"
        self.graph.append(chain + f"[{lab}]")
        self._over(lab, f"({x})-{S}/2", f"({y})-{S}/2")
        return self

    def build(self) -> str:
        return ";".join(self.graph) + f";[{self.cur}]format=rgba[v]"


def _fit(text: str, width: int, max_size: int, factor: float = 0.62) -> int:
    return int(max(28, min(max_size, width / max(1, len(text)) / factor)))


# ---------------------------------------------------------------- designs


def _broadcast(m: _Mark, word: str, number: int | None, accent: str) -> None:
    t = m.t
    m.shape("0x000000", _slab(70, 186, 380, 150, 0.2), blur=6)  # soft drop
    m.shape(t.bar, _slab(56, 172, 380, 150, 0.2))
    m.shape(accent, _slab(56, 330, 300, 34, 0.2))
    m.text(word, _fit(word, 300, 118), t.headline, "250", "248")
    if number is not None:
        m.shape(accent, _rrect(110, 128, 54, 30, 8))
        m.text(str(number), 40, "0xffffff", "110", "128")


def _toon(m: _Mark, word: str, number: int | None, accent: str) -> None:
    t = m.t
    m.shape(t.ink, _rrect(270, 270, 200, 170, 54))  # offset shadow
    m.shape(t.ink, _rrect(246, 246, 200, 170, 54))
    m.shape(t.panel_top, _rrect(246, 246, 186, 156, 44))
    m.text(word, _fit(word, 330, 132, 0.7), "0xffffff", "246", "246", border=(9, t.ink), rotate=-0.10)
    if number is not None:
        m.shape(t.ink, _circle(420, 96, 58))
        m.shape(accent, _circle(420, 96, 48))
        m.text(str(number), 52, "0xffffff", "420", "96")


def _creature(m: _Mark, word: str, number: int | None, accent: str) -> None:
    t = m.t
    m.shape(accent, _ring(256, 236, 196, 30), blur=14)  # glow
    m.shape(t.panel_bottom, _circle(256, 236, 190))
    m.shape(accent, _ring(256, 236, 190, 12))
    m.text(word, _fit(word, 300, 104), t.ink, "256", "236", shadow=(0, 0, accent))
    if number is not None:
        m.shape(accent, _rrect(256, 452, 56, 30, 30))
        m.text(str(number), 40, t.panel_bottom, "256", "452")


def _noir(m: _Mark, word: str, number: int | None, accent: str) -> None:
    m.shape("0x0b0b0b", _rrect(256, 256, 214, 214, 6))
    inner = f"max({_rrect(256, 256, 196, 196, 2)}-{_rrect(256, 256, 192, 192, 2)},0)"
    m.shape("0xe8e8e8", inner)
    m.text(word, _fit(word, 330, 120, 0.66), "0xf2f2f2", "256", "236", font=_font("DejaVu Serif Condensed:bold"))
    m.shape("0xe8e8e8", _rrect(256, 330, 90, 1.5, 0))
    if number is not None:
        m.text(f"No. {number}", 34, "0xbdbdbd", "256", "376", font=_font("DejaVu Serif Condensed:bold"))


def _frontier(m: _Mark, word: str, number: int | None, accent: str) -> None:
    t = m.t
    m.shape(t.bar, _circle(256, 256, 230))
    m.shape(t.panel_top, _circle(256, 256, 214))
    m.shape(t.bar, _ring(256, 256, 196, 5))
    m.shape(accent, _slab(10, 206, 492, 100, 0))
    m.text(word, _fit(word, 400, 92, 0.68), t.panel_top, "256", "256", border=(3, t.bar))
    for cx in (196, 316):
        m.shape(t.bar, _diamond(cx, 146, 12))
    m.shape(t.bar, _diamond(256, 368, 14))
    if number is not None:
        m.text(str(number), 44, t.bar, "256", "146")
    else:
        m.shape(t.bar, _diamond(256, 146, 12))


def _newsreel(m: _Mark, word: str, number: int | None, accent: str) -> None:
    t = m.t
    m.shape("0x151515", _rrect(256, 212, 200, 180, 10))
    m.shape("0xd8d8d8", _ring(256, 212, 150, 6))
    m.shape("0xd8d8d8", _ring(256, 212, 120, 3))
    m.shape("0xd8d8d8", "clip(1.5-abs(X-256),0,1)*clip(180-abs(Y-212),0,1)")
    m.shape("0xd8d8d8", "clip(1.5-abs(Y-212),0,1)*clip(200-abs(X-256),0,1)")
    big = str(number) if number is not None else word[:2]
    m.text(big, 170 if len(big) <= 2 else 120, "0xf0f0f0", "256", "212", font=_font("DejaVu Serif Condensed:bold"))
    m.shape(t.bar, _rrect(256, 440, 200, 44, 6))
    m.text(word, _fit(word, 360, 58, 0.66), "0xf0f0f0", "256", "440", font=_font("DejaVu Serif Condensed:bold"))


def _classroom(m: _Mark, word: str, number: int | None, accent: str) -> None:
    t = m.t
    m.shape(t.bar, _rrect(256, 256, 236, 104, 104))
    m.shape(t.panel_top, f"max({_rrect(256, 256, 226, 94, 94)}-{_rrect(256, 256, 220, 88, 88)},0)")
    has_num = number is not None
    m.text(word, _fit(word, 270 if has_num else 380, 100, 0.66), t.panel_top, "318" if has_num else "256", "256")
    if has_num:
        m.shape(accent, _circle(110, 256, 62))
        m.text(str(number), 54, "0xffffff", "110", "256")


def _vhs(m: _Mark, word: str, number: int | None, accent: str) -> None:
    t = m.t
    for i, col in enumerate([t.bar, accent, t.grey_top, t.bar]):
        m.shape(col, _slab(30, 260 + i * 34, 452, 22, 0.35))
    size = _fit(word, 400, 132, 0.62)
    m.text(word, size, t.bar, "262", "218", shear=-0.3)
    m.text(word, size, accent, "250", "206", shear=-0.3)
    m.text(word, size, "0xffffff", "256", "212", shear=-0.3, border=(2, t.panel_bottom))
    if number is not None:
        m.text(str(number), 64, "0xffffff", "400", "430", shear=-0.3, border=(3, t.bar))


def _concert(m: _Mark, word: str, number: int | None, accent: str) -> None:
    t = m.t
    m.shape("0x0d0d0d", _circle(256, 220, 196))
    for r in (176, 150, 124, 100):
        m.shape("0x2a2a2a", _ring(256, 220, r, 2))
    m.shape(t.bar, _circle(256, 220, 70))
    m.shape("0x0d0d0d", _circle(256, 220, 6))
    if number is not None:
        m.text(str(number), 36, "0xffffff", "256", "184")
    m.shape(accent, _rrect(256, 440, 214, 44, 44))
    m.text(word, _fit(word, 380, 64), t.panel_bottom, "256", "440")


DESIGNS = {"broadcast": _broadcast, "toon": _toon, "creature": _creature, "noir": _noir, "frontier": _frontier,
           "newsreel": _newsreel, "classroom": _classroom, "vhs": _vhs, "concert": _concert}


def render_mark(name: str, number: int | None, theme: Theme, accent: str, text: str | None = None) -> bytes:
    """A 512×512 transparent PNG logo mark for the channel."""
    work = Path(tempfile.mkdtemp(prefix="atv-logo-"))
    try:
        m = _Mark(theme, work)
        DESIGNS.get(theme.key, _broadcast)(m, wordmark(name, text), number, accent)
        cmd = [settings.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-filter_complex", m.build(), "-map", "[v]",
               "-frames:v", "1", "-c:v", "png", "-pix_fmt", "rgba", "-f", "image2pipe", "pipe:1"]
        p = subprocess.run(cmd, capture_output=True, timeout=60)
        if p.returncode != 0 or not p.stdout.startswith(b"\x89PNG"):
            raise RuntimeError(p.stderr.decode(errors="replace")[-600:] or "no image produced")
        return p.stdout
    finally:
        shutil.rmtree(work, ignore_errors=True)
