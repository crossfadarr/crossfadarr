#!/usr/bin/env python3
"""Phase 4 - Review UI: browse matched artists, pick, push selected to Lidarr.

    python app.py            # serves http://127.0.0.1:5000

Reads data/matches.json + data/artwork.json + data/ytm_thumbs.json + config.yaml.
Nothing is sent to Lidarr until you tick artists and click "Add selected".
Card grid (with artwork) or compact list; selection stays in sync across views.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.parse
from datetime import timedelta
from pathlib import Path

import yaml
from flask import (Flask, jsonify, redirect, render_template_string, request,
                   session)
from werkzeug.security import check_password_hash, generate_password_hash

import scanner
import ytm_client
from curl_to_auth import curl_to_headers_raw
from fsio import write_json_atomic
from lidarr import Lidarr

CONFIG_PATH = "config.yaml"
ADDED_LOG = os.path.join("data", "added_log.json")

app = Flask(__name__)
LID = Lidarr()


# ---- P5.4: optional Forms login (arr-style) ---------------------------------

def _read_cfg() -> dict:
    try:
        return yaml.safe_load(open(CONFIG_PATH, encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def _write_cfg(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)


def _load_or_create_secret() -> str:
    """Persistent Flask session secret (config.yaml), created on first run."""
    cfg = _read_cfg()
    sk = cfg.get("secret_key")
    if not sk:
        sk = secrets.token_hex(32)
        cfg["secret_key"] = sk
        _write_cfg(cfg)
    return sk


app.secret_key = _load_or_create_secret()
app.permanent_session_lifetime = timedelta(days=30)

AUTH = dict(_read_cfg().get("auth") or {})  # enabled / username / password_hash


@app.before_request
def _require_login():
    if not AUTH.get("enabled"):
        return None
    if request.path in ("/login", "/favicon.svg"):
        return None
    if session.get("authed"):
        return None
    if request.path.startswith("/api/") or request.method != "GET":
        return jsonify({"ok": False, "error": "authentication required"}), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH.get("enabled"):
        return redirect("/")
    error = None
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = request.form.get("password") or ""
        pw_hash = AUTH.get("password_hash") or ""
        if pw_hash and u == AUTH.get("username") and check_password_hash(pw_hash, p):
            session["authed"] = True
            session.permanent = True
            return redirect("/")
        time.sleep(0.6)  # soften brute-force attempts
        error = "Invalid username or password"
    return render_template_string(LOGIN_TEMPLATE, error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/settings", methods=["POST"])
def api_auth_settings():
    global AUTH
    d = request.get_json(force=True)
    enabled = bool(d.get("enabled"))
    username = (d.get("username") or "").strip()
    password = d.get("password") or ""
    cfg = _read_cfg()
    auth = cfg.get("auth") or {}
    if enabled:
        if not username:
            return jsonify({"ok": False, "error": "username required"})
        if not password and not auth.get("password_hash"):
            return jsonify({"ok": False, "error": "password required"})
        auth["username"] = username
        if password:
            auth["password_hash"] = generate_password_hash(password)
    auth["enabled"] = enabled
    cfg["auth"] = auth
    _write_cfg(cfg)
    AUTH = dict(auth)
    session["authed"] = True   # the enabling browser stays signed in
    session.permanent = True
    return jsonify({"ok": True, "enabled": enabled})

TIER_ORDER = {"green": 0, "amber": 1, "red": 2, "none": 3}


def _alt_label(x: dict) -> str:
    extra = x.get("disambiguation") or x.get("type") or ""
    return f"{x.get('name')}" + (f" — {extra}" if extra else "")


def _load_json(path: str) -> dict:
    try:
        return json.load(open(path, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _channel_id(ytm_ids: list) -> str | None:
    """YT Music channel id for the artist page.

    Library artists come back as 'MPLA' + channel id (e.g. MPLAUCxxxx), which
    is NOT a valid /channel/ url. Liked/subscription artists give the plain
    'UCxxxx'. Prefer a bare UC id; else strip the MPLA prefix.
    """
    for c in ytm_ids:
        if c.startswith("UC"):
            return c
    for c in ytm_ids:
        if c.startswith("MPLA") and c[4:].startswith("UC"):
            return c[4:]
    return None


def _ytm_url(ytm_ids: list, name: str) -> str:
    cid = _channel_id(ytm_ids or [])
    if cid:
        return f"https://music.youtube.com/channel/{cid}"
    return "https://music.youtube.com/search?q=" + urllib.parse.quote(name)


def _ytm_auth_state() -> str:
    for c in ytm_client.AUTH_CANDIDATES:
        if os.path.exists(c):
            days = int((time.time() - os.path.getmtime(c)) / 86400)
            kind = "OAuth" if c == ytm_client.OAUTH_FILE else "browser headers"
            return f"{kind} ({c}) · updated {'today' if days < 1 else f'{days}d ago'}"
    return "not set up"


def load_rows(existing: set) -> list[dict]:
    try:
        matches = json.load(open(LID.matches_file, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []  # pipeline not run yet
    tad = _load_json("data/artwork.json")
    ytm = _load_json("data/ytm_thumbs.json")
    gen = _load_json("data/genres.json")
    rgc = _load_json("data/release_counts.json")
    rows, seen = [], set()
    for a in matches:
        m = a["match"]
        best = m.get("best")
        mbid = best.get("mbid") if best else None
        in_lib = bool(mbid) and mbid in existing
        dup = bool(mbid) and mbid in seen
        if mbid:
            seen.add(mbid)
        alts = ([best] if best else []) + (m.get("alternates") or [])
        art = (tad.get(mbid) or ytm.get(mbid)) if mbid else None
        # last resort: the thumbnail YTM itself showed for this artist/track
        # (captured free during ingest; covers channels get_artist chokes on)
        art = art or a.get("thumb")
        rows.append({
            "ytm_name": a["name"],
            "sources": a.get("sources", []),
            "liked": a.get("liked_track_count", 0),
            "tier": m["tier"],
            "mbid": mbid,
            "mb_name": best.get("name") if best else None,
            "disambig": (best.get("disambiguation") or best.get("type") or "") if best else "",
            "alternates": [{"mbid": x.get("mbid"), "label": _alt_label(x)}
                           for x in alts if x.get("mbid")],
            "in_lib": in_lib,
            "dup": dup,
            "art": art,
            "initials": (a["name"].strip()[:2] or "?").upper(),
            "ytm_url": _ytm_url(a.get("ytm_ids") or [], a["name"]),
            "mb_type": (best.get("type") if best else "") or "",
            "genres": (gen.get(mbid) or []) if mbid else [],
            "genre": ((gen.get(mbid) or [""]) if mbid else [""])[0],
            "match_label": {"green": "full match", "amber": "partial match",
                            "red": "weak match", "none": "no match"}.get(m["tier"], ""),
            "addable": bool(mbid) and not in_lib and not dup,
            # P5.11 - MB lists zero release groups: adding gives an empty artist
            "no_releases": bool(mbid) and rgc.get(mbid) == 0,
        })
    rows.sort(key=lambda r: (TIER_ORDER.get(r["tier"], 9), -r["liked"], r["ytm_name"].lower()))
    return rows


@app.route("/")
def index():
    lidarr_ok = LID.configured
    existing, roots, qps, mps = set(), [], [], []
    if LID.configured:
        try:
            existing = LID.existing_mbids()
            roots, qps, mps = (LID.rootfolders(), LID.quality_profiles(),
                               LID.metadata_profiles())
        except Exception:  # noqa: BLE001 - configured but unreachable/bad key
            lidarr_ok = False
    rows = load_rows(existing)
    counts = {"green": 0, "amber": 0, "red": 0, "none": 0, "in_lib": 0, "addable": 0}
    for r in rows:
        counts[r["tier"]] = counts.get(r["tier"], 0) + 1
        if r["in_lib"]:
            counts["in_lib"] += 1
        if r["addable"]:
            counts["addable"] += 1
    types = sorted({r["mb_type"] for r in rows if r["mb_type"]})
    genres = sorted({g for r in rows for g in r["genres"]})
    return render_template_string(
        TEMPLATE, rows=rows, counts=counts, total=len(rows), types=types, genres=genres,
        rootfolders=roots, qps=qps, mps=mps, defaults=LID.defaults,
        lidarr_url=LID.url, lidarr_key=LID.key,
        configured=LID.configured, lidarr_ok=lidarr_ok,
        ytm_auth_state=_ytm_auth_state(),
        ytm_client_id=(ytm_client.client_creds() or ("", ""))[0],
        ytm_client_secret=(ytm_client.client_creds() or ("", ""))[1],
        auth_enabled=bool(AUTH.get("enabled")),
        auth_username=AUTH.get("username") or "",
        auth_has_pw=bool(AUTH.get("password_hash")),
        tadb_key=(_read_cfg().get("theaudiodb") or {}).get("api_key") or "",
    )


@app.route("/add", methods=["POST"])
def add():
    data = request.get_json(force=True)
    root = data["root"]
    qp = int(data["qp"])
    mp = int(data["mp"])
    monitored = bool(data.get("monitored", True))
    search = bool(data.get("search", True))
    monitor_new = data.get("monitor_new", "all")
    items = data.get("items", [])
    results = [
        LID.add_artist(it["mbid"], root, qp, mp, monitored, search, monitor_new)
        for it in items
    ]
    summary = {
        "added": sum(r["status"] == "added" for r in results),
        "exists": sum(r["status"] == "exists" for r in results),
        "error": sum(r["status"] == "error" for r in results),
    }
    if summary["added"]:
        LID.invalidate("mbids")  # library changed — next page load must see it
    # P5.12 - persistent add history (data/added_log.json, git-ignored):
    # Lidarr files artists under the MB canonical name (often native script),
    # so keep the YTM name alongside for findability.
    ytm_names = {it["mbid"]: it.get("name", "") for it in items}
    try:
        log = json.load(open(ADDED_LOG, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        log = []
    ts = time.strftime("%Y-%m-%d %H:%M")
    log += [{"ts": ts, "ytm_name": ytm_names.get(r["mbid"], ""),
             "lidarr_name": r.get("name"), "mbid": r["mbid"],
             "status": r["status"], "msg": r.get("msg")} for r in results]
    write_json_atomic(ADDED_LOG, log)
    return jsonify({"summary": summary, "results": results})


@app.route("/api/added")
def api_added():
    try:
        log = json.load(open(ADDED_LOG, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        log = []
    return jsonify(log[::-1])  # newest first


@app.route("/api/test", methods=["POST"])
def api_test():
    d = request.get_json(force=True)
    url, key = (d.get("url") or "").strip(), (d.get("key") or "").strip()
    if not url or not key:
        return jsonify({"ok": False, "error": "URL and API key required"})
    try:
        t = Lidarr(url=url, api_key=key)
        if not t.ping():
            return jsonify({"ok": False, "error": "connection/auth failed"})
        return jsonify({"ok": True, "artists": len(t.existing_mbids()),
                        "rootfolders": len(t.rootfolders())})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:200]})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    global LID
    d = request.get_json(force=True)
    url, key = (d.get("url") or "").strip(), (d.get("key") or "").strip()
    if not url or not key:
        return jsonify({"ok": False, "error": "URL and API key required"})
    try:
        if not Lidarr(url=url, api_key=key).ping():
            return jsonify({"ok": False, "error": "connection/auth failed — not saved"})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{str(e)[:180]} — not saved"})

    try:
        cfg = yaml.safe_load(open(CONFIG_PATH, encoding="utf-8")) or {}
    except FileNotFoundError:
        cfg = {}  # first-run save — create config.yaml from scratch
    cfg.setdefault("matches_file", "data/matches.json")
    cfg.setdefault("lidarr", {})["url"] = url
    cfg["lidarr"]["api_key"] = key
    dfl = cfg.setdefault("defaults", {})
    if d.get("root"):
        dfl["root_folder"] = d["root"]
    if d.get("qp"):
        dfl["quality_profile_id"] = int(d["qp"])
    if d.get("mp"):
        dfl["metadata_profile_id"] = int(d["mp"])
    if d.get("monitor_new"):
        dfl["monitor_new"] = d["monitor_new"]
    if "tadb_key" in d:  # optional; empty clears it
        tadb = (d.get("tadb_key") or "").strip()
        if tadb:
            cfg.setdefault("theaudiodb", {})["api_key"] = tadb
        else:
            cfg.pop("theaudiodb", None)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    LID = Lidarr()  # reload with new settings
    return jsonify({"ok": True})


# ---- P5.10: YTM OAuth device flow -------------------------------------------
# In-memory flow state, same single-user pattern as the scanner.

_OAUTH_LOCK = threading.Lock()
OAUTH_FLOW = {"state": "idle",  # idle | pending | done | error
              "user_code": None, "verification_url": None, "error": None}


def _oauth_set(**kw) -> None:
    with _OAUTH_LOCK:
        OAUTH_FLOW.update(kw)


def _save_ytm_creds(client_id: str, client_secret: str) -> None:
    try:
        cfg = yaml.safe_load(open(CONFIG_PATH, encoding="utf-8")) or {}
    except FileNotFoundError:
        cfg = {}
    ytm = cfg.setdefault("ytm", {})
    ytm["client_id"] = client_id
    ytm["client_secret"] = client_secret
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)


def _oauth_worker(creds, device_code: str, interval: float, expires_at: float) -> None:
    from ytmusicapi.auth.oauth import RefreshingToken
    while True:
        with _OAUTH_LOCK:
            if OAUTH_FLOW["state"] != "pending":
                return  # cancelled
        if time.time() > expires_at:
            _oauth_set(state="error", error="the code expired before it was "
                                            "entered — click Connect again")
            return
        time.sleep(interval)
        try:
            raw = creds.token_from_code(device_code)
        except Exception as e:  # noqa: BLE001
            _oauth_set(state="error", error=str(e)[:300])
            return
        if raw.get("access_token"):
            break
        err = raw.get("error")
        if err in (None, "authorization_pending"):
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err == "access_denied":
            _oauth_set(state="error", error="the request was declined in Google "
                                            "— click Connect to try again")
        else:
            _oauth_set(state="error", error=f"Google error: {err}")
        return

    try:
        # Mirror RefreshingToken.prompt_for_token's construction.
        tok = RefreshingToken(
            credentials=creds,
            access_token=raw["access_token"],
            refresh_token=raw["refresh_token"],
            scope=raw["scope"],
            token_type=raw["token_type"],
            expires_in=raw.get("refresh_token_expires_in", raw["expires_in"]),
        )
        tok.update(raw)
        tok.local_cache = Path(ytm_client.OAUTH_FILE)  # writes oauth.json
    except Exception as e:  # noqa: BLE001
        try:
            os.remove(ytm_client.OAUTH_FILE)
        except OSError:
            pass
        _oauth_set(state="error", error=f"could not store the token: {str(e)[:200]}")
        return
    try:
        # Same signed-in canary as the browser-headers path. Keep oauth.json on
        # failure — the token itself is real (Google approved it), and some YTM
        # endpoints can be flaky under OAuth; a kept token can be debugged/used.
        ytm_client.build(ytm_client.OAUTH_FILE).get_liked_songs(limit=1)
    except Exception as e:  # noqa: BLE001
        _oauth_set(state="error", error="Google connected and the token was "
                                        "saved, but a YT Music test call failed: "
                                        f"{str(e)[:200]}")
        return
    scanner.clear_error()
    _oauth_set(state="done", error=None)


@app.route("/api/ytm/oauth/start", methods=["POST"])
def api_oauth_start():
    d = request.get_json(force=True)
    cid = (d.get("client_id") or "").strip()
    cs = (d.get("client_secret") or "").strip()
    if not cid or not cs:
        return jsonify({"ok": False, "error": "client ID and client secret are required"})
    with _OAUTH_LOCK:
        if OAUTH_FLOW["state"] == "pending":
            return jsonify({"ok": False, "error": "a connect attempt is already running"})
    from ytmusicapi.auth.oauth import OAuthCredentials
    creds = OAuthCredentials(client_id=cid, client_secret=cs)
    try:
        code = creds.get_code()
    except Exception as e:  # noqa: BLE001
        msg = str(e)[:300]
        if "OAuth client failure" in msg or "invalid_client" in msg.lower():
            msg = ("Google rejected the client — check the ID and secret, that the "
                   "client type is “TVs and Limited Input devices”, and that "
                   "YouTube Data API v3 is enabled on the project")
        return jsonify({"ok": False, "error": msg})
    _save_ytm_creds(cid, cs)
    _oauth_set(state="pending", user_code=code["user_code"],
               verification_url=code["verification_url"], error=None)
    threading.Thread(
        target=_oauth_worker,
        args=(creds, code["device_code"], float(code.get("interval", 5)),
              time.time() + float(code.get("expires_in", 1800))),
        daemon=True).start()
    return jsonify({"ok": True, "user_code": code["user_code"],
                    "verification_url": code["verification_url"]})


@app.route("/api/ytm/oauth/status")
def api_oauth_status():
    with _OAUTH_LOCK:
        st = dict(OAUTH_FLOW)
    st["ok"] = True
    st["auth_state"] = _ytm_auth_state()
    return jsonify(st)


@app.route("/api/ytm/oauth/cancel", methods=["POST"])
def api_oauth_cancel():
    _oauth_set(state="idle", user_code=None, verification_url=None, error=None)
    return jsonify({"ok": True})


@app.route("/api/ytm/auth", methods=["POST"])
def api_ytm_auth():
    """P5.3 - paste browser headers (raw or Copy-as-cURL) to refresh YTM auth.

    Writes to a temp file and validates with a real liked-songs call before
    replacing auth.json, so a bad paste never clobbers working auth. The pasted
    content is never logged or echoed back.
    """
    raw = ((request.get_json(force=True).get("raw") or "")).strip()
    if not raw:
        return jsonify({"ok": False, "error": "paste headers or a cURL command first"})
    tmp = "auth.json.new"
    try:
        headers_raw = curl_to_headers_raw(raw) if raw.lower().startswith("curl") else raw
        from ytmusicapi import YTMusic, setup
        setup(filepath=tmp, headers_raw=headers_raw)
        # get_liked_songs is the reliable signed-out canary (library calls
        # return empty instead of failing when cookies are stale).
        YTMusic(tmp).get_liked_songs(limit=1)
    except (Exception, SystemExit) as e:  # noqa: BLE001 - incl. curl parser's exit
        try:
            os.remove(tmp)
        except OSError:
            pass
        msg = str(e)[:300] or "invalid headers"
        low = msg.lower()
        if "oauth json provided" in low:
            # ytmusicapi's confusing fallback when the authorization header is
            # missing — the paste didn't come from a full /browse request.
            msg = ("headers incomplete — copy the full request (needs the "
                   "authorization header): use Copy as cURL (bash) on a POST "
                   "/browse request at music.youtube.com")
        elif any(k in low for k in ("twocolumnbrowseresultsrenderer",
                                    "sign in", "signed in", "401")):
            msg = ("headers parsed, but YT Music says signed-out — copy them from "
                   "a logged-in music.youtube.com tab (a POST to /browse)")
        return jsonify({"ok": False, "error": msg})
    os.replace(tmp, "auth.json")
    scanner.clear_error()
    return jsonify({"ok": True, "detail": "connected — your library is reachable",
                    "state": _ytm_auth_state()})


@app.route("/api/scan/start", methods=["POST"])
def api_scan_start():
    ok, err = scanner.start()
    if ok:
        return jsonify({"ok": True})
    code = 409 if err["error_kind"] == "busy" else 400
    return jsonify({"ok": False, **err}), code


@app.route("/api/scan/status")
def api_scan_status():
    return jsonify(scanner.status())


FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#ff0000"/><stop offset="1" stop-color="#10a34a"/>'
    '</linearGradient></defs>'
    '<circle cx="50" cy="50" r="48" fill="url(#g)"/>'
    '<path d="M42 33 L42 67 L70 50 Z" fill="#fff"/></svg>'
)


@app.route("/favicon.svg")
def favicon():
    return app.response_class(FAVICON_SVG, mimetype="image/svg+xml")


LOGIN_TEMPLATE = r"""
<!doctype html><html><head><meta charset="utf-8"><title>Crossfadarr — sign in</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
  :root{color-scheme:dark}
  body{font-family:"Roboto","YouTube Sans",system-ui,Segoe UI,Arial,sans-serif;margin:0;
       background:#0f0f0f;color:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh}
  .card{background:#212121;border:1px solid #303030;border-radius:14px;padding:34px 38px;
        width:min(360px,90vw);box-shadow:0 20px 60px -20px #000;text-align:center}
  .brand{display:flex;align-items:center;justify-content:center;gap:10px;font-weight:800;
         font-size:24px;margin-bottom:24px}
  input{width:100%;box-sizing:border-box;background:#0e1218;color:#fff;border:1px solid #303030;
        border-radius:8px;padding:10px 12px;font-size:14px;margin-bottom:12px}
  button{width:100%;background:#ff0000;color:#fff;border:0;border-radius:8px;padding:11px;
         font-weight:700;font-size:14px;cursor:pointer}
  .err{color:#fca5a5;font-size:13px;margin:0 0 12px}
</style></head><body>
<form class="card" method="post" action="/login">
  <div class="brand">
    <svg viewBox="0 0 100 100" width="30" height="30" aria-hidden="true">
      <circle cx="50" cy="50" r="46" fill="#ff0000"/>
      <path d="M42 33 L42 67 L70 50 Z" fill="#fff"/>
    </svg>
    Crossfadarr
  </div>
  {% if error %}<p class="err">{{error}}</p>{% endif %}
  <input name="username" placeholder="Username" autofocus autocomplete="username">
  <input name="password" type="password" placeholder="Password" autocomplete="current-password">
  <button type="submit">Sign in</button>
</form>
</body></html>
"""

TEMPLATE = r"""
<!doctype html><html><head><meta charset="utf-8"><title>Crossfadarr</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
  :root{color-scheme:dark; --red:#ff0000; --bg:#0f0f0f; --panel:#212121; --line:#303030; --txt:#ffffff; --mut:#aaaaaa}
  *{box-sizing:border-box}
  body{font-family:"Roboto","YouTube Sans",system-ui,Segoe UI,Arial,sans-serif;margin:0;background:var(--bg);color:var(--txt)}
  a{color:inherit}
  header{position:sticky;top:0;z-index:9;background:#212121;border-bottom:1px solid var(--line)}
  .bar{display:flex;align-items:center;gap:14px;padding:12px 18px 6px}
  .brand{display:flex;align-items:center;gap:10px;font-weight:800;letter-spacing:.2px}
  .brand .word{font-size:24px;line-height:1;color:var(--txt)}
  .brand .arrow{color:var(--mut);font-weight:500}
  .brand .lid{color:#22c55e}
  .brand .logo{flex:0 0 auto}
  .brand .logo .circ{animation:hue 4.2s ease-in-out infinite}
  .brand .logo .play{animation:fp 4.2s ease-in-out infinite}
  .brand .logo .bars{animation:fb 4.2s ease-in-out infinite}
  @keyframes hue{0%,16%{fill:#ff0000}50%,66%{fill:#10a34a}100%{fill:#ff0000}}
  @keyframes fp{0%,16%{opacity:1}42%,72%{opacity:0}100%{opacity:1}}
  @keyframes fb{0%,20%{opacity:0}50%,64%{opacity:1}94%,100%{opacity:0}}
  .grow{flex:1}
  .stats{display:flex;gap:8px}
  .stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:5px 12px;text-align:center;min-width:64px}
  .stat b{display:block;font-size:16px}.stat span{font-size:11px;color:var(--mut)}
  .stat.hot b{color:#86efac}
  .statbtn{cursor:pointer;user-select:none;transition:border-color .15s,background .15s}
  .statbtn:hover{border-color:#555}
  .statbtn.on{border-color:var(--red);background:#241416}
  .statbtn .eye{vertical-align:-2px;margin-left:2px;opacity:.85}
  .statbtn .slash{display:inline}
  .statbtn.on .slash{display:none}
  .toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;padding:8px 18px 12px}
  .seg{display:flex;gap:6px;flex-wrap:wrap}
  .muted{color:var(--mut)}
  select,input[type=text]{background:#0e1218;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:6px 9px}
  label.ctl{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--mut)}
  button{background:var(--red);color:#fff;border:0;border-radius:8px;padding:9px 16px;font-weight:700;cursor:pointer}
  button.secondary{background:#2a3140;color:#dbe2ea}
  .pill{padding:2px 9px;border-radius:999px;font-size:11px;font-weight:700}
  .green{background:#14532d;color:#86efac}.amber{background:#78350f;color:#fcd34d}
  .red{background:#7f1d1d;color:#fca5a5}.none{background:#374151;color:#cbd5e1}
  .flt,.vw{padding:5px 11px;border-radius:999px;border:1px solid var(--line);background:#0f0f0f;color:#ddd;cursor:pointer;font-size:13px;display:inline-flex;align-items:center}
  .flt .dot{width:10px;height:10px;margin-right:6px;box-shadow:none}
  .flt.active{background:var(--red);color:#fff;border-color:var(--red)}
  .flt.f-green{border-color:#2ecc71}.flt.f-amber{border-color:#f1c40f}.flt.f-none{border-color:#888}
  .flt.f-green.active{background:#14532d;color:#c9f7d5;border-color:#2ecc71}
  .flt.f-amber.active{background:#78350f;color:#ffe9b0;border-color:#f1c40f}
  .flt.f-none.active{background:#3a3a3a;color:#eee;border-color:#888}
  .vw.active{background:#2563eb;color:#fff;border-color:#2563eb}
  #results{padding:6px 18px;min-height:6px;font-size:14px}

  .item{position:relative;border-radius:10px;transition:background .15s;content-visibility:auto}
  .item.disabled{opacity:.45}
  .help .ic{vertical-align:-3px}
  .help:hover .ic,.help:focus .ic{color:#fff}
  .thumb{position:relative}
  .art,.ph{background:#282828;object-fit:cover;display:block;border-radius:50%}
  .ph{display:flex;align-items:center;justify-content:center;font-weight:800;color:#777}
  .pickwrap{position:absolute;top:8px;left:8px;z-index:3;background:rgba(0,0,0,.6);border-radius:6px;padding:2px 5px;line-height:0}
  .pick{transform:scale(1.3);cursor:pointer;accent-color:var(--red)}
  .dot{width:12px;height:12px;border-radius:50%;display:inline-block;box-shadow:0 0 0 2px #0009}
  .dot.green{background:#2ecc71}.dot.amber{background:#f1c40f}.dot.red{background:#e74c3c}.dot.none{background:#888}
  .name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .sub{font-size:12px;color:var(--mut);margin:3px 0}
  .tag{font-size:10px;color:var(--mut);border:1px solid var(--line);border-radius:5px;padding:0 4px;margin-right:3px;text-transform:capitalize}
  .liked{color:#f472b6}
  .gchip{font-size:10px;color:#c7d2fe;background:#1e3a5f;border-radius:5px;padding:0 5px;margin-left:4px;text-transform:capitalize}
  .norel{font-size:10px;color:#fcd34d;background:#78350f;border-radius:5px;padding:0 5px;margin-left:4px;cursor:help}
  .help{position:relative;cursor:help;color:var(--mut);font-style:normal;outline:none}
  .help .pop{display:none;position:absolute;top:130%;left:0;z-index:20;width:290px;background:#111;border:1px solid var(--line);border-radius:8px;padding:9px 11px;font-size:12px;line-height:1.55;color:#ddd;box-shadow:0 8px 24px -8px #000;white-space:normal;font-weight:400}
  .help:hover .pop,.help:focus .pop{display:block}
  .mbsel{max-width:100%;font-size:12px;margin-top:5px}
  .ytm{display:inline-flex;align-items:center;justify-content:center;text-decoration:none;border-radius:50%}

  /* CARDS — YTM style: circular art, hover play */
  #items.resizing{display:none !important}
  #items.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(172px,1fr));gap:6px;padding:16px}
  .cards .item{display:flex;flex-direction:column;padding:10px;contain-intrinsic-size:auto 240px}
  .cards .item:hover{background:#1f1f1f}
  .cards .thumb{width:100%;aspect-ratio:1/1;margin-bottom:9px}
  .cards .art,.cards .ph{width:100%;height:100%;font-size:34px}
  .cards .dot{position:absolute;bottom:8px;right:8px}
  .cards .ytm{position:absolute;inset:0;margin:auto;width:48px;height:48px;background:#000000cc;opacity:0;transition:opacity .15s}
  .cards .item:hover .ytm{opacity:1}
  .cards .name{font-size:14px}

  /* LIST */
  #items.list{display:flex;flex-direction:column;padding:10px 18px;gap:2px}
  .list .item{display:flex;align-items:center;gap:12px;padding:6px 10px;contain-intrinsic-size:auto 58px}
  .list .item:hover{background:#1f1f1f}
  .list .thumb{width:46px;height:46px;flex:0 0 46px}
  .list .art,.list .ph{width:46px;height:46px;font-size:16px}
  .list .pickwrap{top:-3px;left:-3px;padding:0 2px}
  .list .dot{position:absolute;bottom:-1px;right:-1px}
  .list .ytm{position:absolute;top:-4px;right:-4px;width:22px;height:22px;box-shadow:0 1px 4px #000}
  .list .body{display:flex;align-items:center;gap:14px;flex:1;min-width:0}
  .list .name{flex:0 0 220px;font-size:14px}.list .sub{margin:0}
  .list .match{flex:1;min-width:0}.list .mbsel{margin:0;max-width:440px}

  .gear{cursor:pointer;font-size:20px;padding:2px 6px;border-radius:8px;color:#cbd5e1}
  .gear:hover{background:#0e1218}
  .modal{position:fixed;inset:0;background:#000a;display:flex;align-items:center;justify-content:center;z-index:50}
  .sheet{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:26px 30px;width:min(700px,94vw);max-height:90vh;display:flex;flex-direction:column;box-shadow:0 20px 60px -20px #000;font-size:14px}
  .sheet h3{margin:0 0 16px;font-size:19px}
  .sheetbody{overflow-y:auto;min-height:0;padding-right:6px}
  .sheethead{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
  .sheethead h3{margin:0}
  .closex{cursor:pointer;color:var(--mut);font-size:18px;line-height:1;padding:4px 9px;border-radius:8px}
  .closex:hover{background:#0e1218;color:#fff}
  .sheet .foot{flex:0 0 auto;border-top:1px solid var(--line);padding-top:14px}
  .sheet label{display:block;font-size:13px;color:var(--mut);margin:14px 0 5px}
  .sheet input,.sheet select{width:100%;font-size:14px;padding:8px 11px}
  .sheet textarea{width:100%;background:#0e1218;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:8px 11px;font-family:ui-monospace,Consolas,monospace;font-size:12px;resize:vertical}
  .hint{font-size:12.5px;color:var(--mut);margin-top:7px;line-height:1.6}
  .hint a{color:#93b4e6;text-decoration:underline}
  .histwrap{max-height:60vh;overflow-y:auto}
  .histwrap table{width:100%;border-collapse:collapse;font-size:13px}
  .histwrap th{text-align:left;color:var(--mut);font-weight:500;font-size:12px;padding:4px 10px 6px 0;border-bottom:1px solid var(--line)}
  .histwrap td{padding:6px 10px 6px 0;border-bottom:1px solid #1b1b1b;vertical-align:top}
  .histwrap .st-added{color:#86efac}.histwrap .st-exists{color:#cbd5e1}.histwrap .st-error{color:#fca5a5}
  .authbox{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-top:10px}
  .authbox .meth{font-weight:700;font-size:13.5px}
  .oacode{user-select:all;cursor:pointer;background:#0e1218;border:1px solid var(--line);border-radius:6px;padding:3px 10px;font-family:ui-monospace,Consolas,monospace;font-size:16px;letter-spacing:2px}
  .sheet .inline{display:flex;gap:8px;align-items:center}
  .sheet hr{border:0;border-top:1px solid var(--line);margin:16px 0}
  .sheet .foot{display:flex;gap:10px;align-items:center;margin-top:16px}
  .ok{color:#86efac}.err{color:#fca5a5}
  .setupbar{background:#78350f;color:#fde68a;padding:10px 18px;font-size:14px;display:flex;align-items:center;gap:10px}
  .setupbar button{padding:5px 12px}

  /* P5.2 - in-app scan progress strip */
  .setupbar.scan{background:#1f2937;color:#cbd5e1;flex-wrap:wrap}
  .setupbar.scan.err{background:#78350f;color:#fde68a}
  .setupbar.scan.ok{background:#14351f;color:#86efac}
  .scantrack{flex:1 1 160px;min-width:120px;height:6px;background:#0e1218;border-radius:999px;overflow:hidden}
  #scanhint{flex-basis:100%;font-size:12px;color:#8b98a8}
  #scanhint:empty{display:none}
  .scanfill{height:100%;width:0%;background:var(--red);border-radius:999px;transition:width .4s}
  .setupbar.scan.ok .scanfill{background:#22c55e}
  #scanbtn{white-space:nowrap;display:inline-flex;align-items:center;gap:7px}
  #scanbtn.scanning svg{animation:spin 1.1s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}

  /* organised control panels */
  .panels{display:flex;gap:12px;flex-wrap:wrap;padding:10px 16px 14px}
  .panel{background:#181818;border:1px solid var(--line);border-radius:12px;padding:12px 14px;flex:1 1 250px;min-width:230px}
  .panel.grow2{flex:2 1 360px}
  .panel .phead{display:flex;align-items:center;justify-content:space-between;font-size:14px;font-weight:700;letter-spacing:.3px;color:#e8e8e8;margin-bottom:11px}
  .vwseg{display:inline-flex;gap:6px}
  .vwseg .vw{padding:4px 10px;font-size:14px}
  .frow{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:9px}
  .tiers{margin-top:10px}
  #search{width:100%}
  .searchrow{margin-top:0}
  .searchrow .searchwrap{flex:1 1 180px;min-width:130px;position:relative;display:flex}
  .searchwrap #search{width:100%;padding-right:30px}
  #searchclear{position:absolute;right:7px;top:50%;transform:translateY(-50%);display:none;cursor:pointer;color:var(--mut);line-height:0;padding:4px;border-radius:6px}
  #searchclear:hover{color:#fff;background:#1f2937}
  .small{font-size:12px}
  .toggle{display:inline-flex;border:1px solid var(--line);border-radius:999px;overflow:hidden;margin-top:10px}
  .toggle .tg{padding:5px 18px;cursor:pointer;font-size:13px;color:#ccc}
  .toggle .tg.active{background:var(--red);color:#fff}
  .gcol{display:block;margin-top:10px}
  .ms{position:relative;margin-top:5px;background:#0e1218;border:1px solid var(--line);border-radius:8px;padding:4px 6px;display:flex;flex-wrap:wrap;gap:5px;align-items:center;cursor:text}
  .ms-tags{display:contents}
  .mtag{background:#1e3a5f;color:#c7d2fe;border-radius:6px;padding:1px 4px 1px 7px;font-size:12px;display:inline-flex;gap:6px;align-items:center;text-transform:capitalize}
  .mtag b{cursor:pointer;color:#93b4e6;font-weight:700}
  .ms input{border:0;background:transparent;flex:1;min-width:90px;padding:3px;color:var(--txt);outline:none}
  .ms-drop{display:none;position:absolute;top:100%;left:0;right:0;z-index:30;background:#111;border:1px solid var(--line);border-radius:8px;margin-top:4px;max-height:230px;overflow:auto;box-shadow:0 8px 24px -8px #000}
  .ms-drop.open{display:block}
  .ms-drop div{padding:6px 10px;cursor:pointer;font-size:13px;text-transform:capitalize}
  .ms-drop div:hover,.ms-drop div.hl{background:#2563eb;color:#fff}
</style></head><body>
<header>
  <div class="bar">
    <div class="brand">
      <svg class="logo" viewBox="0 0 100 100" width="30" height="30" aria-hidden="true">
        <circle class="circ" cx="50" cy="50" r="46" fill="#ff0000"/>
        <path class="play" d="M42 33 L42 67 L70 50 Z" fill="#fff"/>
        <g class="bars" fill="#fff"><rect x="34" y="40" width="9" height="20" rx="4.5"/><rect x="45.5" y="32" width="9" height="36" rx="4.5"/><rect x="57" y="44" width="9" height="12" rx="4.5"/></g>
      </svg>
      <span class="word">Crossfadarr</span>
    </div>
    <div class="grow"></div>
    <div class="stats">
      <div class="stat"><b>{{total}}</b><span>candidates</span></div>
      <div class="stat hot"><b>{{counts.addable}}</b><span>addable</span></div>
      <div class="stat statbtn" id="inlibStat" onclick="toggleInlib()" title="Show / hide artists already in your Lidarr library">
        <b>{{counts.in_lib}}</b><span>in Lidarr <svg class="eye" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="2.6"/><line class="slash" x1="4" y1="4" x2="20" y2="20"/></svg></span>
      </div>
    </div>
    <button class="secondary" id="scanbtn" onclick="startScan()" title="Re-scan your YouTube Music library and rebuild the artist list">
      <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
      <span id="scanlbl">Refresh from YouTube Music</span>
    </button>
    <span class="gear" onclick="openSettings()" title="Settings">⚙</span>
  </div>
  <div class="panels">
    <section class="panel grow2">
      <div class="phead"><span>Search &amp; filters</span></div>
      <div class="frow searchrow">
        <span class="searchwrap">
          <input type="text" id="search" placeholder="search artists…" oninput="applyFilters();syncSearchClear()">
          <span id="searchclear" onclick="clearSearch()" title="Clear search"><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><line x1="5" y1="5" x2="19" y2="19"/><line x1="19" y1="5" x2="5" y2="19"/></svg></span>
        </span>
        <span class="seg vwseg">
          <span class="vw active" data-v="cards" title="Card view">▦ cards</span>
          <span class="vw" data-v="list" title="List view">☰ list</span>
        </span>
      </div>
      <div class="seg tiers">
        <span class="flt active" data-f="all">all</span>
        <span class="flt f-green" data-f="green"><i class="dot green"></i>full {{counts.green}}</span>
        <span class="flt f-amber" data-f="amber"><i class="dot amber"></i>partial {{counts.amber}}</span>
        <span class="flt f-none" data-f="none"><i class="dot none"></i>no match {{counts.none}}</span>
      </div>
      <div class="frow">
        <label class="ctl">Source
          <span class="help" tabindex="0"><svg class="ic" viewBox="0 0 24 24" width="15" height="15" aria-label="help"><circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="7.7" r="1.25" fill="currentColor"/><rect x="10.9" y="10.6" width="2.2" height="6.7" rx="1.1" fill="currentColor"/></svg><span class="pop"><b>Library</b> — artists whose music you <em>saved</em> to your YT Music library.<br><b>Subscription</b> — artist channels you <em>follow</em> (no music saved).<br><b>Liked</b> — artists pulled from your <em>thumbed-up</em> songs.</span></span>
          <select id="fsource" onchange="applyFilters()"><option value="all">All</option><option value="library">Library</option><option value="subscription">Subscription</option><option value="liked">Liked</option></select></label>
        <label class="ctl">Type <select id="ftype" onchange="applyFilters()"><option value="all">All</option>{% for t in types %}<option value="{{t}}">{{t}}</option>{% endfor %}</select></label>
        <label class="ctl">Sort <select id="sort" onchange="applySort()"><option value="liked">Most liked</option><option value="name_az">Name A–Z</option><option value="name_za">Name Z–A</option><option value="tier">Confidence</option></select></label>
        <label class="ctl" title="Artists MusicBrainz lists no releases for — adding them creates an empty Lidarr artist"><input type="checkbox" id="fnorel" onchange="applyFilters()"> hide no-release</label>
      </div>
      <label class="ctl gcol">Genre
        <div class="ms" id="genreMS" onclick="document.getElementById('genreInput').focus()">
          <span class="ms-tags" id="genreTags"></span>
          <input id="genreInput" placeholder="type a genre…" autocomplete="off" oninput="genreDropdown()" onfocus="genreDropdown()" onkeydown="genreKey(event)">
          <div class="ms-drop" id="genreDrop"></div>
        </div>
      </label>
    </section>

    <section class="panel grow2">
      <div class="phead"><span>Add to Lidarr</span></div>
      <div class="frow">
        <label class="ctl">Root <select id="root">{% for r in rootfolders %}<option value="{{r.path}}" {{'selected' if r.path==defaults.get('root_folder') else ''}}>{{r.path}}</option>{% endfor %}</select></label>
        <label class="ctl">Quality <select id="qp">{% for q in qps %}<option value="{{q.id}}" {{'selected' if q.id==defaults.get('quality_profile_id') else ''}}>{{q.name}}</option>{% endfor %}</select></label>
        <label class="ctl">Metadata
          <span class="help" tabindex="0"><svg class="ic" viewBox="0 0 24 24" width="15" height="15" aria-label="help"><circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="7.7" r="1.25" fill="currentColor"/><rect x="10.9" y="10.6" width="2.2" height="6.7" rx="1.1" fill="currentColor"/></svg><span class="pop">Lidarr only tracks the <b>release types</b> this profile allows — albums, EPs, singles, live recordings and so on.<br>The <b>Standard</b> profile is albums-only, so an artist who mainly releases EPs or singles (common in K-pop) can appear to have no music at all.<br>For those artists, pick a profile that includes EPs and singles.</span></span>
          <select id="mp">{% for m in mps %}<option value="{{m.id}}" {{'selected' if m.id==defaults.get('metadata_profile_id') else ''}}>{{m.name}}</option>{% endfor %}</select></label>
      </div>
      <div class="frow">
        <label class="ctl">New albums <select id="monitor_new"><option value="all" selected>All</option><option value="new">New</option><option value="none">None</option></select></label>
        <label class="ctl"><input type="checkbox" id="monitored" checked onchange="syncSearch()"> monitor</label>
        <label class="ctl" id="searchlbl" title="Search runs only for monitored artists"><input type="checkbox" id="search_on_add" checked> search</label>
      </div>
      <div class="frow">
        <button class="secondary" onclick="checkVisible(true)">select visible</button>
        <button class="secondary" onclick="checkVisible(false)">clear</button>
        <button class="secondary" onclick="openHistory()" title="Everything added to Lidarr from here">History</button>
        <button id="addbtn" onclick="addSelected()">Add selected (<span id="selcount">0</span>)</button>
      </div>
    </section>
  </div>
</header>
<div id="scanbar" class="setupbar scan" style="display:none">
  <span id="scanmsg"></span>
  <span id="scanstage" class="muted small"></span>
  <div class="scantrack"><div class="scanfill" id="scanfill"></div></div>
  <button class="secondary" id="scanretry" style="display:none" onclick="startScan()">Retry scan</button>
  <button class="secondary" id="scanreload" style="display:none" onclick="location.reload()">Reload</button>
  <span id="scanhint"></span>
</div>
{% if not configured %}
<div class="setupbar">⚠ Lidarr isn't configured yet. <button onclick="openSettings()">Open settings</button> to connect your Lidarr endpoint &amp; API key.</div>
{% elif not lidarr_ok %}
<div class="setupbar">⚠ Can't reach Lidarr at <code>{{lidarr_url}}</code> (check it's running / the API key). <button onclick="openSettings()">Settings</button></div>
{% endif %}
{% if total == 0 %}
<div class="setupbar" style="background:#1f2937;color:#cbd5e1">No matched artists yet — click <b>⟳ Refresh from YouTube Music</b> above to scan your library.</div>
{% endif %}
<div id="results"></div>
<div id="items" class="cards">
{% for r in rows %}
  <div class="item {{'disabled' if not r.addable else ''}}" data-tier="{{r.tier}}" data-name="{{r.ytm_name|lower}}" data-addable="{{'1' if r.addable else '0'}}" data-mbid="{{r.mbid or ''}}" data-liked="{{r.liked}}" data-sources="{{r.sources|join(',')}}" data-type="{{r.mb_type}}" data-genres="{{r.genres|join(',')}}" data-inlib="{{'1' if r.in_lib else '0'}}" data-norel="{{'1' if r.no_releases else '0'}}">
    <div class="thumb">
      <span class="pickwrap">{% if r.addable %}<input type="checkbox" class="pick" onchange="updateCount()">{% endif %}</span>
      {% if r.art %}<img class="art" loading="lazy" src="{{r.art}}" alt="">{% else %}<div class="ph">{{r.initials}}</div>{% endif %}
      <span class="dot {{r.tier}}" title="{{r.match_label}}"></span>
      <a class="ytm" href="{{r.ytm_url}}" target="_blank" rel="noopener" title="Open in YouTube Music">
        <svg viewBox="0 0 24 24" width="20" height="20"><circle cx="12" cy="12" r="12" fill="#ff0000"/><path d="M10 8l6 4-6 4z" fill="#fff"/></svg>
      </a>
    </div>
    <div class="body">
      <div class="name" title="{{r.ytm_name}}{% if r.mb_name %} → {{r.mb_name}}{% endif %}">{{r.ytm_name}}</div>
      <div class="sub">{% for s in r.sources %}<span class="tag">{{s}}</span>{% endfor %}{% if r.liked %}<span class="liked">♥{{r.liked}}</span>{% endif %}{% if r.genre %}<span class="gchip">{{r.genre}}</span>{% endif %}{% if r.no_releases %}<span class="norel" title="MusicBrainz lists no releases for this match — adding it to Lidarr would create an empty artist. It may also be the wrong entity: check the match dropdown for a better entry.">no releases</span>{% endif %}</div>
      <div class="match">
        {% if r.in_lib %}<span class="muted">✓ in Lidarr</span>
        {% elif r.dup %}<span class="muted">dup — {{r.mb_name}}</span>
        {% elif r.alternates %}<select class="mbsel">{% for alt in r.alternates %}<option value="{{alt.mbid}}">{{alt.label}}</option>{% endfor %}</select>
        {% else %}<span class="muted">no match</span>{% endif %}
      </div>
    </div>
  </div>
{% endfor %}
</div>

<div id="history" class="modal" style="display:none" onclick="if(event.target===this)closeHistory()">
  <div class="sheet">
    <h3>Add history</h3>
    <div id="histbody" class="histwrap"><span class="muted">loading…</span></div>
    <div class="foot"><button class="secondary" type="button" onclick="closeHistory()">Close</button></div>
  </div>
</div>

<div id="settings" class="modal" style="display:none">
  <div class="sheet">
    <div class="sheethead">
      <h3>⚙ Settings</h3>
      <span class="closex" onclick="closeSettings()" title="Close (unsaved changes are discarded)">✕</span>
    </div>
    <div class="sheetbody">
    <label style="margin-top:0">Lidarr URL</label>
    <input id="s_url" type="text" value="{{lidarr_url}}" placeholder="http://192.168.0.54:8686">
    <label>API key</label>
    <div class="inline">
      <input id="s_key" type="password" value="{{lidarr_key}}">
      <button class="secondary" type="button" onclick="togglekey()">show</button>
    </div>
    <div class="inline" style="margin-top:10px">
      <button class="secondary" type="button" onclick="testConn()">Test connection</button>
      <span id="s_test"></span>
    </div>
    <hr>
    <label>Default root folder</label>
    <select id="s_root">{% for r in rootfolders %}<option value="{{r.path}}" {{'selected' if r.path==defaults.get('root_folder') else ''}}>{{r.path}}</option>{% endfor %}</select>
    <label>Default quality profile</label>
    <select id="s_qp">{% for q in qps %}<option value="{{q.id}}" {{'selected' if q.id==defaults.get('quality_profile_id') else ''}}>{{q.name}}</option>{% endfor %}</select>
    <label>Default metadata profile</label>
    <select id="s_mp">{% for m in mps %}<option value="{{m.id}}" {{'selected' if m.id==defaults.get('metadata_profile_id') else ''}}>{{m.name}}</option>{% endfor %}</select>
    <div class="hint">Sets which release types Lidarr tracks for newly added artists. The "Standard" profile only includes albums — if an artist mainly releases EPs or singles, choose a profile that includes those too.</div>
    <label>Default monitor new albums</label>
    <select id="s_mn">
      <option value="all" {{'selected' if defaults.get('monitor_new','all')=='all' else ''}}>All</option>
      <option value="new" {{'selected' if defaults.get('monitor_new')=='new' else ''}}>New</option>
      <option value="none" {{'selected' if defaults.get('monitor_new')=='none' else ''}}>None</option>
    </select>
    <label>TheAudioDB API key <span class="muted">(optional)</span></label>
    <input id="s_tadb" type="text" value="{{tadb_key}}" placeholder="blank = free-tier key (123)">
    <div class="hint">Artist portraits come from <a href="https://www.theaudiodb.com" target="_blank" rel="noopener">TheAudioDB</a>'s <b>v1</b> API — the same source Lidarr uses. Left blank, Crossfadarr uses their public free-tier key (123) and keeps requests within the free rate limit. If you support them on Patreon, enter your private key here — artwork fetches then run about 3× faster on a first scan. Artists without a portrait fall back to YouTube Music images.</div>
    <hr>
    <label>Security</label>
    <div class="authbox">
      <div class="inline">
        <label class="ctl" style="margin:0">Authentication
          <select id="sec_enabled">
            <option value="0" {{'selected' if not auth_enabled else ''}}>Disabled</option>
            <option value="1" {{'selected' if auth_enabled else ''}}>Forms (login page)</option>
          </select>
        </label>
      </div>
      <div class="inline" style="margin-top:8px">
        <input id="sec_user" type="text" placeholder="Username" value="{{auth_username}}" autocomplete="off">
      </div>
      <div class="inline" style="margin-top:8px">
        <input id="sec_pass" type="password" placeholder="{{'New password (blank = keep current)' if auth_has_pw else 'Password'}}" autocomplete="new-password">
      </div>
      {% if auth_enabled %}
      <div class="inline" style="margin-top:8px">
        <button class="secondary" type="button" onclick="signOut()">Sign out</button>
      </div>
      {% endif %}
      <div class="hint">Optional arr-style login covering every page and API route. Applied by the <b>Save</b> button below. The password is stored as a salted hash in <code>config.yaml</code>. Locked out? Edit <code>config.yaml</code> → <code>auth: enabled: false</code> and restart.</div>
    </div>

    <hr>
    <label>YouTube Music auth <span class="muted" id="ytm_state">· {{ytm_auth_state}}</span></label>

    <div class="authbox">
      <div class="meth">Browser headers <span class="muted">(the working method)</span></div>
      <textarea id="ytm_raw" rows="4" placeholder="Paste raw request headers or a &quot;Copy as cURL (bash)&quot; command here" style="margin-top:8px"></textarea>
      <div class="hint"><a href="https://music.youtube.com" target="_blank" rel="noopener">music.youtube.com</a> (logged in) → F12 → Network → filter “browse” → right-click a POST /browse request → Copy → <b>Copy as cURL (bash)</b> → paste above. Validated before saving; nothing is stored if it fails.</div>
      <div class="hint"><b>Make it last:</b> do this from a <b>private/incognito window</b>, then close that window <em>without logging out</em>. Google rotates the cookies of sessions that stay active — a snapshot from your everyday browser can die within hours, while one from a closed incognito session typically lasts weeks.</div>
      <div class="inline" style="margin-top:8px">
        <button class="secondary" type="button" id="ytm_save" onclick="saveYtmAuth()">Save YTM auth</button>
        <span id="ytm_msg"></span>
      </div>
    </div>

    <div class="authbox">
      <div class="meth">OAuth <span class="muted">(currently rejected by YouTube Music’s servers)</span></div>
      <div class="hint">⚠ Google’s music API stopped accepting OAuth tokens (verified 2026-07: a valid, correctly-scoped token gets HTTP 400 from every endpoint — known upstream issue, ytmusicapi #676/#682). The flow below is kept in case support returns; use browser headers above for now.</div>
      <div class="inline" style="margin-top:8px">
        <input id="oa_id" type="text" placeholder="OAuth client ID" value="{{ytm_client_id}}">
      </div>
      <div class="inline" style="margin-top:8px">
        <input id="oa_secret" type="password" placeholder="OAuth client secret" value="{{ytm_client_secret}}">
      </div>
      <div class="inline" style="margin-top:8px">
        <button class="secondary" type="button" id="oa_btn" onclick="oauthConnect()">Connect to YouTube Music</button>
        <span id="oa_msg"></span>
      </div>
      <div id="oa_code" style="display:none;margin-top:10px;font-size:14px">
        Go to <a id="oa_link" href="" target="_blank" rel="noopener" style="color:#93b4e6"></a>
        and enter code <b id="oa_user_code" class="oacode" title="Click to select"></b>
        <button class="secondary" type="button" id="oa_copy" style="padding:3px 10px" onclick="copyOaCode()">copy</button>
        <span class="muted">— waiting for approval…</span>
        <button class="secondary" type="button" style="margin-left:4px;padding:3px 10px" onclick="oauthCancel()">Cancel</button>
      </div>
      <div class="hint"><a href="https://console.cloud.google.com" target="_blank" rel="noopener">Google Cloud Console</a> → new project → enable <b>YouTube Data API v3</b> → OAuth consent screen: publish to <b>Production</b> (Testing tokens die after 7 days) → Credentials → Create OAuth client ID → type <b>TVs and Limited Input devices</b> → copy ID + secret above. One-time, ~5 min.</div>
    </div>

    </div>
    <div class="foot">
      <button type="button" onclick="saveSettings()">Save</button>
      <button class="secondary" type="button" onclick="closeSettings()">Cancel</button>
      <span id="s_msg"></span>
    </div>
  </div>
</div>

<script>
function items(){return Array.from(document.querySelectorAll('#items .item'));}
function closeHistory(){document.getElementById('history').style.display='none';}
async function openHistory(){
  document.getElementById('history').style.display='flex';
  const box=document.getElementById('histbody');
  try{
    const log=await (await fetch('/api/added')).json();
    if(!log.length){box.innerHTML='<span class="muted">Nothing added from here yet.</span>';return;}
    const esc=s=>(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;');
    box.innerHTML='<table><tr><th>when</th><th>YTM name</th><th>in Lidarr as</th><th>result</th></tr>'+
      log.map(e=>`<tr><td class="muted">${esc(e.ts)}</td><td>${esc(e.ytm_name)}</td>`+
        `<td>${esc(e.lidarr_name)||'—'}</td>`+
        `<td class="st-${esc(e.status)}">${esc(e.status)}${e.msg?' — '+esc(e.msg):''}</td></tr>`).join('')+
      '</table>';
  }catch(e){box.innerHTML='<span class="err">Could not load history: '+e+'</span>';}
}
function openSettings(){document.getElementById('settings').style.display='flex';}
function closeSettings(){document.getElementById('settings').style.display='none';}
function togglekey(){const k=document.getElementById('s_key');k.type=k.type==='password'?'text':'password';}
async function testConn(){
  const s=document.getElementById('s_test'); s.textContent='testing…'; s.className='';
  try{
    const r=await fetch('/api/test',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url:document.getElementById('s_url').value,key:document.getElementById('s_key').value})});
    const j=await r.json();
    if(j.ok){s.className='ok';s.textContent=`✓ connected · ${j.artists} artists · ${j.rootfolders} root folder(s)`;}
    else{s.className='err';s.textContent='✗ '+j.error;}
  }catch(e){s.className='err';s.textContent='✗ '+e;}
}
async function saveSettings(){
  // one Save for the whole sheet: security first (its failure aborts), then
  // the Lidarr connection + defaults (which triggers the reload)
  const m=document.getElementById('s_msg'); m.textContent='saving…'; m.className='';
  try{
    const sec=await fetch('/api/auth/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({enabled:document.getElementById('sec_enabled').value==='1',
        username:document.getElementById('sec_user').value,
        password:document.getElementById('sec_pass').value})});
    const sj=await sec.json();
    if(!sj.ok){m.className='err';m.textContent='✗ security: '+sj.error;return;}
  }catch(e){m.className='err';m.textContent='✗ security: '+e;return;}
  const body={url:document.getElementById('s_url').value,key:document.getElementById('s_key').value,
    root:document.getElementById('s_root').value,qp:document.getElementById('s_qp').value,
    mp:document.getElementById('s_mp').value,monitor_new:document.getElementById('s_mn').value,
    tadb_key:document.getElementById('s_tadb').value};
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json();
    if(j.ok){m.className='ok';m.textContent='saved — reloading…';setTimeout(()=>location.reload(),700);}
    else{m.className='err';m.textContent='✗ '+j.error;}
  }catch(e){m.className='err';m.textContent='✗ '+e;}
}
// P5.4 - optional Forms login (saved by the sheet's unified Save button)
async function signOut(){
  try{ await fetch('/logout',{method:'POST'}); }catch(e){}
  location='/login';
}
// P5.10 - OAuth device flow: connect, show code, poll until approved
let oaPolling=false;
function oaUI(st){
  const btn=document.getElementById('oa_btn'), msg=document.getElementById('oa_msg'),
        code=document.getElementById('oa_code');
  if(st.state==='pending'){
    btn.disabled=true; msg.textContent=''; code.style.display='block';
    const a=document.getElementById('oa_link');
    a.href=st.verification_url+'?user_code='+encodeURIComponent(st.user_code);
    a.textContent=st.verification_url.replace('https://','');
    document.getElementById('oa_user_code').textContent=st.user_code;
  }else{
    btn.disabled=false; code.style.display='none';
    if(st.state==='done'){
      msg.className='ok'; msg.textContent='✓ connected — OAuth will renew itself';
      document.getElementById('ytm_state').textContent='· '+st.auth_state;
    }else if(st.state==='error'){ msg.className='err'; msg.textContent='✗ '+st.error; }
    else{ msg.textContent=''; }
  }
}
async function oaPoll(){
  let st;
  try{ st=await (await fetch('/api/ytm/oauth/status')).json(); }
  catch(e){ setTimeout(oaPoll,4000); return; }
  oaUI(st);
  if(st.state==='pending') setTimeout(oaPoll,3000);
  else{
    oaPolling=false;
    if(st.state==='done'){ const s=await (await fetch('/api/scan/status')).json(); scanUI(s); }
  }
}
function oaStartPolling(){ if(!oaPolling){oaPolling=true; oaPoll();} }
async function oauthConnect(){
  const msg=document.getElementById('oa_msg'), btn=document.getElementById('oa_btn');
  msg.className=''; msg.textContent='contacting Google…'; btn.disabled=true;
  try{
    const r=await fetch('/api/ytm/oauth/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({client_id:document.getElementById('oa_id').value,
                           client_secret:document.getElementById('oa_secret').value})});
    const j=await r.json();
    if(!j.ok){ msg.className='err'; msg.textContent='✗ '+j.error; btn.disabled=false; return; }
    oaUI({state:'pending',user_code:j.user_code,verification_url:j.verification_url});
    oaStartPolling();
  }catch(e){ msg.className='err'; msg.textContent='✗ '+e; btn.disabled=false; }
}
async function oauthCancel(){
  await fetch('/api/ytm/oauth/cancel',{method:'POST'});
  oaUI({state:'idle'});
}
async function copyOaCode(){
  const btn=document.getElementById('oa_copy');
  try{
    await navigator.clipboard.writeText(document.getElementById('oa_user_code').textContent);
    btn.textContent='copied ✓'; setTimeout(()=>btn.textContent='copy',1500);
  }catch(e){ btn.textContent='select + Ctrl-C'; setTimeout(()=>btn.textContent='copy',2500); }
}
// resume a pending device-code flow if settings is reopened / page reloaded
(async()=>{
  try{
    const st=await (await fetch('/api/ytm/oauth/status')).json();
    if(st.state==='pending'){ oaUI(st); oaStartPolling(); }
  }catch(e){}
})();
async function saveYtmAuth(){
  const m=document.getElementById('ytm_msg'); m.textContent='validating…'; m.className='';
  const btn=document.getElementById('ytm_save'); btn.disabled=true;
  try{
    const r=await fetch('/api/ytm/auth',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({raw:document.getElementById('ytm_raw').value})});
    const j=await r.json();
    if(j.ok){
      m.className='ok'; m.textContent='✓ '+j.detail;
      document.getElementById('ytm_raw').value='';
      document.getElementById('ytm_state').textContent='· '+j.state;
      const s=await (await fetch('/api/scan/status')).json(); scanUI(s);
    }else{m.className='err';m.textContent='✗ '+j.error;}
  }catch(e){m.className='err';m.textContent='✗ '+e;}
  btn.disabled=false;
}
let filter='all';
document.querySelectorAll('.flt').forEach(el=>el.onclick=()=>{
  document.querySelectorAll('.flt').forEach(f=>f.classList.remove('active'));
  el.classList.add('active'); filter=el.dataset.f; applyFilters();
});
document.querySelectorAll('.vw').forEach(el=>el.onclick=()=>{
  document.querySelectorAll('.vw').forEach(v=>v.classList.remove('active'));
  el.classList.add('active'); document.getElementById('items').className=el.dataset.v;
});
let inlibShow=false;
const selectedGenres=new Set();
const ALL_GENRES={{ genres|tojson }};
function applyFilters(){
  const q=document.getElementById('search').value.toLowerCase();
  const src=document.getElementById('fsource').value;
  const typ=document.getElementById('ftype').value;
  const gens=[...selectedGenres];
  items().forEach(it=>{
    let ok=true;
    if(filter!=='all') ok = it.dataset.tier===filter;
    if(ok && !inlibShow) ok = it.dataset.inlib!=='1';
    if(ok && q) ok = it.dataset.name.includes(q);
    if(ok && src!=='all') ok = (it.dataset.sources||'').split(',').includes(src);
    if(ok && typ!=='all') ok = it.dataset.type===typ;
    if(ok && gens.length){const g=(it.dataset.genres||'').split(','); ok = gens.some(x=>g.includes(x));}
    if(ok && document.getElementById('fnorel').checked) ok = it.dataset.norel!=='1';
    it.style.display = ok ? '' : 'none';
  });
}
function syncSearchClear(){
  document.getElementById('searchclear').style.display =
    document.getElementById('search').value ? 'inline-flex' : '';
}
function clearSearch(){
  const s=document.getElementById('search');
  s.value=''; syncSearchClear(); applyFilters(); s.focus();
}
function toggleInlib(){
  inlibShow=!inlibShow;
  document.getElementById('inlibStat').classList.toggle('on',inlibShow);
  applyFilters();
}
function renderGenreTags(){
  document.getElementById('genreTags').innerHTML=
    [...selectedGenres].map(g=>`<span class="mtag">${g}<b data-rm="${g}">×</b></span>`).join('');
}
function genreDropdown(){
  const q=document.getElementById('genreInput').value.toLowerCase();
  const opts=ALL_GENRES.filter(g=>!selectedGenres.has(g)&&g.toLowerCase().includes(q)).slice(0,40);
  const drop=document.getElementById('genreDrop');
  drop.innerHTML=opts.length?opts.map(g=>`<div data-g="${g}">${g}</div>`).join('')
    :'<div class="muted" style="cursor:default">no match</div>';
  drop.classList.add('open');
}
function addGenre(g){
  selectedGenres.add(g); document.getElementById('genreInput').value='';
  renderGenreTags(); document.getElementById('genreDrop').classList.remove('open');
  applyFilters(); document.getElementById('genreInput').focus();
}
function removeGenre(g){ selectedGenres.delete(g); renderGenreTags(); applyFilters(); }
function genreKey(e){
  if(e.key==='Enter'){ const f=document.querySelector('#genreDrop div[data-g]'); if(f){addGenre(f.dataset.g);} e.preventDefault(); }
  else if(e.key==='Backspace'&&!e.target.value&&selectedGenres.size){ removeGenre([...selectedGenres].pop()); }
}
document.getElementById('genreDrop').addEventListener('click',e=>{const d=e.target.closest('[data-g]'); if(d) addGenre(d.dataset.g);});
document.getElementById('genreTags').addEventListener('click',e=>{const b=e.target.closest('[data-rm]'); if(b){removeGenre(b.dataset.rm); e.stopPropagation();}});
document.addEventListener('click',e=>{ if(!e.target.closest('#genreMS')) document.getElementById('genreDrop').classList.remove('open'); });
function applySort(){
  const key=document.getElementById('sort').value;
  const box=document.getElementById('items');
  const order={green:0,amber:1,red:2,none:3};
  items().sort((a,b)=>{
    if(key==='name_az') return a.dataset.name.localeCompare(b.dataset.name);
    if(key==='name_za') return b.dataset.name.localeCompare(a.dataset.name);
    if(key==='tier') return (order[a.dataset.tier]-order[b.dataset.tier]) || (b.dataset.liked-a.dataset.liked);
    return (b.dataset.liked-a.dataset.liked) || a.dataset.name.localeCompare(b.dataset.name);
  }).forEach(el=>box.appendChild(el));
}
function checkVisible(v){
  items().forEach(it=>{
    if(it.style.display==='none') return;
    if(v && it.dataset.norel==='1') return;  // no-release artists opt in only
    const c=it.querySelector('.pick'); if(c) c.checked=v;
  });
  updateCount();
}
function updateCount(){
  document.getElementById('selcount').textContent =
    items().filter(it=>{const c=it.querySelector('.pick'); return c && c.checked;}).length;
}
function syncSearch(){
  // Lidarr can't search an unmonitored artist — enforce the dependency
  const m=document.getElementById('monitored'), s=document.getElementById('search_on_add');
  if(m.checked){
    s.disabled=false;
    if(s.dataset.was==='1'){s.checked=true; delete s.dataset.was;}
  }else{
    s.dataset.was = s.checked ? '1' : '0';
    s.checked=false; s.disabled=true;
  }
  document.getElementById('searchlbl').style.opacity = m.checked ? '' : '.45';
}
function updateStats(){
  // keep the header chips honest after in-place adds (no reload needed)
  const all=items();
  document.querySelector('#inlibStat b').textContent = all.filter(i=>i.dataset.inlib==='1').length;
  document.querySelector('.stat.hot b').textContent = all.filter(i=>i.dataset.addable==='1').length;
}
async function addSelected(){
  const picked=[];
  items().forEach(it=>{
    const c=it.querySelector('.pick');
    if(c && c.checked){
      const sel=it.querySelector('.mbsel');
      const mbid = sel ? sel.value : it.dataset.mbid;
      const nm = (it.querySelector('.name')||{}).textContent || it.dataset.name;
      if(mbid) picked.push({mbid, name: nm.trim(), el: it});
    }
  });
  if(!picked.length){alert('Nothing selected');return;}
  const btn=document.getElementById('addbtn'); btn.disabled=true; btn.textContent='Adding…';
  const body={items:picked.map(p=>({mbid:p.mbid,name:p.name})),
    root:document.getElementById('root').value, qp:document.getElementById('qp').value,
    mp:document.getElementById('mp').value, monitored:document.getElementById('monitored').checked,
    search:document.getElementById('search_on_add').checked,
    monitor_new:document.getElementById('monitor_new').value};
  try{
    const res=await fetch('/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await res.json();
    // P4.2 - flip added/existing rows to "in Lidarr" in place
    const done=new Set(j.results.filter(r=>r.status==='added'||r.status==='exists').map(r=>r.mbid));
    items().forEach(it=>{ if(done.has(it.dataset.mbid)){
      it.classList.add('disabled'); it.dataset.addable='0'; it.dataset.inlib='1';
      const c=it.querySelector('.pick'); if(c){c.checked=false;c.remove();}
      const mt=it.querySelector('.match'); if(mt) mt.innerHTML='<span class="muted">✓ in Lidarr</span>';
    }});
    updateStats();
    document.getElementById('results').innerHTML =
      `<b>Added ${j.summary.added}</b> · already present ${j.summary.exists} · errors ${j.summary.error}` +
      (j.summary.error ? '<br>'+j.results.filter(r=>r.status==='error').map(r=>r.mbid+': '+r.msg).join('<br>') : '');
  }catch(e){ document.getElementById('results').textContent='Error: '+e; }
  btn.disabled=false; btn.textContent='Add selected'; updateCount();
}
// P5.2 - in-app scan: start + poll /api/scan/status, survive page reloads
let scanPolling=false;
function scanBtnState(running){
  const btn=document.getElementById('scanbtn');
  btn.disabled=running;
  btn.classList.toggle('scanning',running);
  document.getElementById('scanlbl').textContent=running?'Scanning…':'Refresh from YouTube Music';
}
function scanUI(j){
  const bar=document.getElementById('scanbar');
  if(j.state==='idle'){bar.style.display='none';scanBtnState(false);return;}
  bar.style.display='flex';
  bar.classList.toggle('err',j.state==='error');
  bar.classList.toggle('ok',j.state==='done');
  document.getElementById('scanmsg').textContent =
    j.state==='error' ? '⚠ '+j.error :
    j.state==='done' ? '✓ Scan complete — reload to see updated artists' : j.message;
  document.getElementById('scanstage').textContent =
    j.state==='running' ? `stage ${j.stage_index}/${j.stages_total}` : '';
  document.getElementById('scanhint').textContent =
    (j.state==='running' && j.hint) ? j.hint : '';
  document.getElementById('scanfill').style.width=(j.state==='done'?100:(j.percent||0))+'%';
  document.getElementById('scanretry').style.display = j.state==='error' ? '' : 'none';
  document.getElementById('scanreload').style.display = j.state==='done' ? '' : 'none';
  scanBtnState(j.state==='running');
}
async function pollScan(){
  let j;
  try{ j=await (await fetch('/api/scan/status')).json(); }
  catch(e){ setTimeout(pollScan,3000); return; }
  scanUI(j);
  if(j.state==='running') setTimeout(pollScan,1500);
  else scanPolling=false;
}
function startScanPolling(){ if(!scanPolling){scanPolling=true; pollScan();} }
async function startScan(){
  const btn=document.getElementById('scanbtn'); btn.disabled=true;
  try{
    const r=await fetch('/api/scan/start',{method:'POST'});
    const j=await r.json();
    if(!j.ok && j.error_kind!=='busy'){
      scanUI({state:'error',error:j.error,error_kind:j.error_kind,percent:0});
      btn.disabled=false;
      return;
    }
  }catch(e){ btn.disabled=false; return; }
  startScanPolling();
}
// on load: resume the progress display if a scan is running, or show a
// still-unacknowledged error from a previous scan (done state is not re-shown —
// a reload already serves the fresh data)
(async()=>{
  try{
    const j=await (await fetch('/api/scan/status')).json();
    if(j.state==='running') startScanPolling();
    else if(j.state==='error') scanUI(j);
  }catch(e){}
})();
updateCount();
applyFilters();   // apply defaults (in-Lidarr hidden) on load
// Hide the card grid during active resize so the browser doesn't reflow all
// cards on every drag frame; re-show once resizing settles.
(function(){
  const box=document.getElementById('items'); let t;
  addEventListener('resize',()=>{
    box.classList.add('resizing');
    clearTimeout(t); t=setTimeout(()=>box.classList.remove('resizing'),150);
  },{passive:true});
})();
{% if not configured %}openSettings();{% endif %}
</script>
</body></html>
"""

if __name__ == "__main__":
    # Defaults suit local dev; the Docker image sets CROSSFADARR_HOST=0.0.0.0.
    host = os.environ.get("CROSSFADARR_HOST", "127.0.0.1")
    port = int(os.environ.get("CROSSFADARR_PORT", "5000"))
    try:
        from waitress import serve
        print(f"Crossfadarr listening on http://{host}:{port}")
        serve(app, host=host, port=port, threads=8)
    except ImportError:  # waitress not installed — fall back to Flask's server
        app.run(host=host, port=port, debug=False)
