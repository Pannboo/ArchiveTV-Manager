"""Parallel fetcher for remote media, held in RAM.

archive.org limits how fast a single connection downloads, and that limit
moves around: the same file measured 2 Mbps on one connection in the
morning and 10 Mbps in the afternoon, while four connections together
reached 27–29 Mbps. A programme whose bitrate sits near or above the
single-connection speed stutters however much read-ahead there is.

FFmpeg therefore reads remote files through a small HTTP server on the
loopback interface. Each programme is split into 512 KiB chunks; up to
``parallel_connections`` range requests fetch the chunks just ahead of
where FFmpeg is reading, and FFmpeg receives bytes as soon as they arrive.
At most a few chunks per programme are held in memory and nothing is
written to disk, so this works in minimal-disk mode. Total download volume
is unchanged: each byte is still fetched once.

Seeks (FFmpeg jumping to an MP4's index at the end of the file, or joining
mid-programme) are ordinary range requests to the relay, which moves its
window to match. Origins that don't support range requests are read
directly, as before.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import time

import httpx

from .config import settings

log = logging.getLogger(__name__)

# Small chunks, so that right after a tune-in or seek the bytes needed first
# are already split across all connections (one 2 MiB chunk on one 2 Mbps
# connection would take 8 s); each request still carries seconds of video.
CHUNK = 512 * 1024
AHEAD = 16  # chunks requested ahead of the reader (8 MiB)
KEEP_BEHIND = 2  # chunks kept behind it, for small backward seeks
RETRIES = 3
WAIT_TIMEOUT = 60.0  # FFmpeg times out (and reconnects) well before this
IDLE_CLOSE = 60.0  # close a relay this long after its last reader left
MAX_CONNECTIONS = 8


def connections() -> int:
    return max(1, min(MAX_CONNECTIONS, int(settings.parallel_connections or 1)))


def enabled() -> bool:
    return connections() > 1


class RelayError(Exception):
    pass


class _Chunk:
    __slots__ = ("data", "done", "error")

    def __init__(self) -> None:
        self.data = bytearray()
        self.done = False
        self.error: str | None = None


class Relay:
    def __init__(self, url: str, size: int, user_agent: str, conns: int):
        self.url, self.size = url, size
        self.token = secrets.token_urlsafe(18)
        self.nchunks = max(1, math.ceil(size / CHUNK))
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=15), headers={"User-Agent": user_agent},
            limits=httpx.Limits(max_connections=conns, max_keepalive_connections=conns),
        )
        self.sem = asyncio.Semaphore(conns)
        self.chunks: dict[int, _Chunk] = {}
        self.tasks: set[asyncio.Task] = set()
        self.readers = 0
        self.last_used = time.monotonic()
        self.fetched = 0
        self.closed = False
        self._progress = asyncio.Event()

    # -- fetching ---------------------------------------------------------------

    def _signal(self) -> None:
        ev, self._progress = self._progress, asyncio.Event()
        ev.set()

    def _chunk(self, k: int) -> _Chunk:
        """Chunk ``k``, fetching it and the next few; chunks far from ``k`` are dropped."""
        lo, hi = k - KEEP_BEHIND, k + AHEAD
        for i in [i for i in self.chunks if i < lo or i > hi + 2]:
            del self.chunks[i]  # an in-flight fetch finishes into a chunk nobody keeps
        for i in range(k, min(self.nchunks, hi + 1)):
            if i not in self.chunks:
                c = self.chunks[i] = _Chunk()
                t = asyncio.create_task(self._fetch(i, c))
                self.tasks.add(t)
                t.add_done_callback(self.tasks.discard)
        return self.chunks[k]

    async def _fetch(self, i: int, c: _Chunk) -> None:
        start, end = i * CHUNK, min(self.size, (i + 1) * CHUNK) - 1
        try:
            async with self.sem:
                if self.chunks.get(i) is not c:
                    c.error = "dropped"  # the reader moved on before this started
                    return
                err = ""
                for attempt in range(RETRIES):
                    try:
                        rng = f"bytes={start + len(c.data)}-{end}"
                        async with self.client.stream("GET", self.url, headers={"Range": rng}) as r:
                            if r.status_code != 206:
                                raise RelayError(f"origin answered HTTP {r.status_code} to a range request")
                            async for b in r.aiter_bytes(65536):
                                c.data += b
                                self.fetched += len(b)
                                self._signal()
                        if len(c.data) >= end - start + 1:
                            del c.data[end - start + 1:]
                            c.done = True
                            return
                        err = "connection closed early"
                    except (httpx.HTTPError, RelayError) as e:
                        err = str(e) or type(e).__name__
                    await asyncio.sleep(0.5 * (attempt + 1))
                c.error = f"chunk {i}: {err}"
                log.warning("relay %s: %s", self.url, c.error)
        finally:
            self._signal()

    # -- serving ----------------------------------------------------------------

    async def read(self, a: int, b: int):
        """Yield bytes ``a``..``b`` (inclusive) as they arrive."""
        pos = a
        while pos <= b:
            k = pos // CHUNK
            c = self._chunk(k)
            off = pos - k * CHUNK
            while len(c.data) <= off and not c.done and not c.error:
                ev = self._progress
                try:
                    await asyncio.wait_for(ev.wait(), WAIT_TIMEOUT)
                except asyncio.TimeoutError:
                    raise RelayError(f"no data from origin for {WAIT_TIMEOUT:.0f}s") from None
            if c.error == "dropped":
                if self.chunks.get(k) is c:
                    del self.chunks[k]  # fetch it afresh on the next pass
                continue
            if len(c.data) <= off:
                raise RelayError(c.error or "chunk ended early")
            n = min(len(c.data), b - k * CHUNK + 1) - off
            yield bytes(c.data[off:off + n])
            pos += n

    def held_bytes(self) -> int:
        return sum(len(c.data) for c in self.chunks.values())

    async def close(self) -> None:
        self.closed = True
        for t in list(self.tasks):
            t.cancel()
        self.chunks.clear()
        await self.client.aclose()


# ---------------------------------------------------------------- registry

_by_token: dict[str, Relay] = {}
_by_url: dict[str, Relay] = {}
_server: asyncio.AbstractServer | None = None
_loop: asyncio.AbstractEventLoop | None = None
_port = 0
_reaper: asyncio.Task | None = None


def _forget_other_loop() -> None:
    """Relays belong to the event loop that created them (tests run several loops)."""
    global _server, _reaper, _loop
    if _loop is not None and _loop is not asyncio.get_running_loop():
        _by_token.clear()
        _by_url.clear()
        _server = _reaper = _loop = None


def local_url(r: Relay) -> str:
    return f"http://127.0.0.1:{_port}/r/{r.token}"


async def _probe(url: str, user_agent: str) -> int | None:
    """Size of the file when the origin honours range requests, else None."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=15), headers={"User-Agent": user_agent}) as c:
            async with c.stream("GET", url, headers={"Range": "bytes=0-0"}) as r:
                cr = r.headers.get("content-range", "")
                if r.status_code == 206 and "/" in cr and cr.rsplit("/", 1)[1].isdigit():
                    return int(cr.rsplit("/", 1)[1])
    except httpx.HTTPError as e:
        log.info("relay probe failed for %s: %s", url, e)
    return None


async def wrap(url: str, user_agent: str | None = None) -> str:
    """The URL FFmpeg should read: a loopback relay that fetches ``url`` over
    parallel connections, or ``url`` itself when that isn't possible."""
    if not enabled() or not url.startswith(("http://", "https://")):
        return url
    ua = user_agent or settings.http_user_agent
    _forget_other_loop()
    r = _by_url.get(url)
    if r is not None and not r.closed:
        r.last_used = time.monotonic()
        return local_url(r)
    size = await _probe(url, ua)
    if not size:
        return url
    await _ensure_server()
    r = Relay(url, size, ua, connections())
    _by_token[r.token] = r
    _by_url[url] = r
    log.info("relay: %d parallel connections for %s (%.0f MB)", connections(), url, size / 1e6)
    return local_url(r)


def stats() -> dict:
    live = [r for r in _by_token.values() if not r.closed]
    return {"relays": len(live), "relay_bytes": sum(r.held_bytes() for r in live),
            "relay_readers": sum(r.readers for r in live)}


async def close_all() -> None:
    global _server, _reaper, _loop
    _forget_other_loop()
    for r in list(_by_token.values()):
        await r.close()
    _by_token.clear()
    _by_url.clear()
    if _reaper:
        _reaper.cancel()
        _reaper = None
    if _server:
        _server.close()
        _server = None
    _loop = None


# ---------------------------------------------------------------- HTTP server


async def _ensure_server() -> None:
    global _server, _port, _reaper, _loop
    if _server is not None:
        return
    _loop = asyncio.get_running_loop()
    _server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    _port = _server.sockets[0].getsockname()[1]
    _reaper = asyncio.create_task(_reap())


async def _reap() -> None:
    while True:
        await asyncio.sleep(10)
        now = time.monotonic()
        for r in [r for r in _by_token.values() if r.readers == 0 and now - r.last_used > IDLE_CLOSE]:
            _by_token.pop(r.token, None)
            if _by_url.get(r.url) is r:
                del _by_url[r.url]
            log.info("relay closed for %s (%.0f MB fetched)", r.url, r.fetched / 1e6)
            await r.close()


def _parse_range(h: str | None, size: int) -> tuple[int, int] | None | bool:
    """(start, end) for a single byte range, None without one, False when unsatisfiable."""
    if not h or not h.strip().lower().startswith("bytes="):
        return None
    spec = h.split("=", 1)[1].split(",")[0].strip()
    a, _, b = spec.partition("-")
    try:
        if not a:  # suffix: the last N bytes
            n = int(b)
            return (max(0, size - n), size - 1) if n > 0 else False
        start = int(a)
        end = min(int(b), size - 1) if b else size - 1
    except ValueError:
        return None
    return (start, end) if start < size and start <= end else False


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    r: Relay | None = None
    counted = False
    try:
        line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1").split()
        headers: dict[str, str] = {}
        while True:
            h = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            if h in ("\r\n", "\n", ""):
                break
            k, _, v = h.partition(":")
            headers[k.strip().lower()] = v.strip()
        method, path = (line[0], line[1]) if len(line) >= 2 else ("", "")
        r = _by_token.get(path.rsplit("/", 1)[-1]) if path.startswith("/r/") else None
        if method not in ("GET", "HEAD") or r is None or r.closed:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return
        rng = _parse_range(headers.get("range"), r.size)
        if rng is False:
            writer.write(f"HTTP/1.1 416 Range Not Satisfiable\r\nContent-Range: bytes */{r.size}\r\n"
                         "Content-Length: 0\r\nConnection: close\r\n\r\n".encode())
            return
        a, b = rng if rng else (0, r.size - 1)
        status = "206 Partial Content" if rng else "200 OK"
        head = (f"HTTP/1.1 {status}\r\nContent-Type: application/octet-stream\r\nAccept-Ranges: bytes\r\n"
                f"Content-Length: {b - a + 1}\r\n" + (f"Content-Range: bytes {a}-{b}/{r.size}\r\n" if rng else "")
                + "Connection: close\r\n\r\n")
        writer.write(head.encode())
        if method == "HEAD":
            return
        r.readers += 1
        counted = True
        async for piece in r.read(a, b):
            writer.write(piece)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
        pass
    except RelayError as e:
        log.warning("relay %s: %s", r.url if r else "?", e)
    finally:
        if r is not None:
            if counted:
                r.readers -= 1
            r.last_used = time.monotonic()
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass
