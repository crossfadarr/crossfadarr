# Crossfadarr — project status & devlog

*Crossfade + arr.* A self-hosted web app that reads **your own YouTube Music**
library (liked songs, library/subscribed artists) and helps you add those artists
to **Lidarr** — a Lidify-style review UI, but sourced from your YTM favourites
rather than existing-library recommendations.

**What it is / isn't:** a metadata & library-management *bridge*. It reads your
YTM account, matches artists to MusicBrainz, and writes artist entries to your
Lidarr via Lidarr's API. It **downloads nothing**, touches no indexers/torrents,
and circumvents no DRM.

Last updated: 2026-07-18.

---

## Status at a glance

| Phase | State |
|---|---|
| 1 · Prove YTM auth | ✅ Done |
| 2 · Ingest YTM library | ✅ Done |
| 3 · MusicBrainz matcher | ✅ Done |
| 4 · Review UI + Lidarr push | ✅ Done (+ 8 polish rounds) |
| 5 · v1.0 public release | 🔜 Planned (MVP-first) |

The MVP is functionally complete and proven end-to-end against a real library
(YTM → 316 candidate artists → MusicBrainz → review UI → Lidarr). Push verified
by actually adding an artist and confirming it in Lidarr.

---

## How it works

Two parts: a **pipeline** that builds JSON data files, and a **Flask app** that
reviews them and pushes to Lidarr. Since P5.2 the pipeline also runs **in-app**:
the "⟳ Refresh from YouTube Music" header button runs all five stages in a
background thread (`scanner.py`) with a weighted progress strip, polled via
`GET /api/scan/status`; the CLI scripts still work standalone (their `main()`s
now wrap shared `run(progress=...)` functions). Data files are written
atomically (`fsio.write_json_atomic`) because the app re-reads them per request.

```
YouTube Music (ytmusicapi, your auth)
     │  ingest.py
     ▼
data/artists.json  (deduped candidate artists)   + data/liked.json
     │  matcher.py         → MusicBrainz (cached, ~1 req/s)
     ▼
data/matches.json  (artist → MBID, confidence: green/amber/none)
     │  artwork.py (TheAudioDB) · ytm_thumbs.py (fallback) · genres.py (MB genres)
     ▼
data/{artwork,ytm_thumbs,genres}.json
     │  app.py  (Flask review UI)
     ▼
Lidarr  (POST /api/v1/artist via artist/lookup?term=lidarr:<mbid>)
```

### Sources (three YTM signals)
- **Library** — artists whose music you *saved* (`get_library_artists`).
- **Subscription** — artist channels you *follow* (`get_library_subscriptions`).
- **Liked** — artists from your *thumbed-up* songs (`get_liked_songs`).

### Matching & confidence
MusicBrainz artist search, alias- and Unicode-aware (compares query vs MB primary
name + all aliases; NFKC + dash/quote folding — essential for native-script
artists like 澤野弘之 = Hiroyuki Sawano). Tiers: **full** (green), **partial**
(amber), **no match** (none). Last full run on the reference library: 246 / 56 /
0 / 14 across 316 artists.

### Artwork (fallback chain)
TheAudioDB (by MBID) → YTM thumbnail → initials placeholder. ~90% coverage on the
reference library (155 TheAudioDB + 105 YTM fallback of 287 matched).

---

## Files

| File | Role |
|---|---|
| `app.py` | Flask review UI + Lidarr push + settings + scan routes + favicon |
| `scanner.py` | In-app scan: background thread, stage progress, auth-error handling |
| `fsio.py` | `write_json_atomic` (readers never see partial data files) |
| `lidarr.py` | Lidarr client (profiles, existing-artist dedup, add) |
| `ingest.py` | Pull YTM library/subs/liked → normalized `data/artists.json` |
| `matcher.py` | Resolve artists → MusicBrainz IDs (cached, throttled, confidence) |
| `artwork.py` | Fetch artist art from TheAudioDB by MBID |
| `ytm_thumbs.py` | YTM-thumbnail artwork fallback (get_artist) |
| `genres.py` | Fetch MB genres (inc=genres+tags) |
| `auth_setup.py` / `curl_to_auth.py` | YTM auth helpers (browser headers) |
| `smoke_test.py` | Phase-1 gate: prove private library is readable |
| `requirements.txt` | ytmusicapi, requests, Flask, PyYAML |

**Data** (`data/`, git-ignored): `artists, liked, matches, artwork, ytm_thumbs,
genres` `.json`.
**Caches** (git-ignored): `mb_cache.db`, `artwork_cache.db`, `genre_cache.db`,
`ytm_thumb_cache.db`.
**Secrets** (git-ignored, never commit): `config.yaml` (Lidarr URL + API key),
`auth.json` (YTM session).

---

## Running (dev, local)

Windows note: execution policy is Restricted, so don't activate the venv — call
the interpreter directly. Console output is forced to UTF-8 (much of a typical
library is non-Latin).

```
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
# 1. authenticate YTM (you do this): ytmusicapi browser  OR  python auth_setup.py browser
./.venv/Scripts/python.exe smoke_test.py          # prove auth
./.venv/Scripts/python.exe ingest.py              # build data/artists.json
./.venv/Scripts/python.exe matcher.py             # → data/matches.json  (~min, MB rate limit)
./.venv/Scripts/python.exe artwork.py             # → data/artwork.json
./.venv/Scripts/python.exe ytm_thumbs.py          # → data/ytm_thumbs.json
./.venv/Scripts/python.exe genres.py              # → data/genres.json
./.venv/Scripts/python.exe app.py                 # http://127.0.0.1:5000
```

Lidarr connection is configured in-app via the ⚙ settings gear (URL + API key,
Test connection, defaults) → written to git-ignored `config.yaml`.

---

## UI (Phase 4, done)

- Slim header: brand (animated morph logo) + stat chips (candidates / addable /
  **in-Lidarr** — the chip itself is the show/hide toggle, default hide).
- Panel **Search & filters**: card/list view toggle, search, confidence pills
  (full/partial/no-match as coloured dots + tooltips), Source (+help), Type, Sort
  (most-liked / name / confidence), Genre (searchable multi-select).
- Panel **Add to Lidarr**: root / quality / metadata / monitor-new (all/new/none)
  / monitor / search + select-visible/clear + Add selected.
- Card grid with circular artwork + hover YTM play link; list view.
- Dedups against existing Lidarr artists; in-place "✓ in Lidarr" after adding.
- Perf: `content-visibility` + hide-during-resize for the large image grid.

---

## Key decisions

- **Host = roll our own** (not Lidarr core / not Tubifarry). Spike verdict: the
  private YT Music library is only reachable via the unofficial `ytmusicapi`;
  Lidarr PR #5395 is public-playlists-only and can't host a Python dep; Tubifarry
  won't build on the fragile YTM package. See memory + board task #3.
- **Name = Crossfadarr** (crossfade + arr). Namespace confirmed free (GitHub /
  PyPI / Docker Hub / npm).
- **v1.0 release**: MIT license · built-in optional Forms login (arr-style) ·
  both YTM auth methods (browser-headers now, OAuth fast-follow) · staged MVP-first.
- **Publish under a GitHub Org**: `github.com/crossfadarr` (created) → repo
  `crossfadarr/crossfadarr`, image `ghcr.io/crossfadarr/crossfadarr`. Keeps it off
  the personal account.

## Known gotchas

- **YTM auth is fragile** (unofficial API). Browser-cookie auth expires in
  days–weeks; OAuth (self-provisioned Google Cloud "TV" client since Nov 2024) is
  the durable path. Re-auth is the user's job. This is the #1 support risk.
- **YTM channel URLs**: library artists' browseId is `MPLAUC…` (invalid for
  `/channel/`). Prefer a bare `UC…` id or strip the `MPLA` prefix.
- **TheAudioDB**: uses public test key `2` — not OK for a distributed app.
  Pre-release: user-supplied key or lean on the YTM-thumbnail fallback.
- **MusicBrainz** rate-limit ~1 req/s → matcher/genres take minutes; cached after.
  Always send a descriptive User-Agent.

## External services & attribution

- `ytmusicapi` (unofficial YouTube Music API) — the only path to the private library.
- MusicBrainz (artist IDs, genres) — descriptive UA + 1 req/s.
- TheAudioDB (artist artwork by MBID) — browser UA.
- Lidarr API v1 (add artists).

## Legal posture (for README/LICENSE at release)

Metadata-only bridge; downloads nothing; no DRM circumvention. Unofficial YTM API
is a ToS grey area (contract, not copyright/piracy). MIT no-warranty. Disclaimers:
not affiliated with Google / YouTube / Lidarr; personal use; nominative trademark
references only; original branding. Not legal advice.

---

## Phase 5 roadmap (v1.0)

MVP: ✅ P5.1 rename → crossfadarr + secrets audit (git repo initialized) ·
✅ P5.2 in-app scan w/ progress (live-verified end-to-end 2026-07-18: YTM →
316 artists in ~5 s on warm caches) ·
✅ P5.3 YTM auth in Settings (paste headers / Copy-as-cURL → validated against a
live liked-songs call before auth.json is replaced; scan errors link here) ·
⛔ P5.10 YTM OAuth — **built, then found blocked upstream** (2026-07-18): the
device flow works end-to-end and mints a valid token (official Data API accepts
it), but YT Music's internal API rejects ALL OAuth Bearer tokens with HTTP 400
(known issue, ytmusicapi #676/#682; no fix in 1.12.1). Code kept + marked
"rejected upstream" in Settings; task #122 stays open as a monitor. Mitigation:
cookie auth hardened — Settings now teaches the **incognito-session trick**
(copy headers from a private window, close without logging out → cookies aren't
rotated → lasts weeks, vs ~40 min from an active browser session) ·
✅ P5.4 Forms login (optional arr-style: Settings → Security, salted hash in
config.yaml, session gate on all routes + 401 for APIs, /login page, 30-day
sessions; ships disabled) · ✅ P5.6 TheAudioDB key user-supplied (optional Settings field; keyless installs
skip the stage and use YTM images — still 100% card art via ingest thumbs) ·
✅ P5.5 Lidarr-read cache (TTL 60s artists / 10min profiles, invalidated on
add + settings save) · P5.8 MIT + README + disclaimers · P5.7 dockerize
(moved last, 2026-07-18 — pairs with the GHCR publish) · P5.9 public repo +
Actions → GHCR.
Later ideas: track-level (liked songs → specific albums), playlist ingestion.

Tracked in Vikunja project 12 (epic #8, sub-tasks P5.0–P5.10).
