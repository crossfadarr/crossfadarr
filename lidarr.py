#!/usr/bin/env python3
"""Lidarr client for the review UI: read profiles/existing artists, add artists.

Add flow (verified against the real instance):
  GET /api/v1/artist/lookup?term=lidarr:<mbid>  -> full artist resource
    - if it comes back with id != 0, the artist is already in the library (skip)
    - else set profiles/root/monitor + addOptions and POST /api/v1/artist
"""
from __future__ import annotations

import time

import yaml
import requests


class Lidarr:
    def __init__(self, cfg_path: str = "config.yaml",
                 url: str | None = None, api_key: str | None = None):
        # url+api_key override the config file (used by the "Test connection" flow)
        if url and api_key:
            self.url = url.rstrip("/")
            self.key = api_key
            self.defaults = {}
            self.matches_file = "data/matches.json"
        else:
            try:
                cfg = yaml.safe_load(open(cfg_path, encoding="utf-8")) or {}
            except FileNotFoundError:
                cfg = {}  # first run — not configured yet
            lc = cfg.get("lidarr") or {}
            self.url = (lc.get("url") or "").rstrip("/")
            self.key = lc.get("api_key") or ""
            self.defaults = cfg.get("defaults", {}) or {}
            self.matches_file = cfg.get("matches_file", "data/matches.json")
        self.s = requests.Session()
        self.s.headers["X-Api-Key"] = self.key
        # P5.5 - read cache: {key: (fetched_at, value)}. Lives on the instance,
        # so the settings-save flow (which rebuilds LID) starts fresh for free.
        self._cache: dict[str, tuple[float, object]] = {}

    @property
    def configured(self) -> bool:
        return bool(self.url and self.key)

    # ---- reads -------------------------------------------------------------

    def _get(self, path: str, **params):
        r = self.s.get(f"{self.url}/api/v1/{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _cached(self, key: str, fn, ttl: float):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        self._cache[key] = (time.time(), val)
        return val

    def invalidate(self, key: str | None = None) -> None:
        if key is None:
            self._cache.clear()
        else:
            self._cache.pop(key, None)

    def ping(self) -> bool:
        try:
            self._get("system/status")
            return True
        except Exception:  # noqa: BLE001
            return False

    def existing_mbids(self) -> set[str]:
        # short TTL: the library changes whenever we (or Lidarr itself) add
        return self._cached(
            "mbids", lambda: {a["foreignArtistId"] for a in self._get("artist")}, 60)

    def rootfolders(self) -> list[dict]:
        return self._cached("roots", lambda: self._get("rootfolder"), 600)

    def quality_profiles(self) -> list[dict]:
        return self._cached("qps", lambda: self._get("qualityprofile"), 600)

    def metadata_profiles(self) -> list[dict]:
        return self._cached("mps", lambda: self._get("metadataprofile"), 600)

    def lookup_mbid(self, mbid: str) -> dict | None:
        res = self._get("artist/lookup", term=f"lidarr:{mbid}")
        return res[0] if res else None

    # ---- write -------------------------------------------------------------

    def add_artist(self, mbid: str, root: str, qp: int, mp: int,
                   monitored: bool = True, search: bool = True,
                   monitor_new: str = "all") -> dict:
        if not monitored:
            search = False  # Lidarr can't search an unmonitored artist
        try:
            obj = self.lookup_mbid(mbid)
        except Exception as e:  # noqa: BLE001
            return {"mbid": mbid, "status": "error", "msg": f"lookup failed: {e}"}
        if obj is None:
            return {"mbid": mbid, "status": "error", "msg": "not found in Lidarr lookup"}
        if obj.get("id"):
            return {"mbid": mbid, "status": "exists", "name": obj.get("artistName")}

        if monitor_new not in ("all", "new", "none"):
            monitor_new = "all"
        obj["qualityProfileId"] = qp
        obj["metadataProfileId"] = mp
        obj["rootFolderPath"] = root
        obj["monitored"] = monitored
        obj["monitorNewItems"] = monitor_new  # future albums: all / new / none
        obj["addOptions"] = {
            "monitor": "all" if monitored else "none",  # existing albums
            "searchForMissingAlbums": bool(search),
        }
        r = self.s.post(f"{self.url}/api/v1/artist", json=obj, timeout=60)
        if r.status_code in (200, 201):
            return {"mbid": mbid, "status": "added", "name": obj.get("artistName")}
        return {"mbid": mbid, "status": "error",
                "msg": f"HTTP {r.status_code}: {r.text[:200]}"}
