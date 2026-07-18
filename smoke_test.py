#!/usr/bin/env python3
"""Phase 1 GATE: prove ytmusicapi can read the PRIVATE YT Music library.

This is the single most important check in the whole project. The official
YouTube Data API cannot see liked songs / library artists; only ytmusicapi can.
If this returns real data against your account, the companion-app approach is
viable and we proceed to Phase 2. If it fails, auth is the blocker and we stop
and rethink.

Usage:
    python smoke_test.py [auth_file]      # default: auth.json (or browser.json)

Shares nothing sensitive: it prints counts and a handful of names so we can
confirm it's really your library — no auth material is emitted.
"""
import os
import sys


def _pick_auth() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    for cand in ("auth.json", "browser.json", "oauth.json"):
        if os.path.exists(cand):
            return cand
    return "auth.json"


def main() -> int:
    auth = _pick_auth()
    try:
        from ytmusicapi import YTMusic
    except ImportError:
        print("[FAIL] ytmusicapi not installed. Run: pip install -r requirements.txt")
        return 1

    try:
        yt = YTMusic(auth)
    except Exception as e:  # noqa: BLE001 - surface anything auth-related
        print(f"[FAIL] Could not init YTMusic with '{auth}': {e}")
        print("       Run  python auth_setup.py  first (see its docstring).")
        return 1

    ok = True
    print(f"Using auth file: {auth}\n")

    # 1. Library / subscribed artists — the MVP source (already artist-level).
    try:
        artists = yt.get_library_artists(limit=None)
        print(f"[ OK ] library artists: {len(artists)}")
        for a in artists[:10]:
            print(f"         - {a.get('artist')}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"[FAIL] get_library_artists: {e}")

    # 2. Liked songs.
    try:
        liked = yt.get_liked_songs(limit=5000)
        tracks = liked.get("tracks", []) if isinstance(liked, dict) else []
        print(f"\n[ OK ] liked songs: {len(tracks)}")
        for t in tracks[:10]:
            arts = ", ".join(x.get("name", "") for x in (t.get("artists") or []))
            print(f"         - {t.get('title')} - {arts}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"\n[FAIL] get_liked_songs: {e}")

    # 3. Subscriptions (artist channels you follow) - nice-to-have, non-fatal.
    try:
        subs = yt.get_library_subscriptions(limit=None)
        print(f"\n[ OK ] subscriptions: {len(subs)}")
    except Exception as e:  # noqa: BLE001
        print(f"\n[warn] get_library_subscriptions (non-fatal): {e}")

    print("\n" + "=" * 60)
    if ok:
        print("RESULT: PASS - private library is reachable. Proceed to Phase 2.")
    else:
        print("RESULT: FAIL - see errors above. Auth/API access is the blocker;")
        print("        stop and rethink before building further.")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
