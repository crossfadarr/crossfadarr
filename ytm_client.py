#!/usr/bin/env python3
"""P5.10 - Shared YTMusic client construction (browser-header or OAuth auth).

Auth file precedence: browser-cookie files first. OAuth would be the durable
path, but as of 2026-07 YouTube Music's internal API rejects OAuth Bearer
tokens outright (HTTP 400 on every endpoint, even for valid correctly-scoped
tokens — known upstream issue, ytmusicapi #676/#682), so oauth.json is only
used as a last resort in case Google restores support.
"""
from __future__ import annotations

import os

import yaml

CONFIG_PATH = "config.yaml"
OAUTH_FILE = "oauth.json"
AUTH_CANDIDATES = ("auth.json", "browser.json", OAUTH_FILE)


def find_auth() -> str | None:
    for c in AUTH_CANDIDATES:
        if os.path.exists(c):
            return c
    return None


def client_creds() -> tuple[str, str] | None:
    """(client_id, client_secret) from config.yaml, or None if not configured."""
    try:
        cfg = yaml.safe_load(open(CONFIG_PATH, encoding="utf-8")) or {}
    except FileNotFoundError:
        return None
    ytm = cfg.get("ytm") or {}
    cid, cs = ytm.get("client_id"), ytm.get("client_secret")
    return (cid, cs) if cid and cs else None


def oauth_credentials():
    """ytmusicapi OAuthCredentials from config.yaml, or None."""
    creds = client_creds()
    if creds is None:
        return None
    from ytmusicapi.auth.oauth import OAuthCredentials
    return OAuthCredentials(client_id=creds[0], client_secret=creds[1])


def build(auth_path: str | None = None):
    """YTMusic instance for the given (or best available) auth file."""
    from ytmusicapi import YTMusic
    path = auth_path or find_auth()
    if path is None:
        raise FileNotFoundError(
            "no YouTube Music auth file (oauth.json / auth.json / browser.json)")
    if path == OAUTH_FILE:
        creds = oauth_credentials()
        if creds is None:
            raise RuntimeError(
                "oauth.json exists but the OAuth client id/secret are missing "
                "from config.yaml — reconnect in Settings")
        return YTMusic(path, oauth_credentials=creds)
    return YTMusic(path)
