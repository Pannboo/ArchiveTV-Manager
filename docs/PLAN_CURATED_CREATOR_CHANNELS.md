# Plan — Link-to-channel: YouTube, Twitch and Archive over IPTV

ArchiveTV Manager 2.x · draft, 9 November 2026 · status: **proposal, nothing built yet**

## Goal

An open-source take on the react.tv idea, delivered as IPTV:

> **Paste a link → it goes to the streaming engine → it plays on a 24/7 channel in Dispatcharr.**

A link can be:
- a **YouTube video** (or a YouTube channel's `/live` address)
- a **Twitch stream** (a channel that's live now) or a **Twitch VOD / clip**
- an **Internet Archive** item

Everyone watching a channel sees the same moment, as on any TV channel.

**In scope:** link import, playback of all three sources through the existing FFmpeg engine, channel playlists, and delivery through Dispatcharr (Xtream Codes, EPG, catch-up where possible).

**Not in scope:** reactions, camera ingest, chat, or anyone going live *through* ArchiveTV. A Twitch or YouTube live stream is only ever something we *play*, never something we host.

## What already exists

| Need | Already in ArchiveTV |
|---|---|
| Archive links → playback | `providers/archive.py` + `direct_url` sources, verified end to end (`PHASE0_FEASIBILITY.md`) |
| YouTube links → playback | Experimental `youtube_stream` source, behind `ATV_YOUTUBE_STREAMING`: yt-dlp resolves at play time and FFmpeg gets a video + audio pair (`ytstream.py`, `stream._cmd`) |
| YouTube live → playback | Live sources: joined at the live edge, fill their time block, fall back when offline, rechecked every minute, not in catch-up |
| Import by URL | *Import* in the admin UI (`POST /api/v1/...` import routes) |
| 24/7 schedule, everyone in sync | `scheduler.py` + one shared `ChannelStream` per channel |
| IPTV delivery | `xtream.py`, `feeds.py`, `catchup.py` |

**The gaps:**
1. Twitch can't be played at all.
2. Getting a link on air takes several screens: import, review, approve, authorise rights, assign to a channel.

## Phase 1 — Twitch playback

**Outcome:** a Twitch link plays on a channel exactly like a YouTube stream source does today.

1. **One resolver layer for "page URL → stream URL":** move `ytstream.py` into `archivetv/resolvers/`.
   - `base.py` holds the `Resolved` dataclass (video, optional audio, user agent, codecs, height, duration, `live`, expiry) and a small interface: `matches(url)`, `cache_key(url)`, `resolve(url, fresh)`, `forget(url)`.
   - `youtube.py` is today's code, moved without behaviour changes.
   - `twitch.py` is new and uses yt-dlp's Twitch extractors (already a dependency).
   - `validator.playable_target` and `stream._resolve` / `_handle_failure` look the resolver up by playback type instead of special-casing YouTube.
2. **Enums:**
   - `PlaybackType.TWITCH_STREAM`, `ProviderType.TWITCH`
   - add the new type to `catalogue.IPTV_TYPES`
   - a toggle, `ATV_TWITCH_STREAMING` (and a Settings switch), like YouTube's
3. **Which Twitch links work:**

   | Link | Becomes | Schedule behaviour | Catch-up |
   |---|---|---|---|
   | `twitch.tv/<login>` | live item that follows whatever the channel is broadcasting | fills its time block, or hourly slots on a channel with nothing else; off-air fallback when they're offline | no (live is never recorded) |
   | `twitch.tv/videos/<id>` | VOD with a fixed length | like any programme | yes, while the VOD still exists |
   | `clips.twitch.tv/…` / `twitch.tv/<login>/clip/…` | short clip | programme or filler | yes |

4. **Twitch-specific handling:**
   - **One input:** Twitch HLS carries audio and video together, so `audio=None`. VODs are normally H.264 + AAC, which can be remuxed (`IPTV_READY`, cheap).
   - **Ads on live streams:** Twitch inserts mid-roll ads that change resolution and break timestamps. Force `transcode` for Twitch live sources so the MPEG-TS output stays continuous for Dispatcharr. VODs keep the cheap remux.
   - **Stream hosts:** live comes from `usher.ttvnw.net` / `*.hls.ttvnw.net`. VODs and clips use CDN domains that vary. Vet them against a Twitch host list, like `ytstream.STREAM_HOSTS`, and log any refused host so the list can be updated.
   - **VODs expire** after 7–60 days. The existing health check takes the source out of rotation, and the schedule fails over to the next item.
   - **Not done:** no logins, cookies or OAuth user tokens, so subscriber-only and age-gated content simply doesn't play. No ad blocking or bot-check workarounds. This is the same policy as YouTube.
5. **Live recheck:** the existing one-minute live-recheck job covers Twitch live items. When the channel goes live, the schedule picks it up automatically.
6. **Tests:**
   - resolver tests with a mocked yt-dlp `extract_info` (the pattern is `tests/test_ytstream.py`)
   - a `stream._cmd` test for the single-input transcode path
   - one real capture through Dispatcharr: 0 backwards timestamps, 0 gaps, 0 decode errors

## Phase 2 — "Paste a link, put it on a channel"

**Outcome:** one action gets a link on air.

- **Add link** box on each channel page, plus the API: `POST /api/v1/channels/{id}/links` with `{url, role, position}`. It accepts a single link or a list.
  1. It works out the provider from the URL (YouTube, Twitch or Archive). Anything else is rejected, still checked by `security.py`.
  2. It imports metadata: title, duration, thumbnail. This works without API keys: yt-dlp metadata for YouTube and Twitch, the metadata API for Archive.
  3. It validates the source with FFprobe, using the resolver.
  4. If validation passes, it **approves the item, authorises the source and adds it to the channel** in one step. The schedule is then extended from the next free slot, so the item airs without a rebuild.
  5. It returns per-link status: `on_air_queued`, `failed: <reason>` or `live_offline`.
- **Approval and rights become one confirmation instead of three screens.** The admin is the person pasting the link, so the paste counts as approval. A **checkbox on the form** ("I have permission to rebroadcast this") is still required, and is recorded in the audit log. YouTube and Twitch terms don't allow re-streaming without the creator's permission, so ArchiveTV should keep that record, even though it no longer blocks the workflow.
  - The existing review queue and discovery rules stay as they are for bulk discovery. The link flow is a fast lane beside them.
- **Playlist order:** links play in the order added (sequential mode), with drag-to-reorder backed by `ChannelItem.position`. There is also a choice between **loop the list** and **play once, then the fallback programme**.
- **Play at a set time:** an optional "start at 20:00" on a link inserts it at that time and moves the rest of the schedule later. `scheduler.override` exists today for replacing an entry; it gets an insert variant.
- **Live links:** a Twitch or YouTube live link can be given a time block (e.g. "every day 20:00–23:00"). Otherwise, on a channel with nothing else, it runs around the clock while live, and the channel shows the fallback or "off air" when it isn't.
- **Guide length:** `ATV_SCHEDULE_DAYS` already exists; allow up to 14.

## Phase 3 — Polish (optional)

- **Dashboard status per link:** last resolve, last error, and whether a live link is live now.
- **Bulk paste a playlist:** a YouTube playlist URL or a Twitch channel's VOD list expands into individual links, so they can be confirmed in one go.
- **Twitch discovery rules** through the official Helix API (client id + secret in `secrets/`), only if you later want automatic discovery rather than pasted links.

## Data model changes

| Change | Phase |
|---|---|
| `PlaybackType.TWITCH_STREAM`, `ProviderType.TWITCH`: string enum values, no column changes | 1 |
| `Channel.end_behaviour` (`loop` / `fallback`) | 2 |
| Nothing else: links reuse `MediaSource`, `MediaItem`, `PlaybackSource`, `ChannelItem` and `ScheduleEntry` | — |

## Risks

| Risk | Effect | Mitigation |
|---|---|---|
| YouTube/Twitch change something and yt-dlp breaks | Links fail to resolve and the channel fails over to the next item | Already handled by failover. Keep yt-dlp current; show resolver errors on the dashboard |
| Bot checks or anonymous access refused | Some videos won't play | Report it clearly when the link is added. No workarounds |
| Twitch live ads | Picture jumps and timestamp breaks | Force transcode for Twitch live (costs CPU: about one x264 encode per live Twitch channel) |
| Expiring stream addresses (YouTube ~6 h, Twitch tokens shorter) | Mid-programme failure | Resolvers re-resolve before expiry; on failure, re-resolve once before failing over |
| Rights | Legal exposure for re-streaming | The permission checkbox and audit trail; the feature stays opt-in per platform |

## Suggested order

1. **Phase 1, Twitch playback.** It mostly reuses the YouTube path. Moving `ytstream.py` into resolvers is a safe refactor first, with the existing tests as a guard.
2. **Phase 2, paste-a-link.** This is the part that makes it feel like react.tv.
3. **Phase 3**, as needed.

## Open questions

1. Should a pasted link go **straight on air** (one confirmation checkbox, as above), or still stop in the review queue?
2. When a channel's list runs out: **loop**, or **fallback programme**?
3. Twitch live channels: do you want them on **time blocks** (e.g. a streamer's usual hours), or **around the clock whenever they're live**?
