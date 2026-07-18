# Crossfadarr

*Crossfade + arr* — get your **YouTube Music** favourites/library into **Lidarr**. A
Lidify-style flow, but sourced from your *own* YT Music account (liked songs,
library/subscribed artists, playlists) rather than existing-library recommendations.

Standalone companion app: reads YT Music via `ytmusicapi`, matches to MusicBrainz, and
pushes artists/albums into Lidarr over its API. It deliberately owns the fragile YT Music
auth so nothing else has to.

> Tracked in Vikunja: project **"YT Music → Lidarr Importer"** (Homelab → id 12).
> Background/decision trail: Lidarr declined this natively (Lidarr#5745, not_planned);
> the official YouTube Data API can't read the private library, so `ytmusicapi` is the
> only path — see the spike notes on board task #3.

## Why this exists / the one hard constraint

The official YouTube Data API v3 **does not expose** your private YT Music library
(liked songs, library artists). The **only** thing that can is the unofficial
`ytmusicapi`, which authenticates by reusing your browser session headers, or via a
self-provisioned Google Cloud "TV device" OAuth client. That auth is the project's main
risk — so we prove it works **before** building anything on top.

## Phase 1 — prove auth (do this first)

This is the gate. Run it against your real account; if it returns real data, we proceed.

```bash
cd crossfadarr
python -m venv .venv && . .venv/Scripts/activate      # Windows; use .venv/bin/activate on mac/Linux
pip install -r requirements.txt

# Authenticate (browser method is easiest). Either:
ytmusicapi browser                 # built-in CLI, writes browser.json
# ...or:
python auth_setup.py browser       # paste headers, writes auth.json

# Verify it can read your PRIVATE library:
python smoke_test.py               # auto-finds auth.json / browser.json
```

Expected: non-zero counts for **library artists** and **liked songs**, plus a few real
names, ending in `RESULT: PASS`. That confirms the whole approach and unblocks Phase 2.

**Security:** your auth file (`auth.json` / `browser.json`) stays on your machine and is
git-ignored. Claude never sees it — only the smoke-test counts.

## Roadmap (Vikunja subtasks under board #2)

1. **Phase 1** — prove `ytmusicapi` auth *(this kit)* ← you are here
2. **Phase 2** — ingest library artists / liked songs / playlists → normalized
3. **Phase 3** — MusicBrainz matcher (SQLite cache, ~1 req/s throttle, confidence)
4. **Phase 4** — review UI + push to Lidarr (`/api/v1/artist`, `/api/v1/album` + search)
5. **Phase 5** — dockerize + deploy to `/opt/stacks/arr` on pop-os (.54)

**MVP first slice:** library/subscribed artists → artist-level add only (skips track
matching entirely — the low-risk path to something useful).

## Files

| File | Purpose |
|---|---|
| `auth_setup.py` | Create the `ytmusicapi` auth file (browser or oauth). You run it. |
| `smoke_test.py` | Phase 1 gate — confirm the private library is readable. |
| `requirements.txt` | `ytmusicapi` (+ `requests` for later phases). |
