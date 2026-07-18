#!/usr/bin/env python3
"""P4.14 - Fetch MusicBrainz genres per matched artist.

MB has curated 'genres' and folksonomy 'tags'. We prefer genres, fall back to
tags, keep the top few by vote count. Cached in genre_cache.db (1 req/s, MB
limit). Writes data/genres.json {mbid: [genre, ...]}.

    python genres.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

import requests

API = "https://musicbrainz.org/ws/2/artist/"
UA = "crossfadarr/0.1 (homelab companion; +https://github.com/crossfadarr/crossfadarr)"
CACHE_DB = "genre_cache.db"
MATCHES = os.path.join("data", "matches.json")
OUT = os.path.join("data", "genres.json")
MIN_INTERVAL = 1.1
_last = [0.0]


def _throttle():
    wait = MIN_INTERVAL - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()


def fetch(mbid: str) -> list:
    for attempt in range(4):
        _throttle()
        try:
            r = requests.get(f"{API}{mbid}", params={"inc": "genres+tags", "fmt": "json"},
                             headers={"User-Agent": UA}, timeout=20)
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 503:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code != 200:
            return []
        d = r.json()
        items = d.get("genres") or d.get("tags") or []
        items.sort(key=lambda g: g.get("count", 0), reverse=True)
        return [g["name"] for g in items if g.get("name")][:3]
    return []


def main() -> int:
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
    have = {row[0]: json.loads(row[1]) for row in con.execute("SELECT mbid, genres FROM g")}
    todo = [m for m in mbids if m not in have]
    print(f"{len(mbids)} MBIDs; {len(todo)} to fetch, {len(have)} cached")

    for i, mb in enumerate(todo, 1):
        try:
            g = fetch(mb)
        except Exception:  # noqa: BLE001
            g = []
        con.execute("INSERT OR REPLACE INTO g VALUES (?,?)", (mb, json.dumps(g)))
        con.commit()
        have[mb] = g
        if i % 25 == 0 or i == len(todo):
            print(f"  ...{i}/{len(todo)}")

    out = {mb: have.get(mb) for mb in mbids if have.get(mb)}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    withg = sum(1 for v in out.values() if v)
    print(f"wrote {OUT}: {withg}/{len(mbids)} artists have a genre")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
