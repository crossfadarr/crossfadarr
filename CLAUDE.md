# Crossfadarr — project guide for Claude

Crossfadarr reads your **own YouTube Music** library (liked songs, library /
subscribed artists) and helps you add those artists to **Lidarr**. It is a
**metadata / library-management bridge** — it downloads nothing, touches no
indexers/torrents, and circumvents no DRM. **Never add downloading** — that line
is what keeps the project clear of piracy/DMCA territory.

**Read `STATUS.md` first** — it's the full snapshot: architecture, data flow,
file map, decisions, gotchas, and the Phase 5 roadmap. The task board lives in
Vikunja **project 12, epic #8** (via the `vikunja` MCP, if connected).

## Running (Windows, dev)

Execution policy is **Restricted** — do **not** activate the venv; call the
interpreter directly:

- App: `./.venv/Scripts/python.exe app.py`  → http://127.0.0.1:5000
- Pipeline (builds `data/*.json`): `ingest.py` → `matcher.py` → `artwork.py`
  → `ytm_thumbs.py` → `genres.py`
- Console is heavily non-Latin (KR/JP artists). Scripts reconfigure stdout to
  UTF-8; for inline `python -c` runs, prefix `PYTHONUTF8=1`.
- After editing code, restart the app: kill the process on port 5000, relaunch in
  the background (Flask debug is off, so no auto-reload).

## Verifying changes

The in-app browser pane **cannot screenshot** the image-heavy card grid (it times
out). Verify by driving the page with JS (`javascript_tool` / evaluate) and
checking element state + computed styles, or use `read_page`. Don't rely on
screenshots for this app.

## Secrets — never commit

`config.yaml` (Lidarr URL + API key), `auth.json` (YTM session), `*_cache.db`,
and `data/*.json` (your library) are all git-ignored. Keep them out of git. No
URLs/keys are hardcoded — everything is configured via the in-app ⚙ settings gear.

## Gotchas

- **YTM auth is fragile** (unofficial `ytmusicapi`). Browser-cookie auth expires
  in days–weeks; OAuth (self-provisioned Google Cloud "TV" client) is the durable
  path. Re-authenticating is the user's job.
- **YTM channel URLs**: library artists' `browseId` is `MPLAUC…` (invalid for
  `/channel/`) — prefer a bare `UC…` id, or strip the `MPLA` prefix.
- **MusicBrainz** rate-limit is ~1 req/s, so `matcher.py`/`genres.py` take
  minutes; results are cached. Always send a descriptive User-Agent.
- **TheAudioDB** test key `2` is not OK for a distributed build — plan for a
  user-supplied key or the YTM-thumbnail fallback.

## Style

CSS uses two font weights (400/500), a dark theme (`#0f0f0f` / `#212121` + red
accent), sentence case, and circular artist art. Match the existing patterns in
`app.py`.

## Status

MVP done and polished (Phases 1–4). Current work: **Phase 5 = v1.0 public
release** — MIT license, optional Forms login, both YTM auth methods, dockerize,
publish to the `crossfadarr` GitHub org + GHCR. Build order:
P5.1 (rename/secrets-audit — folder move done) → P5.2 (in-app scan with progress)
→ P5.4/P5.3 (login + auth-in-UI) → P5.7 (docker) → P5.8/P5.9 (docs + publish).
