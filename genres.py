#!/usr/bin/env python3
"""P4.14 - Fetch MusicBrainz genres per matched artist.

MB has curated 'genres' and folksonomy 'tags'. We prefer genres, fall back to
tags, keep the top few by vote count. Cached in genre_cache.db (1 req/s, MB
limit). Writes data/genres.json {mbid: [genre, ...]}.

P5.11 - the same lookup also returns the artist's release groups for free
(inc=release-groups), so we record a per-artist release count too and write
data/release_counts.json {mbid: n}. n == 0 flags "adding this to Lidarr gives
you an empty artist" (e.g. bootleg entries like 'League of Legends').

    python genres.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

import requests

from fsio import write_json_atomic

API = "https://musicbrainz.org/ws/2/artist/"
UA = "crossfadarr/0.1 (homelab companion; +https://github.com/crossfadarr/crossfadarr)"
CACHE_DB = "genre_cache.db"
MATCHES = os.path.join("data", "matches.json")
OUT = os.path.join("data", "genres.json")
OUT_RGS = os.path.join("data", "release_counts.json")
MIN_INTERVAL = 1.1
_last = [0.0]


def _throttle():
    wait = MIN_INTERVAL - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()


def fetch(mbid: str) -> tuple[list, int | None]:
    """(top genres, release-group count) — count is None on failure so the
    cache treats it as still-pending rather than a real zero."""
    for attempt in range(4):
        _throttle()
        try:
            r = requests.get(f"{API}{mbid}",
                             params={"inc": "genres+tags+release-groups", "fmt": "json"},
                             headers={"User-Agent": UA}, timeout=20)
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 503:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code != 200:
            return [], None
        d = r.json()
        items = d.get("genres") or d.get("tags") or []
        items.sort(key=lambda g: g.get("count", 0), reverse=True)
        genres = [g["name"] for g in items if g.get("name")][:3]
        return genres, len(d.get("release-groups") or [])
    return [], None


def run(progress=None) -> dict:
    """Fetch genres for all matched MBIDs; `progress(i, total)` per live fetch."""
    matches = json.load(open(MATCHES, encoding="utf-8"))
    mbids, seen = [], set()
    for a in matches:
        best = a["match"].get("best")
        mb = best.get("mbid") if best else None
        if mb and mb not in seen:
            seen.add(mb)
            mbids.append(mb)

    con = sqlite3.connect(CACHE_DB)
    con.execute("CREATE TABLE IF NOT EXISTS g (mbid TEXT PRIMARY KEY, genres TEXT)")
    if "rgs" not in [c[1] for c in con.execute("PRAGMA table_info(g)")]:
        con.execute("ALTER TABLE g ADD COLUMN rgs INTEGER")  # P5.11 migration
    have = {row[0]: (json.loads(row[1]), row[2])
            for row in con.execute("SELECT mbid, genres, rgs FROM g")}
    cached = len(have)
    # rgs NULL = row predates P5.11 (or a failed fetch) — refetch it
    todo = [m for m in mbids if m not in have or have[m][1] is None]

    for i, mb in enumerate(todo, 1):
        try:
            g, rgs = fetch(mb)
        except Exception:  # noqa: BLE001
            g, rgs = [], None
        con.execute("INSERT OR REPLACE INTO g VALUES (?,?,?)", (mb, json.dumps(g), rgs))
        con.commit()
        have[mb] = (g, rgs)
        if progress:
            progress(i, len(todo))

    out = {mb: have[mb][0] for mb in mbids if mb in have and have[mb][0]}
    counts = {mb: have[mb][1] for mb in mbids if mb in have and have[mb][1] is not None}
    write_json_atomic(OUT, out)
    write_json_atomic(OUT_RGS, counts)
    return {"mbids": len(mbids), "fetched": len(todo), "cached": cached,
            "with_genre": sum(1 for v in out.values() if v),
            "no_releases": sum(1 for v in counts.values() if v == 0)}


def main() -> int:
    def _cb(i: int, total: int) -> None:
        if i % 25 == 0 or i == total:
            print(f"  ...{i}/{total}")

    s = run(progress=_cb)
    print(f"{s['mbids']} MBIDs; {s['fetched']} fetched, {s['cached']} cached")
    print(f"wrote {OUT}: {s['with_genre']}/{s['mbids']} artists have a genre")
    print(f"wrote {OUT_RGS}: {s['no_releases']} artists have NO releases on MusicBrainz")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
