"""Channel pack routes (/api/v1/packs): browse starters, install, import,
export, tune and uninstall. See packs.py for what a pack is."""

from __future__ import annotations

import asyncio
import json
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from . import ai, packs
from .api import require_api
from .config import settings
from .db import session_scope
from .enums import PackPolicy
from .models import ChannelPack
from .security import UnsafeURLError, check_url_resolves_public

router = APIRouter(prefix="/api/v1/packs", dependencies=[Depends(require_api)], tags=["packs"])

MAX_PACK_BYTES = 256 * 1024
_fills: set[asyncio.Task] = set()


class InstallIn(BaseModel):
    starter: str | None = Field(None, description="Id of a bundled starter pack")
    spec: dict | None = Field(None, description="A pack file's contents")
    url: str | None = Field(None, description="https address of a pack file")
    theme_check: bool = False
    upgrade: bool = False


class PreviewIn(BaseModel):
    spec: dict | None = None
    url: str | None = None


class PackPatch(BaseModel):
    target_hours: float | None = Field(None, ge=1, le=10000)
    policy: PackPolicy | None = None
    theme_check: bool | None = None
    enabled: bool | None = None
    refresh_days: int | None = Field(None, ge=1, le=90)
    rotate_percent: int | None = Field(None, ge=0, le=50)


def _err(e: packs.PackError) -> HTTPException:
    return HTTPException(e.status, e.message)


async def fetch_pack(url: str) -> packs.PackSpec:
    """Download a pack file from a public https address (no private hosts,
    a few redirects at most, 256 KiB at most)."""
    current = url.strip()
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers={"User-Agent": settings.http_user_agent}) as c:
        for _ in range(4):
            parts = urlsplit(current)
            if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
                raise HTTPException(400, "pack URLs must be plain https addresses")
            try:
                await check_url_resolves_public(current)
            except UnsafeURLError as e:
                raise HTTPException(400, str(e))
            try:
                async with c.stream("GET", current) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        current = urljoin(current, r.headers["location"])
                        continue
                    if r.status_code != 200:
                        raise HTTPException(400, f"pack URL returned HTTP {r.status_code}")
                    body = b""
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_PACK_BYTES:
                            raise HTTPException(400, "pack file is too large (256 KiB at most)")
            except httpx.HTTPError as e:
                raise HTTPException(400, f"could not fetch the pack: {e}")
            try:
                return packs.parse_spec(body)
            except packs.PackError as e:
                raise _err(e)
    raise HTTPException(400, "too many redirects")


async def _spec_from(starter: str | None, spec: dict | None, url: str | None) -> packs.PackSpec:
    if sum(x is not None for x in (starter, spec, url)) != 1:
        raise HTTPException(400, "give exactly one of starter, spec or url")
    if starter is not None:
        sp = packs.starter_pack(starter)
        if sp is None:
            raise HTTPException(404, "unknown starter pack")
        return sp
    if url is not None:
        return await fetch_pack(url)
    try:
        return packs.parse_spec(spec)
    except packs.PackError as e:
        raise _err(e)


def _summary(sp: packs.PackSpec, installed: dict[str, int]) -> dict:
    return {"id": sp.id, "name": sp.name, "description": sp.description, "number": sp.number, "policy": sp.policy,
            "target_hours": sp.target_hours, "providers": sorted({r.provider for r in sp.rules}),
            "blocks": [b.name for b in sp.blocks], "theme": sp.theme, "installed": sp.id in installed,
            "installed_version": installed.get(sp.id), "version": sp.version}


def _start_fill(pack_id: int) -> None:
    t = asyncio.create_task(packs.fill_pack(pack_id, force=True), name=f"pack-fill-{pack_id}")
    _fills.add(t)
    t.add_done_callback(_fills.discard)


@router.get("")
def list_packs():
    with session_scope() as s:
        rows = s.scalars(select(ChannelPack).order_by(ChannelPack.installed_at)).all()
        keys = {p.key: p.version for p in rows}
        return {
            "installed": [packs.pack_out(s, p) for p in rows],
            "starters": [_summary(sp, keys) for sp in packs.starter_packs()],
            "theme_check_available": ai.available(),
            "theme_check_model": ai.describe()["model"],
            "ai": ai.describe(),
            "lab_mode": settings.packs_air_everything,
            "youtube_available": packs.youtube_available(),
        }


@router.get("/starters/{key}")
def starter(key: str):
    sp = packs.starter_pack(key)
    if sp is None:
        raise HTTPException(404, "unknown starter pack")
    return sp.model_dump(mode="json", exclude_none=True)


@router.post("/preview")
async def preview(body: PreviewIn):
    """Validate a pack (pasted or from a URL) without installing it."""
    sp = await _spec_from(None, body.spec, body.url)
    with session_scope() as s:
        installed = s.scalar(select(ChannelPack.id).where(ChannelPack.key == sp.id)) is not None
    return {"spec": sp.model_dump(mode="json", exclude_none=True), "installed": installed}


@router.post("/install", status_code=201)
async def install(body: InstallIn, request: Request):
    sp = await _spec_from(body.starter, body.spec, body.url)
    if body.theme_check and not ai.available():
        raise HTTPException(400, "the theme check needs an AI backend (Settings: local model or Claude)")
    actor = request.session.get("user") or "api"
    with session_scope() as s:
        try:
            pack = packs.install(s, sp, theme_check=body.theme_check, actor=actor, upgrade=body.upgrade)
        except packs.PackError as e:
            raise _err(e)
        s.flush()
        pid = pack.id
        out = packs.pack_out(s, pack)
    _start_fill(pid)  # the first scan starts straight away
    return out


def _pack(s, pack_id: int) -> ChannelPack:
    p = s.get(ChannelPack, pack_id)
    if p is None:
        raise HTTPException(404, "unknown pack")
    return p


@router.patch("/{pack_id}")
def patch_pack(pack_id: int, body: PackPatch):
    with session_scope() as s:
        p = _pack(s, pack_id)
        ch = body.model_dump(exclude_unset=True)
        if ch.get("theme_check") and not ai.available():
            raise HTTPException(400, "the theme check needs an AI backend (Settings: local model or Claude)")
        if ch.get("theme_check") and not (p.spec or {}).get("theme"):
            raise HTTPException(400, "this pack has no theme to check against")
        for k, v in ch.items():
            setattr(p, k, str(v) if k == "policy" else v)
        if "target_hours" in ch or "enabled" in ch:
            p.last_fill_at = None  # re-evaluate on the next fill round
        return packs.pack_out(s, p)


@router.post("/{pack_id}/fill", status_code=202)
async def fill_now(pack_id: int):  # async: the fill task needs the event loop
    with session_scope() as s:
        _pack(s, pack_id)
    _start_fill(pack_id)
    return {"started": True}


_refreshes: set[asyncio.Task] = set()


@router.post("/{pack_id}/refresh", status_code=202)
async def refresh_now(pack_id: int):  # async: the refresh task needs the event loop
    """Run the weekly refresh now: re-check tapes, look for new uploads, rotate."""
    with session_scope() as s:
        _pack(s, pack_id)
    t = asyncio.create_task(packs.refresh_pack(pack_id, force=True), name=f"pack-refresh-{pack_id}")
    _refreshes.add(t)
    t.add_done_callback(_refreshes.discard)
    return {"started": True}


@router.get("/{pack_id}/export")
def export_pack(pack_id: int):
    with session_scope() as s:
        p = _pack(s, pack_id)
        data = packs.export(p)
        name = p.key
    return Response(json.dumps(data, indent=2, ensure_ascii=False) + "\n", media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}.archivetv-pack.json"'})


@router.delete("/{pack_id}")
def delete_pack(pack_id: int, disable_channel: bool = False, request: Request = None):
    with session_scope() as s:
        p = _pack(s, pack_id)
        packs.uninstall(s, p, disable_channel=disable_channel, actor=(request.session.get("user") if request else None) or "api")
    return {"ok": True}


# ------------------------------------------------- curated YouTube channels


class ChannelRefIn(BaseModel):
    ref: str = Field(..., description="@handle, youtube.com/@handle or youtube.com/channel/UC…")


def _follow(s, pack_id: int, follow_id: int):
    from .models import FollowedChannel

    fc = s.get(FollowedChannel, follow_id)
    if fc is None or fc.pack_id != pack_id:
        raise HTTPException(404, "unknown channel")
    return fc


@router.get("/{pack_id}/channels")
def pack_channels(pack_id: int):
    """Suggested, followed and dismissed YouTube channels for a pack."""
    from . import ytfollow

    with session_scope() as s:
        _pack(s, pack_id)
        return ytfollow.summary(s, pack_id)


@router.post("/{pack_id}/channels", status_code=201)
async def add_channel(pack_id: int, body: ChannelRefIn):
    """Follow a channel you know (its uploads are harvested on the next fill)."""
    from . import ytfollow

    with session_scope() as s:
        _pack(s, pack_id)
    try:
        await ytfollow.add(pack_id, body.ref)
    except ValueError as e:
        raise HTTPException(400, str(e))
    with session_scope() as s:
        return ytfollow.summary(s, pack_id)


@router.post("/{pack_id}/channels/{follow_id}/{action}")
async def channel_action(pack_id: int, follow_id: int, action: Literal["follow", "unfollow", "dismiss", "vet"]):
    from . import ytfollow

    with session_scope() as s:
        fc = _follow(s, pack_id, follow_id)
        if action == "follow":
            ytfollow.follow(s, fc)
        elif action == "unfollow":
            ytfollow.unfollow(s, fc, dismiss=False)
        elif action == "dismiss":
            ytfollow.unfollow(s, fc, dismiss=True)
    if action == "vet":
        if not ai.available():
            raise HTTPException(400, "no AI backend configured (Settings)")
        await ytfollow.vet(follow_id)
    with session_scope() as s:
        return ytfollow.summary(s, pack_id)


# ----------------------------------------------------------------------- AI

ai_router = APIRouter(prefix="/api/v1/ai", dependencies=[Depends(require_api)], tags=["ai"])
_enrich_tasks: set[asyncio.Task] = set()


@ai_router.get("")
def ai_status():
    from sqlalchemy import func

    from .models import MediaItem

    with session_scope() as s:
        total = s.scalar(select(func.count()).select_from(MediaItem)) or 0
        done = s.scalar(select(func.count()).select_from(MediaItem).where(func.json_extract(MediaItem.ai_meta, "$.title").is_not(None))) or 0
    return {**ai.describe(), "enriched": done, "catalogue": total}


@ai_router.post("/test")
async def ai_test():
    """One tiny request to the configured backend."""
    try:
        return await ai.ping()
    except ai.AIError as e:
        raise HTTPException(502, str(e))


class EnrichIn(BaseModel):
    limit: int = Field(500, ge=1, le=5000)
    provider: Literal["any", "youtube", "archive"] = "any"


@ai_router.post("/enrich", status_code=202)
async def ai_enrich(body: EnrichIn):
    """Clean up and tag catalogue items found before AI was set up (in the background)."""
    from sqlalchemy import func

    from .models import MediaItem, MediaSource

    if not ai.available():
        raise HTTPException(400, "no AI backend configured (Settings)")
    with session_scope() as s:
        q = select(MediaItem.id).join(MediaSource).where(func.json_extract(MediaItem.ai_meta, "$.title").is_(None),
                                                         MediaItem.metadata_locked.is_(False))
        if body.provider != "any":
            q = q.where(MediaSource.provider == body.provider)
        ids = list(s.scalars(q.order_by(MediaItem.id.desc()).limit(body.limit)).all())
    t = asyncio.create_task(ai.enrich_items(ids), name="ai-enrich")
    _enrich_tasks.add(t)
    t.add_done_callback(_enrich_tasks.discard)
    return {"queued": len(ids)}
