#!/usr/bin/env python3
"""P5.2 - In-app scan: run the YTM -> data/*.json pipeline in a background thread.

Single-user app, so job state is a module-level dict guarded by a lock; the UI
polls it via GET /api/scan/status. One scan at a time. Each pipeline stage is a
module-level `run(progress=...)` call (see ingest/matcher/artwork/ytm_thumbs/
genres), so progress is exact and failures surface as real exceptions — the
important one being expired YTM auth, which gets an actionable message.
"""
from __future__ import annotations

import threading
import time

import artwork
import genres
import ingest
import matcher
import ytm_client
import ytm_thumbs

_LOCK = threading.Lock()

_IDLE = {
    "state": "idle",        # idle | running | done | error
    "stage": None, "stage_label": None, "stage_index": 0, "stages_total": 5,
    "done": 0, "total": 0, "percent": 0.0, "message": "", "hint": None,
    "error": None, "error_kind": None,   # error_kind: auth | pipeline
    "started_at": None, "finished_at": None, "summary": None,
}
STATE = dict(_IDLE)

_MB_HINT = ("MusicBrainz allows ~1 request per second, so a first scan takes "
            "several minutes — results are cached, re-scans are fast.")

# (key, label, weight, hint) — weight approximates each stage's share of
# wall-clock on a cold cache (MusicBrainz stages dominate at ~1 req/s).
STAGES = [
    ("ingest",  "Reading YouTube Music library",     5, None),
    ("match",   "Matching artists on MusicBrainz",  40, _MB_HINT),
    ("artwork", "Fetching artist artwork",          15, None),
    ("thumbs",  "Fetching YTM fallback thumbnails", 10, None),
    ("genres",  "Fetching genres",                  30, _MB_HINT),
]
_WEIGHT_TOTAL = sum(s[2] for s in STAGES)

AUTH_HELP = ("YouTube Music auth is missing or expired. Open ⚙ Settings → "
             "YouTube Music auth and paste fresh browser headers (best copied "
             "from a private/incognito window — see the tip there), then retry "
             "the scan.")


def _find_auth() -> str | None:
    return ytm_client.find_auth()


def _is_auth_error(e: Exception) -> bool:
    # Expired browser-cookie auth usually surfaces NOT as a 401 but as a
    # ytmusicapi parse error on the signed-out page ("Sign in to listen...").
    s = str(e).lower()
    return any(k in s for k in ("401", "unauthoriz", "unauthenticated",
                                "please setup", "sign in", "signinendpoint"))


def _set(**kw) -> None:
    with _LOCK:
        STATE.update(kw)


def status() -> dict:
    with _LOCK:
        return dict(STATE)


def clear_error() -> None:
    """Reset a settled error back to idle (e.g. after fresh auth is saved)."""
    with _LOCK:
        if STATE["state"] == "error":
            STATE.clear()
            STATE.update(_IDLE)


def start() -> tuple[bool, dict | None]:
    auth = _find_auth()
    with _LOCK:
        if STATE["state"] == "running":
            return False, {"error": "scan already running", "error_kind": "busy"}
        if auth is None:
            return False, {"error": AUTH_HELP, "error_kind": "auth"}
        STATE.clear()
        STATE.update(_IDLE)
        STATE.update(state="running", started_at=time.time(), message="Starting scan…")
    threading.Thread(target=_worker, args=(auth,), daemon=True).start()
    return True, None


def _stage_cb(label: str, weight_done: int, weight: int):
    def cb(done: int, total: int, *_extra) -> None:
        frac = (done / total) if total else 1.0
        pct = 100.0 * (weight_done + weight * frac) / _WEIGHT_TOTAL
        msg = f"{label} — {done}/{total}" if total else label
        _set(done=done, total=total, percent=round(pct, 1), message=msg)
    return cb


def _worker(auth: str) -> None:
    weight_done = 0
    summary: dict = {}
    for idx, (key, label, weight, hint) in enumerate(STAGES, 1):
        _set(stage=key, stage_label=label, stage_index=idx, done=0, total=0,
             message=label, hint=hint,
             percent=round(100.0 * weight_done / _WEIGHT_TOTAL, 1))
        cb = _stage_cb(label, weight_done, weight)
        try:
            if key == "ingest":
                r = ingest.ingest(auth, progress=cb)
                summary["ingest"] = {k: r[k] for k in (
                    "library_artists", "subscriptions", "liked_tracks",
                    "artists_deduped")}
            elif key == "match":
                r = matcher.run(progress=cb)
                summary["match"] = {"total": r["total"], "tiers": r["tiers"]}
            elif key == "artwork":
                summary["artwork"] = artwork.run(progress=cb)
            elif key == "thumbs":
                summary["thumbs"] = ytm_thumbs.run(auth_path=auth, progress=cb)
            else:
                summary["genres"] = genres.run(progress=cb)
        except Exception as e:  # noqa: BLE001 - surfaced to the UI
            auth_err = key in ("ingest", "thumbs") and _is_auth_error(e)
            msg = AUTH_HELP if auth_err else f"{label} failed: {str(e)[:300]}"
            _set(state="error", error=msg, error_kind="auth" if auth_err else "pipeline",
                 message=msg, finished_at=time.time())
            return
        weight_done += weight
    _set(state="done", percent=100.0, finished_at=time.time(),
         summary=summary, message="Scan complete")
