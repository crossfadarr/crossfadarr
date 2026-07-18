#!/usr/bin/env python3
"""Phase 3 - MusicBrainz matcher: resolve candidate artists -> MusicBrainz IDs.

Reads  data/artists.json  (from ingest.py) and writes  data/matches.json  with,
per artist: the best MB match, a confidence tier (green/amber/red/none), and a
few alternates for the review UI's manual-fix.

Politeness / robustness:
  - SQLite cache (mb_cache.db) keyed on normalized name -> re-runs are instant
    and we never re-hit MB for a name we've already looked up.
  - ~1.1s throttle between *live* MB calls (MB asks for <=1 req/s), 503 backoff.
  - Descriptive User-Agent (MB blocks generic ones).

Usage:
    python matcher.py [--limit N] [--input data/artists.json] [--output data/matches.json]
      --limit N   only match the first N artists (0 = all). Handy for a quick check.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import unicodedata
from difflib import SequenceMatcher

import requests

MB_URL = "https://musicbrainz.org/ws/2/artist/"
USER_AGENT = "crossfadarr/0.1 (homelab companion; +https://github.com/crossfadarr/crossfadarr)"
CACHE_DB = "mb_cache.db"
MIN_INTERVAL = 1.1  # seconds between live MB requests

_last_call = [0.0]


# Unicode dashes/quotes that NFKC doesn't unify but that mean the same thing.
_PUNCT = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-",
    "‘": "'", "’": "'", "‛": "'", "`": "'",
    "“": '"', "”": '"',
})


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").translate(_PUNCT)
    return " ".join(s.casefold().split())


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


# ---- cache -----------------------------------------------------------------

def _cache_open() -> sqlite3.Connection:
    con = sqlite3.connect(CACHE_DB)
    con.execute(
        "CREATE TABLE IF NOT EXISTS artist_query ("
        " query_norm TEXT PRIMARY KEY, response_json TEXT, fetched_at REAL)"
    )
    return con


def _cache_get(con: sqlite3.Connection, q: str):
    row = con.execute(
        "SELECT response_json FROM artist_query WHERE query_norm=?", (_norm(q),)
    ).fetchone()
    return json.loads(row[0]) if row else None


def _cache_put(con: sqlite3.Connection, q: str, payload: list) -> None:
    con.execute(
        "INSERT OR REPLACE INTO artist_query VALUES (?,?,?)",
        (_norm(q), json.dumps(payload, ensure_ascii=False), time.time()),
    )
    con.commit()


# ---- MusicBrainz -----------------------------------------------------------

def _throttle() -> None:
    wait = MIN_INTERVAL - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()


def mb_search(name: str, con: sqlite3.Connection) -> list:
    """Return a trimmed list of MB artist candidates for `name` (cached)."""
    cached = _cache_get(con, name)
    if cached is not None:
        return cached

    for attempt in range(4):
        _throttle()
        try:
            r = requests.get(
                MB_URL,
                params={"query": name, "fmt": "json", "limit": 8},
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 503:  # MB rate-limit / busy
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code != 200:
            break
        cands = [
            {
                "mbid": a.get("id"),
                "name": a.get("name"),
                "score": int(a.get("score", 0)),
                "type": a.get("type"),
                "country": a.get("country"),
                "disambiguation": a.get("disambiguation"),
                # aliases matter: MB often stores artists under a native-script
                # name with the Latin form only as an alias.
                "aliases": [al.get("name") for al in (a.get("aliases") or [])
                            if al.get("name")],
            }
            for a in r.json().get("artists", [])
        ]
        _cache_put(con, name, cands)
        return cands

    _cache_put(con, name, [])  # cache the miss too
    return []


def classify(query: str, cands: list) -> dict:
    if not cands:
        return {"tier": "none", "best": None, "alternates": []}

    best = cands[0]  # MB returns best-relevance first
    # Compare against the primary name AND all aliases (Latin transliterations etc).
    names = [best["name"], *best.get("aliases", [])]
    nq = _norm(query)
    exact = any(nq == _norm(n) for n in names)
    ratio = max((_ratio(query, n) for n in names), default=0.0)
    score = best["score"]

    if exact and score >= 90:
        tier = "green"
    elif exact or ratio >= 0.85 or score >= 92:
        tier = "amber"
    elif score >= 60 or ratio >= 0.6:
        tier = "red"
    else:
        tier = "red"

    return {
        "tier": tier,
        "name_ratio": round(ratio, 3),
        "best": best,
        "alternates": cands[1:4],
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="match only first N (0=all)")
    ap.add_argument("--input", default=os.path.join("data", "artists.json"))
    ap.add_argument("--output", default=os.path.join("data", "matches.json"))
    args = ap.parse_args()

    artists = json.load(open(args.input, encoding="utf-8"))
    if args.limit:
        artists = artists[: args.limit]

    con = _cache_open()
    results, tiers = [], {"green": 0, "amber": 0, "red": 0, "none": 0}
    t0 = time.time()

    for i, a in enumerate(artists, 1):
        cands = mb_search(a["name"], con)
        verdict = classify(a["name"], cands)
        tiers[verdict["tier"]] += 1
        results.append({**a, "match": verdict})
        if i % 25 == 0 or i == len(artists):
            print(f"  ...{i}/{len(artists)}  "
                  f"(g{tiers['green']} a{tiers['amber']} r{tiers['red']} n{tiers['none']})")

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    json.dump(results, open(args.output, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    print(f"\nMatched {len(results)} artists in {time.time()-t0:.0f}s "
          f"-> {args.output}")
    print(f"  green (confident): {tiers['green']}")
    print(f"  amber (review)   : {tiers['amber']}")
    print(f"  red   (weak)     : {tiers['red']}")
    print(f"  none  (no match) : {tiers['none']}")

    print("\nSample (first 20):")
    for a in results[:20]:
        m = a["match"]
        b = m["best"]
        label = f"{b['name']} [{b.get('disambiguation') or b.get('type') or ''}]" if b else "-"
        print(f"  {m['tier']:<5} {a['name']}  ->  {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
