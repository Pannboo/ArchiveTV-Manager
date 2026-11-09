# Plan — Curated creator channels over IPTV

ArchiveTV Manager 2.x · draft, 9 November 2026 · status: **proposal, nothing built yet**

## Goal

Build an open-source equivalent of the react.tv idea: people curate web video (YouTube, Twitch, Internet Archive) into 24/7 channels, and everyone watching a channel sees the same moment. Optionally, a creator can react live, shown beside the scheduled video.

**The hard requirement:** every channel must play as a normal IPTV channel through Dispatcharr (Xtream Codes, live + catch-up), just like today's ArchiveTV channels. A web player is a companion, not the delivery path.

## What already exists

Most of the pipeline is already built and verified (see `PHASE0_FEASIBILITY.md`):

| Need | Already in ArchiveTV |
|---|---|
| Discover and import videos | `providers/archive.py`, `providers/youtube.py`, discovery rules, *Import* by URL |
| Human approval and rights gate | `MediaItem.review_status`, `PlaybackSource.rights_status`, `catalogue.source_is_iptv_ok` |
| Deterministic 24/7 schedule | `scheduler.py` (sequential / shuffle / time blocks, fallback, failover) |
| "Everyone in sync" | Inherent: one shared FFmpeg pipeline per channel, locked to the wall-clock schedule (`stream.ChannelStream`) |
| IPTV delivery + EPG + catch-up | `xtream.py`, `feeds.py`, `catchup.py` |
| YouTube videos airing over IPTV | Experimental `youtube_stream` source: yt-dlp resolves at play time, FFmpeg gets a video + audio pair (`ytstream.py`, `stream._cmd`) |
| YouTube Live as a source | Live sources: joined at the live edge, block-filling, offline fallback, never in catch-up |

**The gaps:** Twitch, a fast "curate a channel from a list of links" flow, a web companion with chat, reaction programmes (compositing two feeds), and more than one curator.

## The rights question (read first)

react.tv is legitimate largely because it **embeds** the official players, so views, ads and analytics go back to the creator. IPTV can't do that. Airing a YouTube or Twitch video over IPTV means **re-streaming** it, and both platforms' terms forbid that without the rights holder's permission.

This plan keeps ArchiveTV's existing rule: nothing airs until an admin has approved the item **and** explicitly marked its source `AUTHORISED`. The realistic sources for IPTV are:
- your own channels and VODs
- creators who opt in, ideally recorded as written permission in `rights_note` / `license_url`
- Creative Commons and public-domain material (Internet Archive)

The web companion (Phase 4) can still use the official embeds for everything, which is the react.tv model.

Recommendation: keep YouTube and Twitch streaming opt-in (`ATV_YOUTUBE_STREAMING`, a new `ATV_TWITCH_STREAMING`), and add a per-source "permission evidence" field before calling any of this production-ready.

---

## Phase 1 — Generic stream resolvers + Twitch playback

**Outcome:** a Twitch VOD, clip or live channel can be approved, authorised and aired over IPTV exactly like a YouTube stream source today.

1. **Generalise `ytstream.py` into `archivetv/resolvers/`:**
   - `base.py` defines a `Resolved` dataclass (video, optional audio, user agent, codecs, height, duration, `live`, expiry) and a `StreamResolver` interface: `matches(url)`, `cache_key(url)`, `resolve(url, fresh)`, `forget(url)`.
   - `youtube.py` holds today's code, moved without behaviour changes.
   - `twitch.py` is new and uses yt-dlp's Twitch extractors, so no new dependency is needed.
   - `validator.playable_target` and `stream._resolve` call through a registry instead of special-casing `PlaybackType.YOUTUBE_STREAM`. `stream._handle_failure` calls the resolver's `forget`.
2. **Enums and migration:**
   - Add `PlaybackType.TWITCH_STREAM` and `ProviderType.TWITCH`.
   - Add the type to `catalogue.IPTV_TYPES`, gated by a generalised `stream_type_allowed(ps)`.
   - Migration `0005_twitch_sources` needs no new columns: the types are strings.
3. **Twitch specifics the resolver must handle:**
   - **Muxed HLS:** Twitch serves video and audio in one HLS stream, so there is a single FFmpeg input (`audio=None`). VODs are usually H.264 + AAC and can be remuxed (`IPTV_READY`).
   - **Hosts:** live playlists come from `usher.ttvnw.net` / `*.hls.ttvnw.net`. VOD and clip CDNs vary (CloudFront and other domains), so the resolver must vet hosts with the same rule as `ytstream.STREAM_HOSTS`, using a Twitch host list. The resolver should log any unknown host it refuses, so the list can be kept current.
   - **Mid-roll ads on live channels:** these insert discontinuities and resolution changes. Force `transcode` for Twitch live sources, the way `PlaybackFormat.AUTO` already transcodes `IPTV_NEEDS_TRANSCODE`, so the TS output stays continuous.
   - **Expiring VODs:** Twitch deletes past broadcasts after 7–60 days. The existing health-check job (`jobs.py`) re-checks them, a failure takes the source out of rotation, and the schedule fails over.
   - **Muted VOD segments:** silent audio stays valid output; flag the item for review.
   - **Out of scope, as for YouTube:** no logins, OAuth user tokens, cookies, subscriber-only VODs or ad-block workarounds.
4. **Live channel address:** `https://www.twitch.tv/<login>` works like YouTube's `/@handle/live`. It's a live item that always follows the current broadcast, is offline when they aren't live, and is rechecked every minute by the existing live-recheck job.
5. **Tests:** resolver unit tests with a mocked yt-dlp `extract_info`, plus a `stream._cmd` test for the single-input + transcode path. Then a live capture check, as in Phase 0: 0 backwards timestamps, 0 gaps and 0 decode errors through Dispatcharr.

## Phase 2 — Twitch discovery provider

**Outcome:** discovery rules can find Twitch content, like the YouTube rules.

- Add `providers/twitch.py`, a `MediaProvider` on the official **Helix API**:
  - an app access token via client credentials, stored in `secrets/twitch_client_secret`, never in the database
  - `ATV_TWITCH_CLIENT_ID` in config
- **Rule kinds:**
  - `channel`: a broadcaster's archives, highlights and uploads (`/videos?user_id=`)
  - `video`: a single VOD or clip
  - `search`: channel search (`/search/channels`)
  - live channel: one item, `live=true`
- **Rate limits:** Helix token-bucket rate limits get handled the way `quota.py` handles YouTube quota, by pausing and reporting on the dashboard.
- **Without credentials:** pasting a single Twitch URL into *Import* still works through yt-dlp metadata only (title, duration, thumbnail), like YouTube's oEmbed path. Search and channel rules need the API.
- Items enter as `PENDING_REVIEW` with a `twitch_stream` source that stays `UNVERIFIED` until FFprobe validation, which is the same flow as every other provider.

## Phase 3 — "Build a channel from links" flow

**Outcome:** the react.tv-style experience of curating a channel quickly, without weakening the approval and rights gates.

- **Channel builder page** in the React UI (`frontend/src/pages/`):
  1. Paste a list of URLs: YouTube videos, playlists or `/live`, Twitch VODs, clips or channels, Archive items.
  2. ArchiveTV imports and validates them in the background and shows each one's eligibility.
  3. The curator ticks the ones to air and confirms **approve + authorise rights** per source. Each one gets an audit entry and a required permission note.
  4. They're added to the channel in the given order.
- **Ordered playlist mode:** `ScheduleMode.SEQUENTIAL` already loops in position order. Add drag-to-reorder backed by `ChannelItem.position` and a "loop / play once then fallback" toggle.
- **Pinned programmes:** a "watch party at 20:00" is a one-off `ScheduleEntry` override at a fixed time. `scheduler.override` exists today for replacing an entry; extend it to insert at a time and push the rest of the schedule later.
- **Two-week horizon:** `ATV_SCHEDULE_DAYS` already exists, so raise its default (or set it per channel) to 14 to match react.tv's two-week guide.
- **API:** `POST /api/v1/channels/{id}/import-links` returns a job id; `GET /api/v1/jobs/{id}` returns per-link results. Everything stays usable through the REST API, as in the rest of ArchiveTV.

## Phase 4 — Web companion (`/watch/{slug}`)

**Outcome:** a public page that shows the same channel in a browser, in sync with the IPTV feed and with chat. IPTV stays the primary delivery path.

- **Player choice, per channel:**
  - **Official embeds (default):** the page asks `GET /api/v1/public/channels/{slug}/now` for the current entry and offset, then loads the YouTube IFrame or Twitch embed and seeks to `now − entry.start`. It re-syncs on each programme change and every 30 s, correcting drift above 2 s. Views go to the creator, and this works even for items that are only `EMBED_ONLY`, so the web page can show a superset of the IPTV channel. Make that difference visible in the UI.
  - **The IPTV output itself:** this would need HLS, but the minimal-disk rule forbids an on-disk segment cache. An in-RAM HLS packager (a rolling window of about 6 × 2 s segments per channel, in memory) is possible but is a separate decision. Defer it.
- **Guide:** now/next and a day grid from the existing schedule API, as a read-only public subset.
- **Chat:** a WebSocket per channel with in-memory ring buffers (no persistence, matching the minimal-disk rule), anonymous nicknames, rate limits and admin moderation (delete message, ban by session). IPTV clients can't show chat. An optional chat burn-in overlay on the IPTV feed is possible later, but it forces transcoding; see Phase 5.
- **Security:**
  - The public routes are separate from the admin API and are off by default (`ATV_PUBLIC_WATCH=false`).
  - They are read-only and expose only channels marked `public`.
  - Keep `127.0.0.1` binding as the default; document a reverse proxy for public exposure.

## Phase 5 — Reaction programmes (composited)

**Outcome:** a creator reacts live to the scheduled video, and IPTV viewers see one composited picture: the video with the reactor's camera beside it or overlaid.

- **How it works over IPTV:**
  - A new kind of segment with **two inputs**: the scheduled programme at its offset, plus the reactor's live feed.
  - FFmpeg composites them with `-filter_complex` (layout presets: picture-in-picture corner, side-by-side `hstack`, 70/30 split) and mixes audio (`amix` with ducking: programme at −10 dB while the reactor speaks, using `sidechaincompress`).
  - `stream._cmd` already handles multiple inputs (the YouTube video + audio pair). This extends it to a filter graph.
  - Compositing **always transcodes**, so CPU cost is about one 720p/1080p x264 encode per reacting channel. Document sizing.
- **Where the reactor's feed comes from:**
  1. **Their own Twitch/YouTube live** (reuses Phase 1 live sources). This has the lowest set-up cost, but there are 5–20 s of platform latency between what the reactor saw and what we receive.
  2. **Direct ingest:** an SRT or WHIP endpoint run by a MediaMTX sidecar container, with RAM-only ingest and no recording. It has the lowest latency, and the reactor needs no platform account. This is the recommended path.
- **Keeping reaction and video aligned:**
  - The reactor watches the channel on a "reactor console" (the Phase 4 page, embed mode, synced to the schedule).
  - Their feed arrives with latency *L*, so the compositor delays the programme input by *L* (a `-itsoffset` / `tpad` delay) and the whole composited channel runs *L* seconds behind the schedule.
  - *L* is measured with a clapper: the console flashes and beeps on a known timestamp, and the server detects it in the ingest. It can then be fine-tuned with a slider.
- **Scheduling:**
  - A reaction is a time block bound to a reactor: `ChannelBlock.reactor_id`, plus a new `Reactor` table (name, ingest key, layout, audio mix).
  - While the reactor feed is offline, the block plays the programme alone, without compositing, and the guide says so.
- **Catch-up:** the reactor's feed is live and never recorded, so reaction airings can't be replayed. They follow the existing live rule ("not available"). Replaying the underlying programme alone is possible and should be a per-channel choice.
- **Rights:** a reaction doesn't change the rights to the underlying video. The programme's source must still be `AUTHORISED`.

## Phase 6 — Multiple curators (optional)

ArchiveTV has a single admin account today. react.tv-style curation by many people needs:
- `User` and `ChannelMember` tables with roles: **owner** (all channels, rights decisions), **curator** (edit their channels' items and blocks, propose imports) and **reactor** (ingest key only)
- password auth plus optional OIDC
- the audit log recording the real actor

**Rights authorisation stays owner-only.** Curators can only propose. Keep SQLite: it's fine at this scale.

---

## Data model changes (summary)

| Change | Phase |
|---|---|
| `PlaybackType.TWITCH_STREAM`, `ProviderType.TWITCH` (enum values only) | 1 |
| `PlaybackSource.permission_evidence` (text/URL) | 1 |
| `Channel.public` (bool), `Channel.web_player` (`embed` / `iptv`) | 4 |
| `Reactor` table; `ChannelBlock.reactor_id`, `.layout` | 5 |
| `User`, `ChannelMember` tables; `AuditLog.actor` → FK | 6 |

All of these are additive Alembic migrations (`0005`…), with no change to how existing schedules or history are stored.

## Risks

| Risk | Effect | Mitigation |
|---|---|---|
| Platform changes break yt-dlp | Sources fail to resolve and the channel fails over | Already handled by failover; keep yt-dlp current (it's pinned in `requirements.txt`); show resolver errors on the dashboard |
| Bot checks / anonymous access refused | Some videos can't air | Same as today: no workarounds. Those items fall back to embed-only for the web companion |
| Twitch ads in live streams | Discontinuities and picture jumps | Force transcode for Twitch live sources; let an admin opt out |
| Compositing CPU cost | Fewer concurrent channels | `ATV_MAX_REACTION_STREAMS`; a hardware encoder option (`h264_vaapi` / `h264_nvenc`) later |
| Public exposure of `/watch` and chat | Abuse, scraping, SSRF via import | Off by default, read-only public API, rate limits, imports stay admin/curator-only and go through `security.py` |
| Rights | Legal exposure | Mandatory rights gate + permission evidence; the web companion uses official embeds |

## Verification (per phase, as in Phase 0)

- Unit tests next to the existing ones (`tests/test_ytstream.py` is the pattern for resolvers, `tests/test_stream.py` for FFmpeg commands).
- An end-to-end capture through a throwaway Dispatcharr 0.33 instance: ingest, EPG, live, catch-up and Start Over, checking 0 backwards timestamps, 0 gaps and 0 decode errors. For reactions, also measure A/V alignment between the programme and the reactor feed (target within 100 ms after calibration).
- A device test in TiviMate.

## Suggested order and size

1. **Phase 1, Twitch playback:** small. It mostly reuses the YouTube stream path. ~1 week.
2. **Phase 3, link-list channel builder:** small to medium; UI-heavy. It delivers most of the react.tv feel over IPTV.
3. **Phase 2, Twitch discovery:** medium.
4. **Phase 5, reactions:** large. It needs an ingest sidecar, a compositor and calibration. Prototype the FFmpeg filter graph first with two local files.
5. **Phase 4, web companion + chat:** medium. Optional for IPTV, but it's the react.tv experience for browser viewers.
6. **Phase 6, multi-user:** do it only if more than one person will curate.

## Open questions for you

1. Which content will you actually air: your own streams, opted-in creators, or Archive/CC material? This decides how much Phase 1–2 is worth versus Phase 5.
2. Reactions: will the reactor stream from OBS straight to ArchiveTV (SRT/WHIP, recommended), or go live on Twitch/YouTube first?
3. Should the web companion be public (internet-facing) or LAN-only?
4. Do you need catch-up for reaction airings? That would require recording, which the minimal-disk rule currently forbids.
