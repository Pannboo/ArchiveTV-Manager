"""Animated "Coming Up Next" bumpers.

The kinetic break style of late-90s kids' and youth channels, rebuilt from
its mechanics rather than any one channel's look: type that slams in and
settles with an overshoot, a frame that bounces up playing a few seconds of
the show that's next, a "then / later" strip, the channel's logo in the
corner and a wipe out into the programme. Colours, type and finish come from
the channel's look (themes.py).

Every moving part is a small layer drawn once and looped; movement is an
``overlay`` position expression (a damped spring), so a bumper costs about
as much CPU as a plain card. A viewer joining mid-bumper sees it exactly as
it stands at that second (``offset``).

The preview is read from the next programme's own source for the length of
the bumper (a few MB at most). If that fails the bumper is redrawn with the
thumbnail instead, panning slowly (see stream._handle_failure).
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

from .cards import CHORDS, FPS, HEIGHT, WIDTH, CardRender, CardSpec, _esc_path, _finish, _font, _wrap
from .config import settings
from .security import FFMPEG_PROTOCOLS

FRAME_X, FRAME_Y, FRAME_W, FRAME_H = 600, 150, 600, 338
BAND_Y, BAND_H = 584, 86
BUG = 100  # corner logo size


def _spring(t0: float, start: float, end: float, o: str, k: float = 6.5, w: float = 11.0) -> str:
    """Position easing from ``start`` to ``end`` from time ``t0``: a damped
    spring, so things overshoot and settle like hand-timed cel animation."""
    tt = f"(t+{o}-{t0:.2f})"
    return f"if(lt(t+{o},{t0:.2f}),{start},{end}+({start}-{end})*exp(-{k}*{tt})*cos({w}*{tt}))"


def render_bumper(card: CardSpec, duration: float, offset: float = 0.0, *, drawtext: bool = True,
                  gradients: bool = True, rate: list[str] | None = None) -> CardRender:
    th = card.theme
    rng = random.Random(f"bumper:{card.seed}")
    rate = rate or []
    d = max(0.5, duration)
    full = d + offset
    o = f"{offset:.3f}"
    work = Path(tempfile.mkdtemp(prefix="atv-bumper-"))
    font = _font(th.font)
    loop = f"loop=loop=-1:size=1,setpts=N/{FPS}/TB"

    def tfile(name: str, content: str) -> str:
        p = work / f"{name}.txt"
        p.write_text(content, encoding="utf-8")
        return _esc_path(str(p))

    # -- inputs: 0 background, 1 audio, 2 preview (clip / thumbnail), 3 logo
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
        # A brighter, bouncier bed than the card's pad: plucked chord stabs.
        pluck = f"exp(-6*({t}-0.5*floor({t}*2)))"  # no commas: this sits inside a lavfi graph
        left = f"0.07*(sin(2*PI*{f1 * 2}*{t})+0.8*sin(2*PI*{f2 * 2}*{t})+0.6*sin(2*PI*{f3 * 2}*{t}))*{pluck}"
        right = f"0.07*(sin(2*PI*{f1 * 2.003:.3f}*{t})+0.8*sin(2*PI*{f2 * 1.997:.3f}*{t})+0.6*sin(2*PI*{f3 * 2}*{t}))*{pluck}"
        inputs += ["-f", "lavfi", "-i", f"aevalsrc=exprs={left}|{right}:s=48000:d={d:.3f}"]
        apre = "[1:a]aecho=0.8:0.5:120:0.2,lowpass=f=5000"
    afade = [] if offset >= 0.3 else [f"afade=t=in:d={0.3 - offset:.2f}"]
    afade.append(f"afade=t=out:st={max(0.0, full - 0.8 - offset):.2f}:d=0.8")
    graph = [f"{apre},{','.join(afade)},atrim=0:{d:.3f}[a]"]

    n = 2
    preview = None
    clip = card.clip
    if clip is not None:
        start = clip.offset + offset
        src = ["-ss", f"{start:.3f}", "-t", f"{d:.3f}"]
        if clip.url and clip.url.startswith("http"):
            src = ["-protocol_whitelist", FFMPEG_PROTOCOLS, "-reconnect", "1", "-rw_timeout", "15000000",
                   "-user_agent", settings.http_user_agent] + src
        inputs += src + rate + ["-i", clip.url or ""]
        # Fill the frame (4:3 Archive films lose a little top and bottom).
        preview = (f"[{n}:v]scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=increase,"
                   f"crop={FRAME_W}:{FRAME_H},setsar=1,fps={FPS},format=yuva420p")
        n += 1
    else:
        thumb = next((card.thumbs.get(r.thumb_url or "") for r in card.rows[:1] if card.thumbs.get(r.thumb_url or "")), None)
        if thumb:
            p = work / "thumb.img"
            p.write_bytes(thumb)
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", str(p)]
            # Slow pan across the still (the "Ken Burns" move).
            preview = (f"[{n}:v]scale={FRAME_W + 80}:{FRAME_H + 46}:force_original_aspect_ratio=increase,"
                       f"crop={FRAME_W}:{FRAME_H}:x='(iw-{FRAME_W})*(t+{o})/{full:.2f}':y='(ih-{FRAME_H})/2',"
                       f"setsar=1,format=yuva420p")
            n += 1
    logo_idx = None
    if card.logo_path and Path(card.logo_path).is_file():
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", card.logo_path]
        logo_idx = n
        n += 1

    # -- background: the look's colours with slow-scrolling slanted stripes
    stripe = (f"color=c=black@0.0:s={WIDTH * 2}x{HEIGHT}:r={FPS}:d=0.04,format=rgba,"
              f"geq=r={int(card.accent[2:4], 16)}:g={int(card.accent[4:6], 16)}:b={int(card.accent[6:8], 16)}:"
              f"a='if(lt(mod(X+0.6*Y,160),56),46,0)',{loop}[stripes]")
    graph.append(stripe)
    graph.append("[0:v]format=yuv420p[bg0]")
    graph.append(f"[bg0][stripes]overlay=x='-mod((t+{o})*60,320)':y=0:shortest=1[bg]")
    cur = "bg"
    step = 0

    def over(layer: str, x: str, y: str, finite: bool = False) -> None:
        """Looped layers end with the background (shortest); a finite one
        (the preview, the logo) just stops being drawn when it runs out."""
        nonlocal cur, step
        step += 1
        nxt = f"v{step}"
        end = "eof_action=pass" if finite else "shortest=1"
        graph.append(f"[{cur}][{layer}]overlay=x='{x}':y='{y}':{end}[{nxt}]")
        cur = nxt

    def text_layer(label: str, content: str, size: int, color: str, w: int, h: int, *, border: str = "",
                   box: str | None = None, spacing: int = 6) -> None:
        opts = (f"textfile='{tfile(label, content)}':expansion=none:{font}:fontsize={size}:fontcolor={color}:"
                f"line_spacing={spacing}:x=16:y=(h-th)/2" + (f":borderw=6:bordercolor={border}" if border else ""))
        base = f"color=c={box or 'black@0.0'}:s={w}x{h}:r={FPS}:d=0.04,format=rgba"
        graph.append(f"{base},drawtext={opts},{loop}[{label}]")

    # -- preview frame, bounces up from below
    frame_y = _spring(0.55, HEIGHT + 40, FRAME_Y, o)
    graph.append(f"color=c=0x000000@0.45:s={FRAME_W + 24}x{FRAME_H + 24}:r={FPS}:d=0.04,format=rgba,{loop}[shadow]")
    over("shadow", str(FRAME_X + 14), f"{frame_y}+14")
    graph.append(f"color=c={th.box_edge}:s={FRAME_W + 24}x{FRAME_H + 24}:r={FPS}:d=0.04,format=rgba,{loop}[frame]")
    over("frame", str(FRAME_X - 12), f"{frame_y}-12")
    if preview:
        # Under the preview, the accent fills the frame if the clip runs out.
        graph.append(f"color=c={card.accent}:s={FRAME_W}x{FRAME_H}:r={FPS}:d=0.04,format=rgba,{loop}[pvbg]")
        over("pvbg", str(FRAME_X), frame_y)
        graph.append(preview + "[pv]")
        over("pv", str(FRAME_X), frame_y, finite=True)
    else:
        graph.append(f"color=c={card.accent}:s={FRAME_W}x{FRAME_H}:r={FPS}:d=0.04,format=rgba,{loop}[pv]")
        over("pv", str(FRAME_X), frame_y)

    if drawtext:
        nxt = card.rows[0] if card.rows else None
        text_layer("coming", "COMING UP", 58, th.ink, 520, 84)
        over("coming", _spring(0.15, -560, 64, o), "70")
        text_layer("next", "NEXT!", 168, card.accent, 560, 196, border=th.ink)
        over("next", _spring(0.32, WIDTH + 20, 52, o, w=13), "128")
        title = _wrap((nxt.title if nxt else card.channel).upper(), 16, 3)
        lines = title.count("\n") + 1
        size = 50 if lines <= 2 else 40
        text_layer("title", title, size, th.headline, 520, 60 + lines * (size + 10), box=th.bar, spacing=10)
        over("title", _spring(0.9, -600, 52, o), "340")
        if nxt and nxt.time:
            text_layer("when", f"{nxt.time} {nxt.ampm}".strip(), 40, th.ink, 260, 64)
            over("when", "68", _spring(1.15, HEIGHT + 20, 340 + 70 + lines * (size + 10), o))
        later = [r for r in card.rows[1:3]]
        if later:
            strip = "      ".join(f"{lbl}  {r.time} {r.ampm}  {_wrap(r.title.upper(), 24, 1)}"
                                  for lbl, r in zip(("THEN", "LATER"), later))
            text_layer("later", strip, 32, th.headline, WIDTH, BAND_H, box=th.bar)
            over("later", "0", _spring(2.0, HEIGHT + 10, BAND_Y, o, k=7))
    if logo_idx is not None:
        graph.append(f"[{logo_idx}:v]scale={BUG}:{BUG},format=yuva420p,"
                     f"fade=t=in:st={max(0.0, 0.5 - offset):.2f}:d=0.4:alpha=1[bug]")
        over("bug", str(WIDTH - BUG - 40), "22", finite=True)
    if drawtext and card.credit:
        text_layer("credit", card.credit, 22, th.ink_dim, 560, 40)
        over("credit", str(FRAME_X - 16), str(FRAME_Y + FRAME_H + 22))

    # -- wipe out: a slab in the look's bar colour sweeps across at the end
    graph.append(f"color=c={th.bar}:s={WIDTH + 200}x{HEIGHT}:r={FPS}:d=0.04,format=rgba,{loop}[wipe]")
    wipe_t0 = max(0.0, full - 0.55)
    over("wipe", f"if(lt(t+{o},{wipe_t0:.2f}),{-WIDTH - 260},{-WIDTH - 260}+({WIDTH + 260})*min(1,(t+{o}-{wipe_t0:.2f})/0.45))", "0")
    finish = _finish(th) + [f"trim=0:{d:.3f}", "setpts=PTS-STARTPTS", "format=yuv420p"]
    graph.append(f"[{cur}]" + ",".join(finish) + "[v]")
    return CardRender(inputs, ";".join(graph), work)
