# Phase 0 — Technical Feasibility Report

ArchiveTV Manager 2.0 · 5 November 2026 · Dispatcharr 0.33.0 (the installed version, read from `/api/core/version/`)

## Summary

**Live playback and schedule-based catch-up both work, and both integrate natively with Dispatcharr.** The chosen protocol is **Approach A**: ArchiveTV acts as an **Xtream Codes (XC) upstream provider**. Dispatcharr 0.33 has a timeshift module that proxies catch-up requests to XC providers. It never does this for plain M3U accounts, so a plain M3U integration (the 1.1 design) can never offer catch-up.

One part of the exit requirement remains: a test from the **intended client** (TiviMate on your device) through **your installed** Dispatcharr.

Everything else has been run end to end through a **real Dispatcharr 0.33.0**: a throwaway local instance of the same image and version as yours, on an isolated Docker network, deleted afterwards. That covered ingestion, the guide, live playback, catch-up, Start Over and historical seek. A script made the same Xtream Codes requests TiviMate makes. Your own Dispatcharr was not touched.

## Results by investigation item

| # | Item | Result | Evidence |
|---|---|---|---|
| 1 | Remote Internet Archive playback | ✅ Verified | `AboutBan1935.mp4` (H.264/AAC, 664 s) streamed over HTTPS from `archive.org` → `dn790000.ca.archive.org`, with every redirect hop vetted |
| 2 | Mid-file seeking | ✅ Verified | Range support recorded (`seekable=True`). A catch-up request for 05:06, 360 s into the programme, was checked frame by frame: 4 s into the output matches the source at 362.1 s (SSIM **0.999**). The ~2 s difference is the remux starting at the previous keyframe |
| 3 | FFmpeg stream output | ✅ Verified | MPEG-TS, H.264 + AAC 48 kHz, fixed PIDs. 0 decode errors, 0 backwards timestamps, 0 gaps > 0.5 s across live (22 s), catch-up (27 s and 17 s) and Start Over (16 s) captures |
| 4 | Transitions between files | ✅ Verified | Automated test crosses remux ↔ transcode boundaries in both live and catch-up sessions with continuous timestamps (`tests/test_stream.py`, `tests/test_catchup.py`) |
| 5 | Dispatcharr ingestion | ✅ Verified on Dispatcharr 0.33.0 | Xtream Codes account refresh: "1 created". The stream arrived with `is_catchup=true, catchup_days=7`. Channel created from the stream; XMLTV EPG mapped (1,783 programmes parsed); live through Dispatcharr's proxy: 18 s, valid TS, no backwards timestamps |
| 6 | Dispatcharr catch-up compatibility | ✅ Verified on Dispatcharr 0.33.0 | Catch-up through Dispatcharr's `/timeshift/…` and `/streaming/timeshift.php`: correct programme, from 0.0 s, no Dispatcharr errors. Required fixes 7 and 8 below |
| 7 | TiviMate Start Over / catch-up | ✅ XC client emulated · ⏳ device test pending | The script made TiviMate's requests against Dispatcharr's Xtream Codes output. `get_live_streams` returned `tv_archive=1` and `tv_archive_duration=7`. The guide (`get_simple_data_table`) listed 14 past programmes, all `has_archive=1`. Catch-up, Start Over and a 6-minute seek all played. Only the real app on your device is untested |
| 8 | RAM-only buffering / minimal storage | ✅ Verified | In a read-only container with all capabilities dropped and a 256 MB tmpfs, `/tmp` used **0 bytes** during catch-up. The data directory grew 78 KB (database rows only) across a 20-minute test with 9 sessions, and no media files were written |

## Chosen integration protocol (Approach A)

```
TiviMate ──XC──▶ Dispatcharr 0.33 ──XC upstream──▶ ArchiveTV
                 (channels, EPG, clients,           /player_api.php, /xmltv.php
                  catch-up proxy, stats)            /live/{u}/{p}/{id}.ts
                                                    /timeshift/{u}/{p}/{min}/{start}/{id}.ts
```

What Dispatcharr's code requires of the upstream, and what ArchiveTV does:

- **Whole minutes.** Dispatcharr drops seconds, so ArchiveTV snaps Xtream Codes requests to a programme starting within that minute (fix 8).
- **Timezone.** Dispatcharr converts the inbound UTC timestamp into the upstream's `server_info.timezone`. ArchiveTV advertises `UTC`, so timestamps arrive unconverted and are parsed as UTC. Dispatcharr drops seconds, so catch-up starts on a whole minute.
- **Duration.** It arrives in minutes: the client's hint + 5 min, or the EPG programme length + 5, defaulting to 120 and capped at 480. ArchiveTV plays that long, continuing across programme boundaries, then ends the stream.
- **Byte proxying.** Dispatcharr proxies bytes rather than redirecting, and peeks at the first 1024 bytes for MPEG-TS sync. ArchiveTV's first packet is always a TS packet.
- **Concurrency.** Each catch-up session holds one profile slot in Dispatcharr and one FFmpeg process in ArchiveTV, capped by `ATV_MAX_CATCHUP_SESSIONS` (default 3, returning 503 when full).
- **Advertising.** Dispatcharr only advertises archive when `tv_archive_duration > 0`, so ArchiveTV sends the channel's retention, which defaults to 7.

**EPG history.** Dispatcharr's EPG output defaults to **0 days back**, which hides every ended programme from clients. The *Default days back* setting must be raised to the catch-up window, otherwise TiviMate has nothing to start catch-up from. Dispatcharr also keeps past EPG data only as long as the mapped catch-up channels' retention, clamped to 1–30 days. ArchiveTV's XMLTV includes each channel's full history.

**Approach B** is implemented for completeness: direct `/catchup/{slug}.ts?start=…` and `/startover/{slug}.ts` endpoints for VLC and testing. It is not needed for Dispatcharr.

## Defects found and fixed during Phase 0

1. **Timestamp misparse.** Python's `datetime.fromisoformat()` accepts Dispatcharr's `2026-11-05:05-06` and reads it as 05:00 at UTC−06:00, which puts catch-up 6 hours out. Xtream Codes layouts are now matched explicitly before ISO parsing, with a regression test.
2. **Catch-up start lag.** Source resolution happened twice per request (pre-flight plus first segment), and resolution time counted against the requested start, so playback began 2.2 s late. Vetted redirect targets are now reused for 10 minutes (dropped on any failure). The catch-up clock is held until the first segment starts, so requested offsets are now exact (360.0 s, 0.0 s).

## Known limitations

- **Keyframe pre-roll.** A remuxed source joined mid-programme starts at the previous keyframe, measured at 1.9 s here and typically 0–4 s. Transcoded channels (`playback_format=transcode`) start exactly on time at a higher CPU cost.
  - **Mid-stream joins** (after a failover or a late start), and any join on a local file, probe the keyframe first. Timestamps continue forward and audio and video start together (within 0.02 s).
  - **The first join on a remote source** skips that probe to keep start-up fast (it costs several seconds on archive.org). There, video can lead audio by up to the pre-roll (2–4 s measured), once, at tune-in.
- **No byte-range seeking.** Catch-up responses are generated live, so they have no `Content-Length` and ignore byte-range requests. Seeking inside catch-up is done by a new timestamp request, which is how Dispatcharr and TiviMate behave. Seek controls that depend on byte ranges or a known file length will not work.
- **Rewind inside a catch-up stream** is whatever TiviMate offers through repeated timeshift requests. It must be confirmed on the device.
- **Startup latency.** First byte arrives 1–3 s after the request (archive.org redirect resolution plus FFmpeg opening the remote MP4). Dispatcharr's own upstream timeouts were not resolved from its configuration, but are expected to be well above this.

## Acceptance and failure-condition tests (specification §17, §19)

Everything below runs automatically (`tests/test_failures.py` and others, 69 tests) or was run by hand where noted. "Clean" means:
- every response is valid MPEG-TS
- no timestamps go backwards
- the viewer is never starved for more than 3.5 s

| Condition | Result |
|---|---|
| FFmpeg killed mid-programme | ✅ Stream continues on the next segment, the failure is logged, and the viewer is never starved more than 3.5 s |
| Network drops mid-programme (source closes at 1/3, reconnects refused) | ✅ The failure is logged, the next programme follows, and timestamps stay continuous (after fix 3 below) |
| Busy mirror (HTTP 503 twice, then OK) | ✅ Retried, the programme plays, and the source stays healthy (fix 4) |
| Restart during a programme (new process, same database) | ✅ Joins where the guide says the programme is now, within start-up time, twice in a row |
| Database becomes unavailable mid-stream | ✅ Viewers stay on an "off air" card, `/health` returns 503, and programmes resume when the database returns (fix 5) |
| Temporary filesystem 100% full (container, 256 MB tmpfs filled) | ✅ Live and catch-up stream normally (media never touches `/tmp`). The alert reads "temporary filesystem 100.0% full" and `/health` stays up (fix 6) |
| Codecs differ between programmes | ✅ Remux ↔ transcode transitions are continuous |
| Two viewers, and one rewinds while another watches live | ✅ One shared live pipeline plus an independent catch-up pipeline |
| Programme cannot be seeked | ✅ 409 (direct) / 404 (Xtream Codes) unless requested from the start |
| YouTube quota exhausted | ✅ Discovery pauses until the reset, without errors piling up (mocked API) |
| DST change-over nights (25 Oct, 29 Mar) | ✅ Gap-free UTC schedules. A 00:30–02:30 local block spans 3 h / 1 h of real time |
| Discovery limits | ✅ At most `max_results` candidates per scan. A multi-video Archive item is one candidate and is imported whole |
| Plays in VLC (manual) | ✅ VLC 3 picked its H.264 (avcodec) and AAC (faad) decoders for live and catch-up. Re-muxed by VLC: 14.8 s of a 15 s run, 0 decode errors. Its only warnings were the intended discontinuity flag, once per PID at stream start |
| Dispatcharr disconnects / client closes | ✅ Catch-up sessions stop immediately; live stops after the idle delay |

### Further defects found and fixed by these tests

3. **Backwards timestamps after a mid-programme failover.** A remux that starts mid-programme begins on the previous keyframe and kept those packets' earlier timestamps. They landed up to a GOP (8 s in the test) behind the previous segment, which can stall players. Now the keyframe is probed, the segment clock is shifted by the pre-roll, `-noaccurate_seek` aligns audio to it, and `-t` covers the pre-roll so audio runs to the same end.
4. **One HTTP 5xx from archive.org took a programme off air and out of catch-up.** This happened for real during testing. Transient open errors (5xx, 429, timeouts, resets) are now retried twice, re-resolving the mirror each time, before failover.
5. **A database error ended every live stream.** Streams now hold on the "off air" card and retry, and `/live` answers 503 instead of 500.
6. **The resource monitor crashed on a full `/tmp`.** It used `tempfile.gettempdir()`, which writes a probe file, so it failed exactly when the alert was needed. It now reads the path directly.
7. **Dispatcharr's catch-up proxy gives up after about 5 s without data.** A remote mid-programme start can take longer than that to open, and the request failed with "Read timed out" and a 500. Responses now begin with MPEG-TS null packets (PID 0x1FFF, standard padding players discard), sent every second until video flows.
8. **Dispatcharr sends whole-minute times.** Start Over for a programme that began at 08:19:48 arrived as 08:19:00, so it started in the previous programme's tail. Xtream Codes requests now snap forward to a programme starting within the next 60 s. When several start in that minute, the one whose length matches Dispatcharr's duration hint wins.

## Steps to close Phase 0 (need your approval)

1. Deploy ArchiveTV on the Dispatcharr host with `docker compose up -d --build`, on Dispatcharr's Docker network.
2. Add the Xtream Codes account in Dispatcharr and enable the ArchiveTV group. **This changes your Dispatcharr configuration.**
3. Raise Dispatcharr's EPG *Default days back* to 7. **This is a Dispatcharr setting change.**
4. Confirm no Dispatcharr DVR rules target the new channels.
5. On TiviMate:
   - Live: tune the channel.
   - Start Over: restart the current programme.
   - Catch-up: play yesterday's programme from the guide.
   - Seek and rewind inside it.
   - Return to live.

   Record what works. If a control isn't available through Dispatcharr's XC output, document it as unsupported.

## Experimental: YouTube streaming (proof of concept, outside the spec)

You asked to break the specification's YouTube rule as a proof of concept, so `ATV_YOUTUBE_STREAMING` is opt-in and off by default.

**Tested live with "Me at the zoo"** (YouTube's first video, 19 s):
- **Resolution:** yt-dlp returned H.264 240p plus AAC.
- **Validation:** `IPTV_READY` and seekable.
- **Gates:** the video could not be scheduled until it was approved *and* its rights authorised, and switching the feature off blocked it again.
- **Live:** a 30 s capture looped across programme boundaries with 0 backwards timestamps, 0 gaps and 0 decode errors.
- **Catch-up:** replaying an airing from 2 minutes earlier gave the same clean result.

**YouTube Live, tested via channel `/live` addresses:**
- **NASA:** validated as live, 720p H.264 + AAC, and remuxed. In a time block, the live slot filled the block exactly (08:54–09:54). 35 s at the live edge had no gaps, no backwards timestamps and no decode errors.
- **Audio:** NASA's own feed was silent at the time (-90 dB at the source). Lofi Girl's stream confirmed audio passes through intact: -14.9 dB out against -15.2 dB at the source.
- **Offline:** the SpaceX channel wasn't live, was marked unavailable, and was never scheduled.
- **Catch-up:** past live slots are refused as "a live broadcast", and the guide marks them "Live".

Nothing is downloaded. Logins, cookies, proxies, bot-check workarounds and DRM are all excluded.

## v2.0 items completed after Phase 0

These were deferred while Phase 0/1 was proven, and are now done:
- **React + TypeScript** admin frontend at `/app/`, built on `/api/v1`, with every admin page. The classic UI stays at `/admin`.
- **APScheduler** for all background jobs.
- **Duplicate detection** by file checksum (Archive SHA-1/MD5, local SHA-1), then media URL, then title, episode and duration.
- **Per-rule preferences:** preferred formats and preferred resolution, plus an option to skip validation.
- **Logo upload** to `/data/logos`, served at `/logos/…`.
- **schedule_versions** publication history.
- Alembic migrations 0002 and 0003 for the above.

Still open:
- **Dispatcharr and TiviMate:** the steps above. You asked for Dispatcharr to be left alone for now.
- **Live YouTube Data API test:** needs a free API key. YouTube is discovery and preview only, so this affects nothing that airs.
  - **Without a key:** single-video imports now work through YouTube's official oEmbed endpoint. Tested live: "Me at the zoo" was imported with its title, channel and embedded player.
  - **Search and channel discovery without a key:** opt-in through ytscrape (`ATV_YOUTUBE_UNOFFICIAL=true`), an unofficial source.
    - Tested live: a keyword search imported 3 videos with full metadata, and the `@jawed` channel rule found "Me at the zoo".
    - Everything stays preview-only and is flagged as unofficial.
  - **Still needs the key:** playlist rules, and the official-API test itself.
