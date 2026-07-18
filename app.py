#!/usr/bin/env python3
"""Phase 4 - Review UI: browse matched artists, pick, push selected to Lidarr.

    python app.py            # serves http://127.0.0.1:5000

Reads data/matches.json + data/artwork.json + data/ytm_thumbs.json + config.yaml.
Nothing is sent to Lidarr until you tick artists and click "Add selected".
Card grid (with artwork) or compact list; selection stays in sync across views.
"""
from __future__ import annotations

import json
import urllib.parse

import yaml
from flask import Flask, render_template_string, request, jsonify

from lidarr import Lidarr

CONFIG_PATH = "config.yaml"

app = Flask(__name__)
LID = Lidarr()

TIER_ORDER = {"green": 0, "amber": 1, "red": 2, "none": 3}


def _alt_label(x: dict) -> str:
    extra = x.get("disambiguation") or x.get("type") or ""
    return f"{x.get('name')}" + (f" — {extra}" if extra else "")


def _load_json(path: str) -> dict:
    try:
        return json.load(open(path, encoding="utf-8"))
    except FileNotFoundError:
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


def load_rows(existing: set) -> list[dict]:
    try:
        matches = json.load(open(LID.matches_file, encoding="utf-8"))
    except FileNotFoundError:
        return []  # pipeline not run yet
    tad = _load_json("data/artwork.json")
    ytm = _load_json("data/ytm_thumbs.json")
    gen = _load_json("data/genres.json")
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
    results = [
        LID.add_artist(it["mbid"], root, qp, mp, monitored, search, monitor_new)
        for it in data.get("items", [])
    ]
    summary = {
        "added": sum(r["status"] == "added" for r in results),
        "exists": sum(r["status"] == "exists" for r in results),
        "error": sum(r["status"] == "error" for r in results),
    }
    return jsonify({"summary": summary, "results": results})


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
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    LID = Lidarr()  # reload with new settings
    return jsonify({"ok": True})


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
  .sheet{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px 22px;width:min(480px,92vw);box-shadow:0 20px 60px -20px #000}
  .sheet h3{margin:0 0 14px;font-size:16px}
  .sheet label{display:block;font-size:12px;color:var(--mut);margin:10px 0 4px}
  .sheet input,.sheet select{width:100%}
  .sheet .inline{display:flex;gap:8px;align-items:center}
  .sheet hr{border:0;border-top:1px solid var(--line);margin:16px 0}
  .sheet .foot{display:flex;gap:10px;align-items:center;margin-top:16px}
  .ok{color:#86efac}.err{color:#fca5a5}
  .setupbar{background:#78350f;color:#fde68a;padding:10px 18px;font-size:14px;display:flex;align-items:center;gap:10px}
  .setupbar button{padding:5px 12px}

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
  .searchrow #search{flex:1 1 180px;width:auto;min-width:130px}
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
    <span class="gear" onclick="openSettings()" title="Settings">⚙</span>
  </div>
  <div class="panels">
    <section class="panel grow2">
      <div class="phead"><span>Search &amp; filters</span></div>
      <div class="frow searchrow">
        <input type="text" id="search" placeholder="search artists…" oninput="applyFilters()">
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
        <label class="ctl">Metadata <select id="mp">{% for m in mps %}<option value="{{m.id}}" {{'selected' if m.id==defaults.get('metadata_profile_id') else ''}}>{{m.name}}</option>{% endfor %}</select></label>
      </div>
      <div class="frow">
        <label class="ctl">New albums <select id="monitor_new"><option value="all" selected>All</option><option value="new">New</option><option value="none">None</option></select></label>
        <label class="ctl"><input type="checkbox" id="monitored" checked> monitor</label>
        <label class="ctl"><input type="checkbox" id="search_on_add" checked> search</label>
      </div>
      <div class="frow">
        <button class="secondary" onclick="checkVisible(true)">select visible</button>
        <button class="secondary" onclick="checkVisible(false)">clear</button>
        <button id="addbtn" onclick="addSelected()">Add selected (<span id="selcount">0</span>)</button>
      </div>
    </section>
  </div>
</header>
{% if not configured %}
<div class="setupbar">⚠ Lidarr isn't configured yet. <button onclick="openSettings()">Open settings</button> to connect your Lidarr endpoint &amp; API key.</div>
{% elif not lidarr_ok %}
<div class="setupbar">⚠ Can't reach Lidarr at <code>{{lidarr_url}}</code> (check it's running / the API key). <button onclick="openSettings()">Settings</button></div>
{% endif %}
{% if total == 0 %}
<div class="setupbar" style="background:#1f2937;color:#cbd5e1">No matched artists yet — run <code>ingest.py</code> then <code>matcher.py</code> to build <code>data/matches.json</code>.</div>
{% endif %}
<div id="results"></div>
<div id="items" class="cards">
{% for r in rows %}
  <div class="item {{'disabled' if not r.addable else ''}}" data-tier="{{r.tier}}" data-name="{{r.ytm_name|lower}}" data-addable="{{'1' if r.addable else '0'}}" data-mbid="{{r.mbid or ''}}" data-liked="{{r.liked}}" data-sources="{{r.sources|join(',')}}" data-type="{{r.mb_type}}" data-genres="{{r.genres|join(',')}}" data-inlib="{{'1' if r.in_lib else '0'}}">
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
      <div class="sub">{% for s in r.sources %}<span class="tag">{{s}}</span>{% endfor %}{% if r.liked %}<span class="liked">♥{{r.liked}}</span>{% endif %}{% if r.genre %}<span class="gchip">{{r.genre}}</span>{% endif %}</div>
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

<div id="settings" class="modal" style="display:none">
  <div class="sheet">
    <h3>⚙ Settings</h3>
    <label>Lidarr URL</label>
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
    <label>Default monitor new albums</label>
    <select id="s_mn">
      <option value="all" {{'selected' if defaults.get('monitor_new','all')=='all' else ''}}>All</option>
      <option value="new" {{'selected' if defaults.get('monitor_new')=='new' else ''}}>New</option>
      <option value="none" {{'selected' if defaults.get('monitor_new')=='none' else ''}}>None</option>
    </select>
    <div class="foot">
      <button type="button" onclick="saveSettings()">Save</button>
      <button class="secondary" type="button" onclick="closeSettings()">Cancel</button>
      <span id="s_msg"></span>
    </div>
  </div>
</div>

<script>
function items(){return Array.from(document.querySelectorAll('#items .item'));}
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
  const m=document.getElementById('s_msg'); m.textContent='saving…'; m.className='';
  const body={url:document.getElementById('s_url').value,key:document.getElementById('s_key').value,
    root:document.getElementById('s_root').value,qp:document.getElementById('s_qp').value,
    mp:document.getElementById('s_mp').value,monitor_new:document.getElementById('s_mn').value};
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json();
    if(j.ok){m.className='ok';m.textContent='saved — reloading…';setTimeout(()=>location.reload(),700);}
    else{m.className='err';m.textContent='✗ '+j.error;}
  }catch(e){m.className='err';m.textContent='✗ '+e;}
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
    it.style.display = ok ? '' : 'none';
  });
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
  items().forEach(it=>{ if(it.style.display!=='none'){const c=it.querySelector('.pick'); if(c) c.checked=v;} });
  updateCount();
}
function updateCount(){
  document.getElementById('selcount').textContent =
    items().filter(it=>{const c=it.querySelector('.pick'); return c && c.checked;}).length;
}
async function addSelected(){
  const picked=[];
  items().forEach(it=>{
    const c=it.querySelector('.pick');
    if(c && c.checked){
      const sel=it.querySelector('.mbsel');
      const mbid = sel ? sel.value : it.dataset.mbid;
      if(mbid) picked.push({mbid, name: it.dataset.name, el: it});
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
      it.classList.add('disabled'); it.dataset.addable='0';
      const c=it.querySelector('.pick'); if(c){c.checked=false;c.remove();}
      const mt=it.querySelector('.match'); if(mt) mt.innerHTML='<span class="muted">✓ in Lidarr</span>';
    }});
    document.getElementById('results').innerHTML =
      `<b>Added ${j.summary.added}</b> · already present ${j.summary.exists} · errors ${j.summary.error}` +
      (j.summary.error ? '<br>'+j.results.filter(r=>r.status==='error').map(r=>r.mbid+': '+r.msg).join('<br>') : '');
  }catch(e){ document.getElementById('results').textContent='Error: '+e; }
  btn.disabled=false; btn.textContent='Add selected'; updateCount();
}
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
    app.run(host="127.0.0.1", port=5000, debug=False)
