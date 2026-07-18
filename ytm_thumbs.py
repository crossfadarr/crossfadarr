#!/usr/bin/env python3
"""P4.5 - YTM-thumbnail fallback for artists TheAudioDB has no image for.

For each matched artist that lacks a TheAudioDB thumb (data/artwork.json) but has
a YT Music channel id, fetch the YTM artist thumbnail via ytmusicapi.get_artist.
Cached in ytm_thumb_cache.db (keyed by channel id). Writes data/ytm_thumbs.json
{mbid: url} which the app uses as a fallback before the initials placeholder.

    python ytm_thumbs.py [auth_file]
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

import ytm_client
from fsio import write_json_atomic

MATCHES = os.path.join("data", "matches.json")
ART = os.path.join("data", "artwork.json")
OUT = os.path.join("data", "ytm_thumbs.json")
CACHE_DB = "ytm_thumb_cache.db"
INTERVAL = 0.3


def _pick_auth() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    return ytm_client.find_auth() or "auth.json"


def _largest(thumbs: list) -> str | None:
    if not thumbs:
        return None
    return max(thumbs, key=lambda t: t.get("width", 0)).get("url")


def run(auth_path: str | None = None, progress=None) -> dict:
    """Fetch YTM fallback thumbs; `progress(i, total)` per target artist."""
    yt = ytm_client.build(auth_path)

    matches = json.load(open(MATCHES, encoding="utf-8"))
    tad = json.load(open(ART, encoding="utf-8")) if os.path.exists(ART) else {}

    con = sqlite3.connect(CACHE_DB)
    con.execute("CREATE TABLE IF NOT EXISTS t (cid TEXT PRIMARY KEY, url TEXT)")
    cache = {row[0]: row[1] for row in con.execute("SELECT cid, url FROM t")}

    # artists needing a fallback: matched, have a channel id, no TheAudioDB art
    targets = []
    for a in matches:
        best = a["match"].get("best")
        mbid = best.get("mbid") if best else None
        cids = a.get("ytm_ids") or []
        if mbid and cids and mbid not in tad:
            targets.append((mbid, cids[0]))

    out = {}
    fetched = 0
    for i, (mbid, cid) in enumerate(targets, 1):
        if cid in cache:
            url = cache[cid]
        else:
            try:
                info = yt.get_artist(cid)
                url = _largest(info.get("thumbnails") or [])
            except Exception:  # noqa: BLE001 - not every channel is an artist
                url = None
            con.execute("INSERT OR REPLACE INTO t VALUES (?,?)", (cid, url))
            con.commit()
            cache[cid] = url
            fetched += 1
            time.sleep(INTERVAL)
        if url:
            out[mbid] = url
        if progress:
            progress(i, len(targets))

    write_json_atomic(OUT, out)
    return {"targets": len(targets), "fetched": fetched,
            "thumbs": len(out), "tad": len(tad)}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    def _cb(i: int, total: int) -> None:
        if i % 25 == 0 or i == total:
            print(f"  ...{i}/{total}")

    s = run(auth_path=_pick_auth(), progress=_cb)
    print(f"{s['targets']} artists needed a YTM fallback thumb (live {s['fetched']})")
    print(f"wrote {OUT}: {s['thumbs']} fallback thumbs "
          f"({s['tad']} had TheAudioDB art already)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
