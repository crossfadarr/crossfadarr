#!/usr/bin/env python3
"""Phase 2 - Ingest: pull YT Music library into a normalized snapshot.

Reads (via ytmusicapi + your auth.json):
  - library artists      (artists you've added to your library)
  - subscriptions        (artist channels you follow)
  - liked songs          (tracks -> also a strong artist signal)

Writes a normalized snapshot to ./data/ so downstream phases (MusicBrainz match,
review UI, Lidarr push) never have to re-hit YT Music:
  - data/artists.json    deduped candidate artists (the MVP output)
  - data/liked.json      raw-ish liked tracks (for later track-level matching)

Usage:
    python ingest.py [auth_file]      # default: auth.json / browser.json

Prints only names + counts. No auth material is emitted.
"""
from __future__ import annotations

import json
import os
import sys
from collections import OrderedDict

DATA_DIR = "data"


def _pick_auth() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    for cand in ("auth.json", "browser.json", "oauth.json"):
        if os.path.exists(cand):
            return cand
    return "auth.json"


def _artist_key(name: str) -> str:
    """Dedupe by normalized name.

    YT Music uses different id namespaces for library artists vs liked-song
    artists, so id-based keying splits the same artist. Name is also what the
    MusicBrainz matcher (Phase 3) resolves on, so it's the right dedupe key.
    Same-name collisions are rare and get caught in the review UI.
    """
    return "name:" + " ".join((name or "").lower().split())


class ArtistRegistry:
    """Dedupes artists across sources, tracking where each came from."""

    def __init__(self) -> None:
        self._by_key: "OrderedDict[str, dict]" = OrderedDict()

    def add(self, name: str, ytm_id: str | None, source: str) -> None:
        name = (name or "").strip()
        if not name:
            return
        key = _artist_key(name)
        entry = self._by_key.get(key)
        if entry is None:
            entry = {
                "name": name,
                "ytm_ids": [],
                "sources": [],
                "liked_track_count": 0,
            }
            self._by_key[key] = entry
        if ytm_id and ytm_id not in entry["ytm_ids"]:
            entry["ytm_ids"].append(ytm_id)
        if source not in entry["sources"]:
            entry["sources"].append(source)

    def bump_liked(self, name: str, ytm_id: str | None) -> None:
        entry = self._by_key.get(_artist_key(name))
        if entry is not None:
            entry["liked_track_count"] += 1

    def sorted_list(self) -> list[dict]:
        # Most-liked first, then alphabetical - the useful review order.
        return sorted(
            self._by_key.values(),
            key=lambda e: (-e["liked_track_count"], e["name"].lower()),
        )


def ingest(auth: str) -> dict:
    from ytmusicapi import YTMusic

    yt = YTMusic(auth)
    reg = ArtistRegistry()

    # 1. Library artists (already artist-level).
    lib = yt.get_library_artists(limit=None) or []
    for a in lib:
        reg.add(a.get("artist"), a.get("browseId"), "library")

    # 2. Subscriptions (followed artist channels).
    try:
        subs = yt.get_library_subscriptions(limit=None) or []
    except Exception:  # noqa: BLE001 - non-fatal, some accounts have none
        subs = []
    for a in subs:
        reg.add(a.get("artist"), a.get("browseId"), "subscription")

    # 3. Liked songs -> track records + artist signal.
    liked_raw = yt.get_liked_songs(limit=5000) or {}
    tracks = liked_raw.get("tracks", []) if isinstance(liked_raw, dict) else []
    liked_tracks = []
    for t in tracks:
        artists = [
            {"name": x.get("name"), "ytm_id": x.get("id")}
            for x in (t.get("artists") or [])
            if x.get("name")
        ]
        album = t.get("album") or {}
        liked_tracks.append({
            "title": t.get("title"),
            "artists": artists,
            "album": album.get("name") if isinstance(album, dict) else None,
            "album_ytm_id": album.get("id") if isinstance(album, dict) else None,
            "video_id": t.get("videoId"),
        })
        for x in artists:
            reg.add(x["name"], x.get("ytm_id"), "liked")
            reg.bump_liked(x["name"], x.get("ytm_id"))

    artists = reg.sorted_list()

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "artists.json"), "w", encoding="utf-8") as f:
        json.dump(artists, f, ensure_ascii=False, indent=2)
    with open(os.path.join(DATA_DIR, "liked.json"), "w", encoding="utf-8") as f:
        json.dump(liked_tracks, f, ensure_ascii=False, indent=2)

    return {
        "library_artists": len(lib),
        "subscriptions": len(subs),
        "liked_tracks": len(liked_tracks),
        "artists_deduped": len(artists),
        "artists": artists,
    }


def main() -> int:
    # Half the library is non-Latin (KR/JP); keep console output from crashing.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    auth = _pick_auth()
    try:
        result = ingest(auth)
    except ImportError:
        print("[FAIL] ytmusicapi not installed. Run: pip install -r requirements.txt")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] ingest error: {e}")
        return 1

    print(f"Auth: {auth}")
    print(f"  library artists : {result['library_artists']}")
    print(f"  subscriptions   : {result['subscriptions']}")
    print(f"  liked tracks    : {result['liked_tracks']}")
    print(f"  -> deduped candidate artists: {result['artists_deduped']}")
    print(f"  wrote {DATA_DIR}/artists.json and {DATA_DIR}/liked.json\n")

    print("Top 15 candidate artists (by liked-track count):")
    for a in result["artists"][:15]:
        src = "+".join(a["sources"])
        print(f"  {a['liked_track_count']:>3}  {a['name']}  [{src}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
