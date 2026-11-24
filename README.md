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

Approving an item never makes it IPTV-eligible by itself. The one automatic path is a [channel pack](#channel-packs) with the `auto_air` policy, and only for finds with a public-domain or Creative Commons licence; every such approval is logged against the pack.

**YouTube airs like any other source.** YouTube items get a `youtube_stream` playback source (resolved by yt-dlp at play time, see [Airing YouTube videos](#airing-youtube-videos)) alongside the official embedded player for previews. They go through the same gates: approval, validation and authorised rights. `ATV_YOUTUBE_STREAMING=false` makes YouTube discovery-only again.

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
- The published port is `127.0.0.1:8090`, so the admin UI is **not** reachable from other machines. For your lab network, change the `ports:` line to the server's LAN IP: that's what the "public" guide API and HLS streams are for. "Public" in this README means viewers on your lab network, not the internet. Never publish the service on an internet-facing interface.
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
- Sources are read over HTTP(S) into FFmpeg, through an in-RAM [parallel fetcher](#parallel-fetching).
- FFmpeg writes MPEG-TS to a pipe.
- FFmpeg may run up to `ATV_READAHEAD_SECONDS` (default 20) ahead of real time. That output waits in a per-channel RAM buffer, capped at 64 MiB, and is released to viewers at playback speed. A slow or briefly stalled origin drains this buffer instead of making viewers buffer. Set it to 0 to turn read-ahead off; it is also under *Settings*.
- Output goes to bounded per-viewer RAM queues: 512 chunks, about 32 MiB, before a stalled viewer is dropped.
- A keyframe cache of up to 24 MiB serves viewers who join mid-stream.

There is no DVR, no timeshift buffer, no HLS segment cache, and the optional prefetch cache is refused. `/tmp` is a 256 MB tmpfs and stays empty in normal operation.

Persistent writes are limited to:
- the SQLite database: schedules, history, approvals and sessions, maintained daily and capped by `ATV_DB_MAX_MB` alerts
- daily backups
- size-limited Docker logs (3 × 10 MB)

### Parallel fetching

archive.org limits how fast each connection downloads, and the limit moves around during the day. A programme can stutter even when the speed check passed it: About Bananas measured 7× its bitrate, then dropped below it for minutes at a time. So FFmpeg reads every remote file through a small relay inside ArchiveTV instead of straight from archive.org:
- The relay splits the file into 512 KiB pieces and fetches the next 8 MiB over `ATV_PARALLEL_CONNECTIONS` (default 4) simultaneous range requests. FFmpeg gets each byte as soon as it arrives.
- Pieces are held in RAM only (about 10 MiB per programme) and dropped once played, so this works in minimal-disk mode. Each byte is still downloaded once.
- Tuning in, joining mid-programme and catch-up work as before: a seek is just a range request to the relay.
- An origin that can't serve byte ranges is read directly. Set the value to 1 (also under *Settings*) to turn the relay off.

Against archive.org, four connections fetched the 3.7 Mbps AV1 test clip at 15 Mbps, against 5.4 Mbps on one connection.

### Pre-downloading to disk (optional fallback)

For a source still too slow with parallel connections, turn minimal-disk mode off (`ATV_DISABLE_MEDIA_RECORDING=false`) and set `ATV_CACHE_SLOW_SOURCES=true`. Sources flagged **slow source** are then also written to disk:
- Only channels someone is watching are affected. From the moment you tune in, the channel's slow sources are downloaded to `data/cache` before they air: up to `ATV_CACHE_LEAD_HOURS` (default 3) ahead, or earlier when the measured speed means a file needs longer. Downloads run two at a time, soonest airing first.
- A slow source that is already downloaded airs from disk. One that isn't streams through the relay. With the relay turned off, the channel instead shows a "downloading, back shortly" card, checks again every 30 seconds, and joins the programme at its scheduled position once the file is ready.
- Each file is deleted 10 minutes after its airing ends, unless it airs again within the lead time. When everyone stops watching a channel, its downloads are cancelled and its files deleted within about two minutes.
- Sources whose *Allow cache* is off are never downloaded.

`ATV_CACHE_ENABLED=true` additionally caches the next two programmes of channels being watched, whatever their speed. Files are then kept until `ATV_CACHE_MAX_GB` (default 20) evicts the oldest. Both options are under *Settings*.

**Monitoring:** the dashboard, `/health` and `/api/v1/system/status` report:
- CPU and memory, including FFmpeg
- RAM buffer bytes
- database, temp and volume usage
- FFmpeg process count
- alerts, also sent to `ATV_NOTIFY_WEBHOOK` when they first appear

## Workflow

1. **Discovery sources:** add Internet Archive rules (keyword, collection, creator, subject, year range, licensed-only) or YouTube rules (search, `@handle` uploads, playlist, single video), each with filters and a scan frequency. Rules can feed channels directly. *Import* takes a single URL.
2. **Discovery** runs on schedule or on demand. It normalises metadata, groups Archive derivatives, flags duplicates, FFprobes the preferred file (codecs, duration, seekability) and queues everything as `PENDING_REVIEW`. It never approves anything.
   - **Per-rule preferences:** preferred formats (ordered, e.g. `h.264, mpeg4`) and preferred resolution decide which file is probed and aired.
   - **Skip validation:** queues items unprobed. Unvalidated sources can't be scheduled.
   - **Speed check:** validation also downloads a remote file for `ATV_SPEED_CHECK_SECONDS` (default 6; 0 skips it) and compares the measured speed with the file's bitrate. A source slower than bitrate × `ATV_MIN_SPEED_RATIO` (default 1.5) is marked **slow source** on the Sources and item pages, and an item airs from a faster source when it has one. Slow sources can still air, and the read-ahead buffer covers short dips. One that is slower than its bitrate on average will stall anyway, which is what [parallel fetching](#parallel-fetching) is for; otherwise replace it or attach a faster copy.
   - **Duplicates** are checked strongest first: an identical file (Archive SHA-1/MD5, or SHA-1 of local files), then the same media URL, then the same normalised title with matching episode and duration. Possible duplicates are flagged for review, never removed.
3. **Review queue:** preview, then approve, reject or quarantine, individually or in bulk. Rejected items stay excluded when rediscovered.
4. **Rights review:** mark validated sources `AUTHORISED`, `CATALOGUE_ONLY` or `RESTRICTED`.
5. **Channels:** assign approved items as programme, filler or ident; pick sequential, shuffle or block scheduling; set the catch-up window. You can upload a logo: PNG, JPEG or WebP up to 1 MB, checked by file signature; SVG is refused. It is stored in `/data/logos`, served at `/logos/…`, and used in every feed.
6. Schedules fill 7 days ahead and the channel appears in the feeds. Every change to a published schedule is kept as publication history on the Schedule page (`schedule_versions`): rebuilds, regenerations, overrides and failover replacements. Routine extensions are merged into one row per day.

## Channel packs

A channel pack is a shareable recipe that builds a channel and keeps it filled. It is a plain JSON file holding discovery rules (Internet Archive and YouTube), time blocks, a look, a rights policy and a target size. Install one from **Channel packs** in the admin UI: ten starter packs are bundled, or paste or link your own. Installing creates the channel, its rules and blocks, draws its logo and starts scanning at once.

```json
{
  "id": "monster-movies", "name": "Monster Movies", "number": 13,
  "look": "creature", "logo_text": "MONSTER", "break_style": "bumper",
  "order": "shuffle", "policy": "auto_air", "target_hours": 150,
  "theme": "Monster, creature and B-movie horror features",
  "public_domain_collections": [],
  "rules": [
    {"provider": "archive", "query": "monster OR creature",
     "params": {"collection": "feature_films", "min_duration": 3600, "max_duration": 10800,
                "excluded_keywords": "trailer, sample, clip", "preferred_formats": "h.264, mpeg4"}},
    {"provider": "youtube", "query": "creature feature full movie",
     "params": {"creative_commons_only": true, "min_duration": 3600}, "policy": "inbox"}
  ],
  "blocks": [{"name": "Creature Feature", "days": "45", "start": "22:00", "end": "02:00", "tag_filter": "horror"}]
}
```

- **Self-filling.** Every 30 minutes the pack compares the channel's playable hours with `target_hours`. Below the target its rules scan, and an Archive rule moves one page further into its results whenever a page held nothing new. At or above the target the rules rest, with a check every 6 hours. Sources that disappear drop out through the normal health checks, so the channel refills itself. Pack rules are run by the pack, not by the discovery schedule.
- **Rights policy.** With `inbox`, every find waits in the review queue. With `auto_air`, a find goes on air without review only when there is evidence it may be rebroadcast:
  - a public-domain or Creative Commons licence on the Archive item
  - membership of a collection the pack lists in `public_domain_collections`
  - a Creative Commons YouTube upload (with YouTube streaming on, the default)

  Anything else goes to the review queue, flagged with the reason. Auto-approvals and authorisations are written to the audit log as `pack:<id>`. A rule can override the pack's policy with its own `policy`.
- **Lab mode.** On a private lab network, *Settings → Lab mode: auto-air packs air every find* (`ATV_PACKS_AIR_EVERYTHING=true`) drops the licence check. Every auto-air pack then puts every playable find on air, Internet Archive and YouTube alike. Only finds that can't play are held: items the Archive restricts, and YouTube videos while YouTube streaming is off. Switching it on immediately airs what auto-air packs were holding for review, and the weekly re-check no longer pulls tapes back over licences (tapes that are gone still drop out). Packs set to review first still send everything to review.
- **Mixing sources.** A pack's rules can be any mix of Internet Archive and YouTube. Their finds share one channel pool, and the shuffle draws from all of it, so the mix on air follows the pool. To lean one way, give that provider's rules a larger `max_results` or a broader query, or follow more channels.
- **Curated YouTube channels.** YouTube keyword searches are expensive: 100 quota units each, and YouTube also caps them per day. So a pack uses them to *find channels*, and you curate:
  1. The channels behind a pack's search finds appear as **suggestions** on the pack (*YouTube channels*), with how many finds each produced, example titles and, with an AI backend set, its verdict on whether the channel fits.
  2. You **Follow**, **Dismiss** or ask the AI again. You can also follow a channel directly by `@handle` (needs an API key or unofficial discovery) or channel URL (no key needed). Nothing is followed automatically.
  3. A followed channel's uploads are harvested once in depth (200 videos for about 8 units), relisted weekly, and between relists checked for new uploads through its public RSS feed, which is free and needs no key. Its finds go through the same validation, clean-up and pack policy as everything else.
  4. Searches share a daily budget across all packs (`ATV_YOUTUBE_SEARCH_BUDGET`, default 30). A pack's search runs at most every 12 hours, and only weekly once it follows three or more channels.
- **Theme check (optional).** With an [AI backend](#ai-helpers) set, a pack can check each find's title, description and subjects against its `theme` before it airs. Off-theme finds, refusals and errors all go to review, never on air.
- **Weekly freshness.** Every `refresh_days` (default 7, per pack) a pack refreshes itself, like spelunkr's weekly check:
  1. **Re-checks every tape** against its provider, Internet Archive or YouTube, fetching fresh metadata. A tape that's gone is marked unavailable and taken off the schedule. One the pack put on air whose licence no longer backs it (for example, the uploader removed a Creative Commons licence) loses its authorisation and goes back to review. Tapes a person approved are left alone. Files are also health-checked every few hours between refreshes.
  2. **Re-applies the policy** to the pack's finds still waiting for review. A Creative Commons YouTube video found while YouTube streaming was off gets its stream source, is validated and goes on air.
  3. **Looks for new uploads**: every rule scans its newest results (Archive by date added, YouTube by upload date), even when the channel is full.
  4. **Rotates**: the most-aired `rotate_percent` (default 15%) of the channel's tapes rest for four refreshes, then come back. The channel never drops below half its target, and the schedule is rebuilt from the first slot a rested tape held. Resting tapes stay approved in the catalogue. If the rotation leaves the channel short, the next fill tops it up with new finds.

  The Packs page shows each pack's last refresh (checked, gone, new, rested, back) and the next one. **Refresh now** runs it at once, and every refresh is in the audit log as `pack_refresh`.
- **Sharing.** Export any installed pack (with your target, policy and blocks) and import it elsewhere from a file or an https URL. Installing a newer `version` of a pack you have upgrades it in place: the channel, its programmes, its history and your own settings (policy, target, refresh, rotation) are kept, and finds still waiting for review stay with the pack. Starter packs show an **Upgrade** button when a newer version ships.

"Auto-air" with lab mode off means *licensed* finds air; with lab mode on, every playable find airs. "Review" packs always wait for you: switch a pack's *Finds* setting to make it auto-air.

| Starter pack | Sources | Policy | Look |
|---|---|---|---|
| Monster & Sci-Fi | Archive `SciFi_Horror`, `feature_films` + YouTube classic horror | auto-air | creature |
| Cartoons | Archive `classic_cartoons`, `animationandcartoons` + YouTube vintage cartoons | auto-air | toon |
| Classroom Films | Archive `prelinger` (declared public domain), `educationalfilms` + YouTube | auto-air | classroom |
| Westerns | Archive `feature_films` (westerns) + YouTube | auto-air | frontier |
| Film Noir | Archive `Film_Noir` + YouTube | auto-air | noir |
| Newsreels | Archive `universal_newsreels` (declared public domain) + YouTube | auto-air | newsreel |
| Old Commercials | Archive `classic_tv_commercials` + YouTube | review | vhs |
| Sitcoms | Archive `classic_tv` + YouTube | review | vhs |
| Saturday Morning | Archive `vhsvault` home recordings + YouTube | review | toon |
| Live Music | YouTube, Creative Commons uploads only | review | concert |

## Break cards, bumpers, looks and logos

With breaks on (channel settings, and on by default for pack channels), a short generated break airs before each programme, 12 seconds by default. FFmpeg draws it live; nothing is stored. There are two styles:

- **Animated bumper**, in the kinetic “Coming up next” tradition of late-90s youth channels. “COMING UP” and “NEXT!” slam in and settle, and a frame bounces up playing a few seconds of the next show, read from a quarter of the way into it. The title slab, start time and a “THEN / LATER” strip slide in, the channel's logo sits in the corner, and a wipe sweeps out into the programme. A bumper that previews a show gets the same read-ahead as a programme, so a slow origin can't stall it. If the preview can't be read, the bumper is redrawn with the show's thumbnail panning slowly instead.
- **Lineup card**: a calm “UP NEXT” card listing the next three programmes with times, thumbnails and titles beside leaning bars.

The break style is a channel setting (also a pack field `break_style`). “Automatic” gives bumpers to the creature, toon, vhs and concert looks, and the lineup card to the rest. Thumbnails are fetched only from archive.org and YouTube's image hosts, kept in RAM and skipped if slow. The footer credits where the tapes come from (“via archive.org”, “via YouTube”).

- **Music.** Put Creative Commons or public-domain tracks in `media/music` (`ATV_MUSIC_DIR`), with the on-screen credit as the first line of a sidecar file `<track>.txt`. Breaks rotate through them. Without any, breaks play a generated chord bed (soft pads on cards, plucked stabs on bumpers), which needs no licence.
- **Looks.** A channel's look sets the colours, type, bars and finish of its breaks and of its drawn logo: `broadcast`, `creature`, `toon`, `noir`, `frontier`, `newsreel`, `classroom`, `vhs` and `concert`. “Automatic” uses the pack's `look`, else a look matched from the channel's name and description, else `broadcast`. The channel page shows a preview still. Docker installs DejaVu Sans and Serif Condensed; `ATV_CARD_FONT` takes a fontconfig name or a `.ttf`/`.otf` path, for example an OFL display face in `/data`.
- **Logos.** Every channel has a logo. An uploaded file wins, then a logo URL (the channel's own or its pack's). Otherwise ArchiveTV draws a station mark in the channel's look: a 512×512 transparent PNG with a shape, a wordmark and the channel number. The marks are leaning slabs (broadcast), a chunky shadowed block (toon), a glowing ring (creature), a framed plate (noir), a starred badge (frontier), a film countdown leader (newsreel), a numbered pill (classroom), sheared chrome type over stripes (vhs) and a record (concert). The wordmark is the pack's `logo_text` (e.g. `"NOIR"`), a short channel name, or its initials. A drawn logo is redrawn when the name, number or look changes.
- Breaks are folded into the programme before them in XMLTV and the public guide. Catch-up and Start Over requests that land on a break mean the programme around it.

## AI helpers

One setting, *Settings → AI backend* (`ATV_AI_BACKEND`), powers every AI job:

- `local`: any OpenAI-compatible server on your lab network, such as [Ollama](https://ollama.com), LM Studio or llama.cpp's server. Set `ATV_AI_BASE_URL` (e.g. `http://ai-box:11434/v1`) and `ATV_AI_MODEL` (e.g. `qwen2.5:7b-instruct`; a 7–14B instruct model is plenty). It's free per call and private, so it can run on every find. ArchiveTV asks for JSON matching a schema (`response_format: json_schema`), falls back to plain JSON mode for servers without schema support, and sends at most two requests at a time. `ATV_AI_API_KEY` is only needed if your server wants one.
- `anthropic`: Claude, with `ANTHROPIC_API_KEY` and `ATV_THEME_CHECK_MODEL` (default `claude-opus-5-5`), at low effort with server-side refusal fallback.
- `none` (the default without a Claude key): no AI; everything else works as before.

What it does:

- **Clean-up** (*AI: clean up and tag new finds*, `ATV_AI_ENRICH`, on by default once a backend is set). Every new find is tidied before review:
  - The title is cleaned: "Plan 9 From Outer Space (1959) [HD] FULL MOVIE" becomes *Plan 9 from Outer Space*, with year 1959.
  - Series, season and episode numbers are filled in, and genre tags are added so time blocks work for YouTube finds too.
  - The video is classified, and clips, trailers and reaction videos are held for review even in lab mode.
  - Cleaned titles let the duplicate check spot the same film on both the Archive and YouTube.

  The clean-up survives provider refreshes until the uploader renames the video. Items an admin has edited are left alone. *Settings → Clean up existing catalogue* runs it over earlier finds in the background.
- **Pack theme checks** and **vetting suggested YouTube channels** (above).

Item metadata comes from public uploads, so it is always passed to the model as data, never as instructions. Answers are validated and bounded before they touch the catalogue, and a failure or unclear answer is never taken as a yes. *Settings → Test connection* checks the backend.

## HLS output

Every channel is also available as HLS at `/hls/<channel>/index.m3u8` (`/playlist.m3u?format=hls` lists them all). Xtream Codes clients set to HLS output (`/live/<user>/<pass>/<id>.m3u8`) get it too. The HLS stream is one more viewer of the channel's shared stream, so the origin is fetched once whatever the mix of viewers. It is cut at keyframes into `ATV_HLS_SEGMENT_SECONDS` (default 4) segments, and the last `ATV_HLS_WINDOW` (default 6) are held in RAM. Programme changes are marked `#EXT-X-DISCONTINUITY`. When nobody has requested a playlist or segment for a while, the HLS viewer leaves.

## Public guide API

Read-only JSON for a web player or app on your lab network, the way spelunkr.tv works. It follows `ATV_FEED_TOKEN` like the feeds, sends `Access-Control-Allow-Origin: *`, and can be switched off with `ATV_PUBLIC_GUIDE=false`.

| Endpoint | Returns |
|---|---|
| `GET /api/public/channels` | every channel with now, next, logo and stream URLs |
| `GET /api/public/guide?hours=6&from=<ISO>` | the guide grid (max 48 hours) |
| `GET /api/public/channels/<slug>/now` | the programme on air, how far in it is, and how a browser can play it directly |

`playback` in `/now` lets a browser play the programme straight from its source, with no video through your server:
- `{"type": "file", "url": "https://archive.org/download/…", "offset": 1234.5}`: seek the Archive file to `offset`
- `{"type": "youtube", "video_id": "…", "start": 1234, "embed_url": "…"}`: the official embed

It is `null` when only the relayed stream (`streams.hls` / `streams.ts`) can play it. Everyone who asks at the same moment gets the same offset, so browser viewers and IPTV viewers watch the same thing at the same second. Browser viewers then use the Archive's bandwidth directly, as on archive.org, so consider pointing them to the Internet Archive's donation page.

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
POST /api/v1/media/bulk                   PATCH /api/v1/media/{id}       POST /api/v1/media/{id}/sources
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

### Airing YouTube videos

YouTube streaming is on by default (`ATV_YOUTUBE_STREAMING`, or *Settings → Air YouTube videos*). YouTube items get a `youtube_stream` playback source, which yt-dlp resolves to YouTube's current stream addresses at play time. FFmpeg reads those directly, so nothing is downloaded or cached.

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
- **Metadata only:** title, channel, duration, description, keywords, category, availability. ytscrape never reads stream URLs; airing works exactly as for officially discovered videos (the yt-dlp stream source, with YouTube streaming on). Items are flagged as coming from the unofficial API. ytscrape doesn't report a video's licence, so outside lab mode an auto-air pack sends these finds to review; in lab mode they air like any other find.
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
