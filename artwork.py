#!/usr/bin/env python3
"""Fetch artist artwork (TheAudioDB, keyed by MBID) for the card view.

TheAudioDB is the same source Lidarr pulls artist images from, so the review
cards match what Lidarr shows after adding. Cached in artwork_cache.db so
re-runs are instant; misses are cached too. Browser UA (TheAudioDB 403s the
'Lidarr' UA).

    python artwork.py            # fills data/artwork.json {mbid: thumb_url}
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

import requests

API = "https://www.theaudiodb.com/api/v1/json/2/artist-mb.php"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) crossfadarr/0.1"
CACHE_DB = "artwork_cache.db"
MATCHES = os.path.join("data", "matches.json")
OUT = os.path.join("data", "artwork.json")
INTERVAL = 0.6  # be gentle with the free tier


def _cache():
    con = sqlite3.connect(CACHE_DB)
    con.execute("CREATE TABLE IF NOT EXISTS art (mbid TEXT PRIMARY KEY, url TEXT)")
    return con


def fetch(mbid: str) -> str | None:
    r = requests.get(API, params={"i": mbid}, headers={"User-Agent": UA}, timeout=20)
    if r.status_code != 200:
        return None
    arts = (r.json() or {}).get("artists") or []
    if not arts:
        return None
    a = arts[0]
    return a.get("strArtistThumb") or a.get("strArtistWideThumb") or a.get("strArtistLogo")


def main() -> int:
    matches = json.load(open(MATCHES, encoding="utf-8"))
    mbids = []
    seen = set()
    for a in matches:
        best = a["match"].get("best")
        mb = best.get("mbid") if best else None
        if mb and mb not in seen:
            seen.add(mb)
            mbids.append(mb)

    con = _cache()
    have = {row[0]: row[1] for row in con.execute("SELECT mbid, url FROM art")}
    todo = [m for m in mbids if m not in have]
    print(f"{len(mbids)} matched MBIDs; {len(todo)} to fetch, {len(have)} cached")

    for i, mb in enumerate(todo, 1):
        try:
            url = fetch(mb)
        except Exception:  # noqa: BLE001
            url = None
        con.execute("INSERT OR REPLACE INTO art VALUES (?,?)", (mb, url))
        con.commit()
        have[mb] = url
        if i % 25 == 0 or i == len(todo):
            print(f"  ...{i}/{len(todo)}")
        time.sleep(INTERVAL)

    art = {mb: have.get(mb) for mb in mbids if have.get(mb)}
    json.dump(art, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"wrote {OUT}: {len(art)}/{len(mbids)} artists have artwork")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
