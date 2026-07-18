#!/usr/bin/env python3
"""Fetch artist artwork (TheAudioDB, keyed by MBID) for the card view.

TheAudioDB is the same source Lidarr pulls artist images from, so the review
cards match what Lidarr shows after adding. Cached in artwork_cache.db so
re-runs are instant; misses are cached too. Browser UA (TheAudioDB 403s the
'Lidarr' UA).

Needs a user-supplied API key (config.yaml -> theaudiodb: api_key). No key
ships with the app — without one this stage is skipped and artist images come
from the YTM thumbnail chain instead.

    python artwork.py            # fills data/artwork.json {mbid: thumb_url}
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

import requests
import yaml

from fsio import write_json_atomic

API = "https://www.theaudiodb.com/api/v1/json/{key}/artist-mb.php"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) crossfadarr/0.1"
CACHE_DB = "artwork_cache.db"
MATCHES = os.path.join("data", "matches.json")
OUT = os.path.join("data", "artwork.json")
INTERVAL = 0.6  # be gentle with the free tier


def _api_key() -> str | None:
    try:
        cfg = yaml.safe_load(open("config.yaml", encoding="utf-8")) or {}
    except FileNotFoundError:
        return None
    return (cfg.get("theaudiodb") or {}).get("api_key") or None


def _cache():
    con = sqlite3.connect(CACHE_DB)
    con.execute("CREATE TABLE IF NOT EXISTS art (mbid TEXT PRIMARY KEY, url TEXT)")
    return con


def fetch(mbid: str, key: str) -> str | None:
    r = requests.get(API.format(key=key), params={"i": mbid},
                     headers={"User-Agent": UA}, timeout=20)
    if r.status_code != 200:
        return None
    arts = (r.json() or {}).get("artists") or []
    if not arts:
        return None
    a = arts[0]
    return a.get("strArtistThumb") or a.get("strArtistWideThumb") or a.get("strArtistLogo")


def run(progress=None) -> dict:
    """Fetch artwork for all matched MBIDs; `progress(i, total)` per live fetch.

    Without a configured API key the stage is skipped (existing artwork.json,
    if any, is left as-is) and the caller falls back to YTM thumbnails.
    """
    key = _api_key()
    if not key:
        return {"skipped": True, "mbids": 0, "fetched": 0, "cached": 0, "with_art": 0}
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
    cached = len(have)
    todo = [m for m in mbids if m not in have]

    for i, mb in enumerate(todo, 1):
        try:
            url = fetch(mb, key)
        except Exception:  # noqa: BLE001
            url = None
        con.execute("INSERT OR REPLACE INTO art VALUES (?,?)", (mb, url))
        con.commit()
        have[mb] = url
        if progress:
            progress(i, len(todo))
        time.sleep(INTERVAL)

    art = {mb: have.get(mb) for mb in mbids if have.get(mb)}
    write_json_atomic(OUT, art)
    return {"mbids": len(mbids), "fetched": len(todo), "cached": cached,
            "with_art": len(art)}


def main() -> int:
    def _cb(i: int, total: int) -> None:
        if i % 25 == 0 or i == total:
            print(f"  ...{i}/{total}")

    s = run(progress=_cb)
    if s.get("skipped"):
        print("skipped: no TheAudioDB API key in config.yaml (theaudiodb: api_key) "
              "— artist images will come from YTM thumbnails")
        return 0
    print(f"{s['mbids']} matched MBIDs; {s['fetched']} fetched, {s['cached']} cached")
    print(f"wrote {OUT}: {s['with_art']}/{s['mbids']} artists have artwork")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
