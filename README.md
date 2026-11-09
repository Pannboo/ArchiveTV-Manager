# ArchiveTV Manager 2.0

Self-hosted service that builds traditional linear TV channels from remotely hosted video. It discovers content on the **Internet Archive** and **YouTube**, queues it for **mandatory manual approval**, and builds deterministic 24/7 schedules. It then delivers the channels, with **Start Over and catch-up**, through an existing **Dispatcharr** install. Nothing is recorded or downloaded.

ArchiveTV does not replace Dispatcharr. Dispatcharr keeps managing channels, groups, clients and EPG distribution, and ArchiveTV appears to it as one more provider.

The project keeps these concerns separate, so a new provider never touches scheduling or delivery:

| Concern | Where it lives |
|---|---|
| Where content was discovered | `MediaSource` (provider + external id) |
| Whether an admin approved it | `MediaItem.review_status` |
| Redistribution permission | `PlaybackSource.rights_status` |
| Technical playback eligibility | `PlaybackSource.eligibility` + `.health_ok` + `.seekable` |
| When it airs | `ScheduleEntry` (persisted, UTC) |
| Whether it can be replayed later | checked again at request time (`catchup.resolve_request`) |

**Scheduling rule:** an item enters a published schedule only when all four of these hold:
- it is `APPROVED`
- it has a playback source that is `IPTV_READY` or `IPTV_NEEDS_TRANSCODE`
- that source's rights are `AUTHORISED`
- that source passed its most recent health check

Approving an item never makes it IPTV-eligible by itself. **Put on air** (below) does all of these steps in one admin action; it never overrides a failed playback check or a source marked `RESTRICTED`.

**YouTube is discovery only by default.** YouTube items get the official embedded player for previews and stay `EMBED_ONLY`/`REFERENCE_ONLY`. YouTube URLs are rejected as playback sources. To air something you found on YouTube, attach a separately authorised source on the item page, or opt into the experimental streaming below.

---

## Quick start (Docker, next to Dispatcharr)

1. Find the Docker network Dispatcharr is on:
   ```bash
   docker inspect dispatcharr --format '{{json .NetworkSettings.Networks}}'
   ```
2. Configure:
   ```bash
   cp .env.example .env
   ```
   Then set `DISPATCHARR_NETWORK` and `ATV_ADMIN_PASSWORD`, and optionally `ATV_XC_PASSWORD`.
3. Create the YouTube key secret. Leave the file empty if you aren't using YouTube:
   ```bash
   mkdir -p secrets media && touch secrets/youtube_api_key && chmod 600 secrets/youtube_api_key
   ```
4. Start it:
   ```bash
   docker compose up -d --build
   ```
5. Open the admin UI at `http://127.0.0.1:8090`. It is bound to localhost by default; see *Networking*.

The admin UI is a React + TypeScript app served at `/app/`, built from `frontend/` inside the Docker image. It is built entirely on the REST API. The original server-rendered pages remain at `/admin` as a fallback and use the same back end.

The compose file never touches Dispatcharr: no shared volumes, no database access, no restarts.

## Adding ArchiveTV to Dispatcharr

Dispatcharr (verified against **0.33.0**) only passes catch-up through for **Xtream Codes** accounts. ArchiveTV therefore exposes a small Xtream Codes-compatible upstream interface, and that is the recommended integration.

### 1. Xtream Codes account (live + catch-up)

In **M3U & EPG Manager → Add M3U**, choose type **Xtream Codes**:

| Field | Value |
|---|---|
| Server URL | `http://archivetv:8090` |
| Username | `archivetv` (or `ATV_XC_USERNAME`) |
| Password | `ATV_XC_PASSWORD`, or the generated one from `docker exec archivetv cat /data/run/xc_password` |

Let Dispatcharr create the EPG source it offers (`/xmltv.php?username=…&password=…`), or add it by hand. Then enable the **ArchiveTV** group, create channels from the imported streams, and match EPG by `tvg-id`.

Stream ids are ArchiveTV's internal channel ids and never change. Refreshing the account keeps your Dispatcharr numbering, logos and groups.

### 2. Dispatcharr settings that matter

- **Settings → EPG → Default days back**: this is **0** by default, which hides every ended programme from clients and makes catch-up unreachable. Set it to your catch-up window, e.g. 7. A per-user `epg_prev_days` or `?prev_days=N` also works.
- **System settings → Catch-up** must be enabled (the default), and so must the per-user catch-up option for each user.
- **No recordings:** ArchiveTV never records, but Dispatcharr's DVR can. Check that nothing targets ArchiveTV channels under recordings, recurring rules and series rules (`/api/channels/recordings/`, `/recurring-rules/`, `/series-rules/`).
- **Stream profile:** the default proxy profile works. ArchiveTV already outputs MPEG-TS (H.264 + AAC 48 kHz).

### 3. Plain M3U (live only, optional)

`http://archivetv:8090/playlist.m3u` and `/epg.xml` still work for clients that read ArchiveTV directly. Dispatcharr gives catch-up only to Xtream Codes accounts, so don't add both to Dispatcharr.

### Networking

- Dispatcharr reaches ArchiveTV over the shared Docker network at `http://archivetv:8090`.
- The published port is `127.0.0.1:8090`, so the admin UI is **not** reachable from other machines. For LAN access, change the `ports:` line to your server's LAN IP. Never publish it on a public interface.
- If Dispatcharr runs on another host, set `ATV_PUBLIC_BASE_URL` to an address it can reach, bind the port to that LAN IP, and set `ATV_FEED_TOKEN`. The Xtream Codes routes are protected by the Xtream Codes credentials.

---

## Catch-up, Start Over and rewind

ArchiveTV keeps **programme history**, not recordings. A historical request works like this:
1. It is resolved to the programme that aired at that moment, from the stored schedule.
2. The programme is re-checked: still approved, source still authorised, eligible and healthy, and seekable when the request lands mid-programme.
3. The original source is replayed from the matching offset.

Each request gets its own FFmpeg pipeline and playhead, so catch-up never disturbs live viewers. Playback continues into the following programmes until the requested duration ends.

| Feature | How |
|---|---|
| Catch-up / historical seek | Dispatcharr calls `/timeshift/{user}/{pass}/{minutes}/{YYYY-MM-DD:HH-MM}/{id}.ts` (or `/streaming/timeshift.php`); times are UTC |
| Start Over | A catch-up request for the start of the programme on air, as the client sends it. Direct: `/startover/{channel}.ts` |
| Rewind | Whatever the client offers on top of catch-up requests. TiviMate rewinds by issuing a new timeshift request |
| Return to live | The client reopens the live stream |
| Direct (no Dispatcharr) | `/catchup/{channel}.ts?start=<unix or ISO>&duration=<minutes>` (`utc`/`utcend` aliases) |

- **Retention:** 1, 3, 7 (default), 14 or 30 days per channel. Older history is pruned by the schedule job.
- **Catch-up page:** the admin page lists past programmes with their current availability and active and recent sessions (with a Stop button). It also has a test that checks a historical time without streaming.
- **Failures:** a request outside the window, for an off-air period, or for a programme that is no longer approved or available returns 404 to Dispatcharr ("not available"); disabled catch-up returns 403. If a source fails mid-session, the rest of that programme shows an "unavailable" card and playback moves on. History is never edited.

## Minimal-disk architecture

`ATV_DISABLE_MEDIA_RECORDING=true` (the default) guarantees that no media is written anywhere:
- Sources are read over HTTP(S) straight into FFmpeg.
- FFmpeg writes MPEG-TS to a pipe.
- Output goes to bounded per-viewer RAM queues: 512 chunks, about 32 MiB, before a stalled viewer is dropped.
- A keyframe cache of up to 24 MiB serves viewers who join mid-stream.

There is no DVR, no timeshift buffer, no HLS segment cache, and the optional prefetch cache is refused. `/tmp` is a 256 MB tmpfs and stays empty in normal operation.

Persistent writes are limited to:
- the SQLite database: schedules, history, approvals and sessions, maintained daily and capped by `ATV_DB_MAX_MB` alerts
- daily backups
- size-limited Docker logs (3 × 10 MB)

**Monitoring:** the dashboard, `/health` and `/api/v1/system/status` report:
- CPU and memory, including FFmpeg
- RAM buffer bytes
- database, temp and volume usage
- FFmpeg process count
- alerts, also sent to `ATV_NOTIFY_WEBHOOK` when they first appear

## Workflow

### The quick way: paste links

1. **Channels → New channel.** A name is all it needs.
2. On the channel page, paste links into **Add videos**, one per line: Internet Archive items, YouTube videos or playlists, or a YouTube `@channel/live` address. Tick *I have permission to broadcast these* and press **Put on air**.

Each link is imported (or reused if it's already in the library), test-played with FFprobe, approved, its playable source authorised, and added to the channel. The schedule updates straight away. Links that can't air are listed with the reason, e.g. a failed playback check, or YouTube playback switched off in Settings.

The channel page lists everything on the channel with a plain **on air** or **⚠ reason** status. Reordering (▲▼), changing a role or removing an item rebuilds the upcoming schedule automatically; the programme on air now is kept.

Elsewhere:
- **Inbox:** what discovery rules found. **Put on air** and **Reject** work per item or on a selection. The fine-grained steps (approve only, authorise only, quarantine…) are under *More actions*.
- **Find videos:** discovery rules. A rule with **Put new finds on air automatically** skips the inbox: each new find that passes the playback check goes straight onto the rule's channels. Finds that fail the check still go to the inbox.

### Step by step

1. **Discovery sources:** add Internet Archive rules (keyword, collection, creator, subject, year range, licensed-only) or YouTube rules (search, `@handle` uploads, playlist, single video), each with filters and a scan frequency. Rules can feed channels directly. *Import* takes a single URL.
2. **Discovery** runs on schedule or on demand. It normalises metadata, groups Archive derivatives, flags duplicates, FFprobes the preferred file (codecs, duration, seekability) and queues everything as `PENDING_REVIEW`. It never approves anything.
   - **Per-rule preferences:** preferred formats (ordered, e.g. `h.264, mpeg4`) and preferred resolution decide which file is probed and aired.
   - **Skip validation:** queues items unprobed. Unvalidated sources can't be scheduled.
   - **Duplicates** are checked strongest first: an identical file (Archive SHA-1/MD5, or SHA-1 of local files), then the same media URL, then the same normalised title with matching episode and duration. Possible duplicates are flagged for review, never removed.
3. **Review queue:** preview, then approve, reject or quarantine, individually or in bulk. Rejected items stay excluded when rediscovered.
4. **Rights review:** mark validated sources `AUTHORISED`, `CATALOGUE_ONLY` or `RESTRICTED`.
5. **Channels:** assign approved items as programme, filler or ident; pick sequential, shuffle or block scheduling; set the catch-up window. You can upload a logo: PNG, JPEG or WebP up to 1 MB, checked by file signature; SVG is refused. It is stored in `/data/logos`, served at `/logos/…`, and used in every feed.
6. Schedules fill 7 days ahead and the channel appears in the feeds. Every change to a published schedule is kept as publication history on the Schedule page (`schedule_versions`): rebuilds, regenerations, overrides and failover replacements. Routine extensions are merged into one row per day.

## REST API

The versioned API lives at `/api/v1/…`, with OpenAPI docs at `/api/docs` and the schema at `/api/v1/openapi.json`. Authenticate with the admin session (`POST /api/v1/auth/login`) or `Authorization: Bearer $ATV_API_TOKEN`. Everything the admin UI does is available; the main routes are listed below, and the full list is in the OpenAPI docs.

```
GET  /api/v1/providers                    GET  /api/v1/streams/active
GET  /api/v1/discovery/rules              GET  /api/v1/system/status
POST /api/v1/discovery/rules              POST /api/v1/discovery/scan
GET  /api/v1/media[?status=&provider=&q=] GET  /api/v1/media/{id}
POST /api/v1/media/{id}/approve|reject|quarantine|revalidate
GET  /api/v1/channels                     POST /api/v1/channels      PATCH /api/v1/channels/{id}
GET  /api/v1/channels/{id}/schedule[?from=&to=]
POST /api/v1/channels/{id}/schedule/generate[?mode=extend|regenerate]
GET  /api/v1/catchup/programmes[?channel_id=]
GET  /api/v1/catchup/sessions             POST /api/v1/catchup/sessions   DELETE /api/v1/catchup/sessions/{id}
GET  /api/v1/dashboard | /meta | /catalogue | /sources | /settings | /events
POST /api/v1/channels/{id}/links         POST /api/v1/media/{id}/air    POST /api/v1/channels/{id}/items/order
POST /api/v1/media/bulk  (action "air" = approve + authorise + add to channel)
PATCH /api/v1/media/{id}                  POST /api/v1/media/{id}/sources
POST /api/v1/channels/{id}/items | /blocks | /logo                       GET  /api/v1/channels/{id}/schedule/versions
POST /api/v1/schedule/entries/{id}/override                              POST /api/v1/local/import
```

## YouTube Data API

YouTube discovery needs a free **YouTube Data API v3** key:
1. Create a Google Cloud project and enable the API.
2. Create an API key and restrict it to that API.
3. Put the key in `secrets/youtube_api_key` and restart.

Quota is 10,000 units a day by default, reset at midnight Pacific time:
- Search costs 100 units per page.
- Channel and playlist scans cost 1 unit per 50 videos.

Usage is tracked, a reserve is kept for manual imports, and an exhausted quota pauses YouTube discovery until the reset. The key is never stored in the database, sent to the browser or logged. Without a key, Internet Archive discovery and every channel feature work normally.

**Without a key**, pasting a single YouTube video link into *Import* still works through YouTube's official **oEmbed** endpoint (no key, no quota). It gives title, channel, thumbnail and the embedded player only:
- Duration, description and restrictions stay unknown, and the item is flagged.
- Embedding-disabled videos become reference-only; removed or private ones are marked unavailable.
- Search, channel and playlist rules need the Data API key, unless you opt into unofficial discovery (below).

### Experimental: airing YouTube videos (opt-in proof of concept)

`ATV_YOUTUBE_STREAMING=true` (or *Settings → Experimental: air YouTube videos*) deliberately departs from the v2.0 specification's YouTube rule. YouTube items then get a `youtube_stream` playback source, which yt-dlp resolves to YouTube's current stream addresses at play time. FFmpeg reads those directly, so nothing is downloaded or cached.

- **The same gates as any source:** the item must be approved, the stream source must pass validation (FFprobe), and you must explicitly authorise its rights. Authorise only videos you are allowed to rebroadcast; that decision is yours.
- **What it won't do:** log in or use cookies (so age-restricted, members-only and private videos fail), use proxies or bot-check workarounds, or play anything DRM-protected.
- **Format:** YouTube serves video and audio separately, so FFmpeg gets two inputs. H.264 up to `ATV_YOUTUBE_STREAM_MAX_HEIGHT` (default 1080, also in Settings) is remuxed, and audio is normalised to AAC 48 kHz. 1080p60 is about 5.5 Mb/s per channel; higher resolutions cost bandwidth, not CPU. Catch-up and Start Over work like any other source.
- **Fragility:** yt-dlp needs a JavaScript runtime for YouTube (Node is in the Docker image) and frequent updates, because YouTube changes often. When resolution fails, the usual failover moves the channel to its next eligible programme.
- **Turning it off** immediately makes every YouTube stream source ineligible again. Already-authorised rights are kept but have no effect.

#### YouTube Live (for example your own recurring stream)

Paste a channel's live address, `https://www.youtube.com/@yourchannel/live` (or `/channel/UC…/live`), into *Import*. It becomes a **live item** that always follows whatever that channel is broadcasting now, so a recurring stream that gets a new video ID each time keeps working. A single live video's link works too, while it's live.

- **Scheduling:** a live item has no length.
  - **In a time block,** it fills the block. Tag the item (it's tagged `live`) and add a block such as `Fireplace 20:00–23:00` with tag filter `live`.
  - **On a channel with nothing else,** it runs 24/7 in hourly slots.
- **Playback:** viewers join at the live edge, with no seeking or pacing. YouTube serves live video and audio as separate HLS playlists, and both are combined as for ordinary videos.
- **When you're not live:** the source is marked offline and the channel falls back to its fallback programme, other eligible content, or the "off air" card. A background job rechecks offline live sources every minute, and once you go live it reschedules the channel automatically.
- **Catch-up:** live programmes are never recorded, so they can't be replayed. Catch-up requests for them return "not available", and the guide lists them under the category "Live".
- **Upcoming broadcasts** (scheduled but not started) count as offline until they begin.

### Unofficial discovery without a key (opt-in)

`ATV_YOUTUBE_UNOFFICIAL=true` (or *Settings → Unofficial YouTube discovery*) uses [ytscrape](https://github.com/vsmutok/ytscrape) for **keyword search, channel rules and single videos** while no API key is set. ytscrape reads YouTube's private InnerTube API.

- **The official API wins:** once you add a key, ArchiveTV switches to the official Data API and refreshes everything from it.
- **Metadata only:** title, channel, duration, description, keywords, category, availability. Stream URLs are never read. Items get the same embed-only or reference-only status as officially discovered ones, so they can never air. They are also flagged as coming from the unofficial API.
- **Polite by design:** one request at a time, at least `ATV_YOUTUBE_UNOFFICIAL_INTERVAL` seconds apart (default 2). No proxies and no bot-detection workarounds. If YouTube refuses (bot check, captcha, rate limit, consent wall), the scan stops with a clear error on the dashboard.
- **Channels:** `@handles` are resolved through channel search, accepting only an exact handle match. `/channel/UC…` URLs are used directly.
- **Playlists:** not supported in this mode.
- **Risk:** this is not an official interface. ytscrape's own README warns it may conflict with YouTube's Terms of Service, and it can break when YouTube changes. Prefer a free API key.

## Operations

- **Health:** `GET /health` (also the Docker healthcheck). Logs are JSON on stdout.
- **Schema:** Alembic migrations run automatically at start-up (`archivetv/migrations`).
- **Background jobs:** run on APScheduler (`archivetv/jobs.py`): schedule upkeep, discovery, health checks, YouTube refresh, monitoring, database maintenance and backups. Each job runs at most once at a time, and missed runs are coalesced. Last and next run times are in `/health`.
- **Backups:** SQLite online backup daily to `/data/backups`, or on demand from Settings.
- **Recovery:**
  1. Stop the container.
  2. Copy a backup over `/data/archivetv.sqlite3` and delete the `-wal`/`-shm` files next to it.
  3. Start the container. Everything, including programme history, lives in that one file. Uploaded logos are in `/data/logos`, so back up the whole `/data` volume to keep them.
- **Generated secrets:** the admin and Xtream Codes passwords are generated on first start if unset, and stored in `/data/run/`.

## Development

```bash
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt pytest
```

```bash
.venv/bin/python -m pytest -q
```

```bash
ATV_DATA_DIR=./data ATV_MEDIA_DIR=./media ATV_ADMIN_PASSWORD=dev .venv/bin/python -m archivetv.main
```

```bash
PYTHONPATH=. .venv/bin/alembic -c archivetv/migrations/alembic.ini revision --autogenerate -m "describe change"
```

Frontend (Node 20+). `npm run dev` serves on :5173 and proxies to the back end on :8090; `npm run build` writes `archivetv/web/app/`:

```bash
cd frontend && npm ci && npm run dev
```

```bash
cd frontend && npm run build
```

To add a provider, subclass `archivetv/providers/base.py:MediaProvider` and register it in `archivetv/providers/__init__.py`. Adapters only describe content and its delivery options; they never schedule or publish.

## Layout

```
archivetv/
  providers/   base.py (interface), archive.py, youtube.py
  catalogue.py approval, rights, eligibility, duplicate detection
  discovery.py discovery engine + provider-agnostic filters
  validator.py FFprobe validation, health checks, vetted playback targets
  scheduler.py deterministic schedules + history retention
  stream.py    shared live MPEG-TS engine
  catchup.py   schedule-based catch-up / Start Over sessions
  xtream.py    Xtream Codes-compatible upstream for Dispatcharr
  api.py       /api/v1 REST API (catalogue, channels, catch-up, system)
  api_admin.py /api/v1 admin endpoints and session auth used by the React UI
  logos.py     uploaded channel logos
  feeds.py     M3U + XMLTV (with history)
  monitor.py   storage / memory / process monitoring and alerts
  security.py  URL validation, SSRF protection, host allowlist
  jobs.py      background jobs
  migrations/  Alembic
  web/         classic server-rendered admin (/admin); built React app in web/app (/app)
frontend/      React + TypeScript admin UI (Vite)
docs/PHASE0_FEASIBILITY.md   Phase 0 technical feasibility report
```

## Known limitations

- **Remux joins:** a remuxed (`auto`/`copy`) stream joined mid-programme, live or catch-up, starts at the keyframe before the requested point, typically 0–4 s early.
  - **Failover joins and local files:** the keyframe is probed, so timestamps continue and audio and video start together.
  - **First join on a remote source:** the probe is skipped to keep start-up fast, so video can lead audio briefly at tune-in.
  - `playback_format=transcode` is exact in every case.
- **Byte-range seeking:** catch-up streams are generated on the fly, so they have no `Content-Length` and ignore byte-range requests. Seeking inside catch-up works the XC way, with a new timestamp request, which is what Dispatcharr and TiviMate do.
- **Timestamp precision:** Dispatcharr drops seconds from catch-up timestamps. ArchiveTV snaps those requests to a programme that starts within the minute, so Start Over and catch-up begin at the programme's real start. A deliberate seek through Dispatcharr is accurate to the minute.
- **HLS sources:** only the playlist URL is validated; FFmpeg fetches the segments itself.
- **Scale:** a single admin account, SQLite and a single process, sized for a home or small-lab server.
- **Not yet verified:**
  - Catch-up through the running Dispatcharr and the intended IPTV client (TiviMate). See the feasibility report.
  - The YouTube adapter against the live API.
