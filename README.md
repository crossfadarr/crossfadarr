# Crossfadarr

*Crossfade + arr.* A self-hosted web app that reads **your own YouTube Music
library** — liked songs, saved artists, subscriptions — and helps you add those
artists to **[Lidarr](https://lidarr.audio/)**.

Think of it as a Lidify-style review grid, but sourced from what you actually
listen to on YouTube Music rather than recommendations derived from your
existing library.

<!-- screenshot: add docs/screenshot.png before publishing -->

**What it is:** a metadata and library-management bridge. It reads your YTM
account, matches artists to MusicBrainz, and adds the ones you pick to Lidarr
via Lidarr's own API.

**What it is not:** a downloader. Crossfadarr downloads no music, talks to no
indexers or torrents, and circumvents nothing. What Lidarr does after an artist
is added is Lidarr's business, configured by you, in Lidarr.

## Features

- **One-click scan** — "⟳ Refresh from YouTube Music" runs the whole pipeline
  in-app with a live progress bar (ingest → MusicBrainz match → artwork →
  genres). Results are cached, so only the first scan is slow.
- **Three signals from YTM**: artists whose music you *saved* (library),
  artist channels you *follow* (subscriptions), and artists from your
  *thumbed-up* songs (liked).
- **Careful matching** — MusicBrainz search that is alias- and Unicode-aware
  (essential for native-script artists: 澤野弘之 = Hiroyuki Sawano), with
  match-confidence dots and an alternates dropdown to fix any misses by hand.
- **Honest flags** — artists MusicBrainz lists *no releases* for are badged
  and excluded from bulk selection (adding them would create an empty Lidarr
  artist; it usually also means the match hit a bootleg or placeholder entry).
- **Review before anything happens** — search, filters (confidence, source,
  type, genre), card/list views, circular artwork with a YTM play link.
  Nothing is sent to Lidarr until you tick artists and click *Add selected*.
- **Lidarr-aware** — dedupes against what's already in your library, shows an
  "in Lidarr" count, respects your root folder / quality / metadata profiles,
  monitor and search options, and keeps a history log of every add.
- **Optional login** — arr-style Forms authentication (salted password hash),
  off by default for LAN use.

## Requirements

- Python 3.11+
- A [Lidarr](https://lidarr.audio/) instance and its API key
- A YouTube Music account
- Optional: a private [TheAudioDB](https://www.theaudiodb.com/) API key for
  artist portraits — by default Crossfadarr uses their public free-tier key
  (v1 API) within its rate limits, and falls back to YouTube Music's own
  images for anything without a portrait

## Quick start

```bash
git clone https://github.com/crossfadarr/crossfadarr.git
cd crossfadarr
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt     # Windows
# .venv/bin/pip install -r requirements.txt       # macOS / Linux
.venv/Scripts/python app.py                       # -> http://127.0.0.1:5000
```

Then, in the app:

1. **⚙ Settings** → enter your Lidarr URL + API key → *Test connection* → Save.
2. **⚙ Settings → YouTube Music auth** → paste your browser headers (see below).
3. Click **⟳ Refresh from YouTube Music** and let the scan run. The first scan
   takes several minutes (MusicBrainz allows ~1 request/second); re-scans take
   seconds.
4. Review, tick, **Add selected**.

A Docker image is planned — until then it runs anywhere Python does.

## YouTube Music authentication (read this)

Google offers **no official API** for reading your private YouTube Music
library, so Crossfadarr uses [ytmusicapi](https://github.com/sigma67/ytmusicapi),
which authenticates by reusing your browser session cookies. This is the
fragile part of the whole exercise, and it's worth two minutes of reading:

**How to connect:**

1. Open a **private/incognito window** and log into
   [music.youtube.com](https://music.youtube.com).
2. Press F12 → **Network** tab → type `browse` in the filter → click around in
   YTM so a POST to `/browse` appears.
3. Right-click that request → **Copy → Copy as cURL (bash)**.
4. **Close the incognito window** (don't log out).
5. Paste into Crossfadarr's **⚙ Settings → YouTube Music auth** box and save.
   The paste is validated against your library before anything is stored.

**Why incognito?** Google rotates the cookies of sessions that stay active. If
you copy headers from your everyday browser, that browser keeps using (and
rotating) the session, and your pasted snapshot can die within hours. A closed
incognito session's cookies are never rotated again and typically last weeks.
When they do expire, the scan tells you plainly and points you back to
Settings.

**Why not OAuth?** We built the full OAuth device flow — and then verified
that YouTube Music's internal API currently **rejects valid OAuth tokens
outright** (HTTP 400 on every endpoint, even though the same token works
against the official YouTube Data API). This is a Google-side limitation
affecting all ytmusicapi-based tools. The OAuth code remains in the app,
marked as unavailable, in case Google restores support.

Your auth file (`auth.json`) stays on your machine, is git-ignored, and is
used for nothing but reading your library.

## How it works

```
YouTube Music (ytmusicapi, your session)
     │  ingest: library + subscriptions + liked songs
     ▼
deduped candidate artists (+ thumbnails YTM already provides)
     │  match: MusicBrainz search, alias/Unicode-aware, cached, ~1 req/s
     ▼
artist → MBID + confidence tier + alternates + release counts + genres
     │  artwork: TheAudioDB portraits (v1 API, free tier) → YTM images fallback
     ▼
review UI  →  Add selected  →  Lidarr (POST /api/v1/artist)
```

Everything lands in plain JSON files under `data/` and SQLite caches next to
the app — no database server, easy to inspect, easy to back up.

## Configuration notes

- All configuration lives in the in-app **⚙ Settings**: Lidarr connection and
  defaults, YouTube Music auth, optional TheAudioDB key, optional Forms login.
  It's written to a git-ignored `config.yaml`.
- **Metadata profile tip:** Lidarr's "Standard" profile tracks albums only. If
  an artist mainly releases EPs or singles (common in K-pop), pick a metadata
  profile that includes those, or the artist will look empty in Lidarr.
- Locked yourself out of the Forms login? Edit `config.yaml` →
  `auth: enabled: false` and restart.

## External services & attribution

| Service | Used for | Notes |
|---|---|---|
| [ytmusicapi](https://github.com/sigma67/ytmusicapi) | Reading your own YTM library | Unofficial API |
| [MusicBrainz](https://musicbrainz.org/) | Artist identification, genres, release counts | Throttled to ~1 req/s with a descriptive User-Agent, per their guidelines |
| [TheAudioDB](https://www.theaudiodb.com/) | Artist portraits | v1 API; public free-tier key by default (rate-limited accordingly), private key optional |
| [Lidarr](https://lidarr.audio/) | The destination | Its API v1, your instance |

## Disclaimers

- Crossfadarr is an independent project, **not affiliated with or endorsed by
  Google, YouTube, Lidarr, MusicBrainz, or TheAudioDB**. Names are used only
  to describe interoperability.
- It accesses **your own account's data** via an unofficial API. Automated
  access may conflict with YouTube's Terms of Service; use at your own
  discretion, for personal use.
- Crossfadarr manages metadata only. It downloads no media and circumvents no
  technical protection measures.
- Provided **as is**, without warranty of any kind — see [LICENSE](LICENSE).

## License

[MIT](LICENSE)
