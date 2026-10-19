#!/usr/bin/env python3
"""A small page in your browser for loop.py: pick your silver, weight and focus, see the
best routes, and click one for the stop-by-stop plan.

    python3 ui.py            # then open http://127.0.0.1:8767 (it opens by itself)
    python3 ui.py --no-open

Like the collector it only listens on 127.0.0.1, so nothing outside your computer can
reach it. It uses the same planner and the same data as loop.py.
"""
import argparse
import json
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import loop
from collector import DB_PATH, PORT as COLLECTOR_PORT
from trip import load_books

PORT = 8767
TOP = 40
_planning = threading.Lock()

# Form field -> (loop.py option, type). Anything else in a request is ignored.
FIELDS = {
    "silver": ("silver", float), "kg": ("kg", float), "focus": ("focus", float),
    "hop_min": ("hop_min", float), "caerleon_min": ("caerleon_min", float),
    "brecilien_min": ("brecilien_min", float), "stop_min": ("stop_min", float),
    "max_hops": ("max_hops", int), "max_age": ("max_age", float), "confidence": ("confidence", float),
    "station_fee": ("station_fee", float), "days": ("days", float), "share": ("share", float),
}


def collector_running():
    try:
        with socket.create_connection(("127.0.0.1", COLLECTOR_PORT), timeout=0.3):
            return True
    except OSError:
        return False


def status():
    seen = {}
    books = load_books(10 ** 7, seen) if DB_PATH.exists() else {}
    now = time.time()
    cities = {}
    for (item, city, _), t in seen.items():
        c = cities.setdefault(city, dict(city=city, lists=0, items=set(), newest=0))
        c["lists"] += 1
        c["items"].add(item)
        c["newest"] = max(c["newest"], t)
    out = [dict(city=c["city"], lists=c["lists"], items=len(c["items"]), minutes_ago=(now - c["newest"]) / 60)
           for c in sorted(cities.values(), key=lambda c: -c["newest"])]
    return dict(here=loop.current_city(), collector=collector_running(), cities=out, lists=len(books),
                cities_known=loop.CITIES)


def make_args(form):
    args = loop.parser().parse_args([])
    for key, (attr, kind) in FIELDS.items():
        value = form.get(key)
        if value not in (None, ""):
            setattr(args, attr, kind(value))
    args.no_premium = not form.get("premium", True)
    args.orders = bool(form.get("orders"))
    args.origin = loop.city_name(form["origin"]) if form.get("origin") else None
    args.avoid = [loop.city_name(c) for c in form.get("avoid") or []]
    return args


def route_json(r):
    return dict(name=loop.route_name(r["stops"]), stops=r["stops"], hops=len(r["stops"]) - 1,
                minutes=r["minutes"], profit=r["profit"], captured=r["captured"], per_hour=r["per_hour"],
                focus=r["focus"], later=r["later"], red=r["red"], from_you=r["from_you"], legs=r["legs"],
                cash=r["cash"], load=r["load"], rows=loop.stop_rows(r))


def plan(form):
    args = make_args(form)
    res = loop.plan_routes(args, log=lambda *_: None)
    routes = res["routes"][:TOP]
    if res["here"]:  # keep the best route from where you are, even if it's further down
        mine = next((r for r in res["routes"] if r["stops"][0] == res["here"]), None)
        if mine and mine not in routes:
            routes.append(mine)
    return dict(here=res["here"], captured=res["captured"], margin=loop.margin_text(res, args),
                total=len(res["routes"]), routes=[route_json(r) for r in routes])


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, kind="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def local(self):
        # Only answer pages opened as 127.0.0.1/localhost, so other websites can't talk to it.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host in ("127.0.0.1", "localhost"):
            return True
        self.send(403, {"error": "open this page at http://127.0.0.1:%d" % PORT})
        return False

    def do_GET(self):
        if not self.local():
            return
        path = urlsplit(self.path).path
        if path == "/":
            self.send(200, PAGE, "text/html")
        elif path == "/api/status":
            self.send(200, status())
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.local():
            return
        if urlsplit(self.path).path != "/api/plan":
            return self.send(404, {"error": "not found"})
        try:
            form = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad request"})
        if not _planning.acquire(blocking=False):
            return self.send(409, {"error": "already planning, give it a moment"})
        try:
            self.send(200, plan(form))
        except (ValueError, argparse.ArgumentTypeError) as e:
            self.send(400, {"error": str(e)})
        finally:
            _planning.release()

    def log_message(self, *args):
        pass


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Albion Trade Planner</title>
<style>
:root {
  --bg: #f6f4ef; --surface: #ffffff; --sunken: #efece5; --border: #ddd8cc; --text: #22201b;
  --muted: #6d675b; --accent: #a8721c; --accent-text: #ffffff; --good: #2f7d4a; --bad: #b3412e;
  --red-zone: #c0392b; --shadow: 0 1px 2px rgba(0,0,0,.06);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #15140f; --surface: #1e1c16; --sunken: #26231c; --border: #37332a; --text: #ece7db;
    --muted: #a39b89; --accent: #d9a441; --accent-text: #1a160d; --good: #6cc48a; --bad: #e57a66;
    --red-zone: #ef6b5b; --shadow: none;
  }
}
:root[data-theme="dark"] {
  --bg: #15140f; --surface: #1e1c16; --sunken: #26231c; --border: #37332a; --text: #ece7db;
  --muted: #a39b89; --accent: #d9a441; --accent-text: #1a160d; --good: #6cc48a; --bad: #e57a66;
  --red-zone: #ef6b5b; --shadow: none;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
header { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 18px; padding: 14px 20px;
  border-bottom: 1px solid var(--border); background: var(--surface); }
h1 { font-size: 17px; margin: 0; letter-spacing: .2px; }
h1 span { color: var(--accent); }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { font-size: 12px; padding: 3px 9px; border-radius: 999px; background: var(--sunken);
  border: 1px solid var(--border); color: var(--muted); white-space: nowrap; }
.chip b { color: var(--text); font-weight: 600; }
.chip.on { color: var(--good); } .chip.off { color: var(--bad); }
main { display: grid; grid-template-columns: 300px minmax(0, 1fr); gap: 18px; padding: 18px 20px;
  max-width: 1400px; margin: 0 auto; }
@media (max-width: 900px) { main { grid-template-columns: minmax(0, 1fr); padding: 14px 16px; } }
.panel { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  box-shadow: var(--shadow); }
form.panel { padding: 14px; align-self: start; position: sticky; top: 14px; }
@media (max-width: 900px) { form.panel { position: static; } }
fieldset { border: 0; padding: 0; margin: 0 0 12px; }
legend { font-size: 11px; text-transform: uppercase; letter-spacing: .8px; color: var(--muted);
  margin-bottom: 6px; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 8px; }
input, select { width: 100%; margin-top: 3px; padding: 6px 8px; font: inherit; color: var(--text);
  background: var(--bg); border: 1px solid var(--border); border-radius: 6px; }
input:focus, select:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
.check { display: flex; align-items: center; gap: 7px; color: var(--text); font-size: 13px; }
.check input { width: auto; margin: 0; }
details { margin: 4px 0 12px; } summary { cursor: pointer; color: var(--muted); font-size: 12px; margin-bottom: 8px; }
button { width: 100%; padding: 9px; font: inherit; font-weight: 600; border: 0; border-radius: 7px;
  background: var(--accent); color: var(--accent-text); cursor: pointer; }
button:disabled { opacity: .6; cursor: progress; }
button.more { width: auto; margin: 10px 12px; padding: 6px 12px; background: var(--sunken); color: var(--text);
  border: 1px solid var(--border); font-weight: 500; }
.note { font-size: 12px; color: var(--muted); padding: 10px 14px; border-bottom: 1px solid var(--border); }
.empty { padding: 40px 20px; text-align: center; color: var(--muted); }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .6px; color: var(--muted);
  font-weight: 600; padding: 8px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }
td { padding: 8px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.routes tbody tr { cursor: pointer; }
.routes tbody tr:hover { background: var(--sunken); }
.routes tbody tr.sel { background: var(--sunken); box-shadow: inset 3px 0 0 var(--accent); }
.red { color: var(--red-zone); font-size: 11px; font-weight: 600; margin-left: 6px; }
.mine { color: var(--accent); font-size: 11px; font-weight: 600; margin-left: 6px; }
.detail { margin-top: 18px; }
.detail h2 { font-size: 16px; margin: 0; padding: 14px 14px 4px; }
.summary { display: flex; flex-wrap: wrap; gap: 8px 22px; padding: 6px 14px 14px; color: var(--muted);
  border-bottom: 1px solid var(--border); }
.summary b { color: var(--text); font-size: 15px; }
.stop { padding: 12px 14px; border-bottom: 1px solid var(--border); }
.stop:last-child { border-bottom: 0; }
.stop h3 { margin: 0 0 6px; font-size: 14px; display: flex; gap: 8px; align-items: baseline; }
.stop h3 .n { display: inline-grid; place-items: center; width: 22px; height: 22px; border-radius: 50%;
  background: var(--sunken); border: 1px solid var(--border); font-size: 12px; }
.stop table { table-layout: fixed; min-width: 640px; }
.stop table td { padding: 5px 10px; border-bottom: 0; overflow: hidden; text-overflow: ellipsis; }
.stop col.c-verb { width: 76px; } .stop col.c-item { width: 170px; } .stop col.c-units { width: 120px; }
.stop col.c-silver { width: 130px; }
.verb { font-size: 11px; font-weight: 700; letter-spacing: .5px; text-transform: uppercase; width: 64px; }
.verb.sell, .verb.list { color: var(--good); } .verb.buy { color: var(--bad); } .verb.refine { color: var(--accent); }
.leg { font-size: 12px; color: var(--muted); margin-top: 8px; padding-left: 30px; }
.passing { color: var(--muted); font-size: 12px; padding-left: 30px; }
.tier { display: inline-block; min-width: 36px; padding: 1px 5px; margin-right: 6px; border-radius: 4px;
  font-size: 11px; font-weight: 700; text-align: center; color: #fff; }
.t4 { background: #3d78c2; } .t5 { background: #c0442f; } .t6 { background: #d27b1f; }
.t7 { background: #c9a227; color: #1d1a12; } .t8 { background: #e6e3da; color: #1d1a12; }
.muted { color: var(--muted); }
.spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--border);
  border-top-color: var(--accent); border-radius: 50%; animation: spin .8s linear infinite; vertical-align: -2px; margin-right: 6px; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<header>
  <h1>Albion <span>Trade Planner</span></h1>
  <div class="chips" id="chips"><span class="chip">Loading&hellip;</span></div>
</header>
<main>
  <form class="panel" id="form">
    <fieldset>
      <legend>You</legend>
      <div class="row">
        <label>Silver<input name="silver" inputmode="numeric" value="5000000"></label>
        <label>Carry (kg)<input name="kg" inputmode="numeric" value="5000"></label>
      </div>
      <div class="row">
        <label>Focus<input name="focus" inputmode="numeric" value="0"></label>
        <label>Start in<select name="origin" id="origin"><option value="">Anywhere</option></select></label>
      </div>
      <label class="check"><input type="checkbox" name="premium" checked> Premium (4% tax)</label>
    </fieldset>
    <fieldset>
      <legend>Travel</legend>
      <div class="row">
        <label>Min per hop<input name="hop_min" inputmode="decimal" value="10"></label>
        <label>Max hops<input name="max_hops" inputmode="numeric" value="5"></label>
      </div>
      <label class="check"><input type="checkbox" name="avoid_caerleon"> Avoid Caerleon (red zones)</label>
      <label class="check"><input type="checkbox" name="avoid_brecilien"> Avoid Brecilien</label>
    </fieldset>
    <fieldset>
      <legend>Safety</legend>
      <label>Plan for prices to hold<select name="confidence">
        <option value="0.5">Off (use captured prices)</option><option value="0.7">70% of the time</option>
        <option value="0.8" selected>80% of the time</option><option value="0.9">90% of the time</option>
        <option value="0.95">95% of the time</option></select></label>
      <label>Use captures from the last (min)<input name="max_age" inputmode="numeric" value="60"></label>
    </fieldset>
    <details>
      <summary>More settings</summary>
      <div class="row">
        <label>Caerleon min<input name="caerleon_min" inputmode="decimal" placeholder="= hop"></label>
        <label>Brecilien min<input name="brecilien_min" inputmode="decimal" placeholder="= hop"></label>
      </div>
      <div class="row">
        <label>Trading min/stop<input name="stop_min" inputmode="decimal" value="3"></label>
        <label>Station fee<input name="station_fee" inputmode="numeric" value="500"></label>
      </div>
      <label class="check"><input type="checkbox" name="orders"> Also plan listing sell orders</label>
      <div class="row">
        <label>Days to fill<input name="days" inputmode="decimal" value="1"></label>
        <label>Max share of sales<input name="share" inputmode="decimal" value="0.2"></label>
      </div>
    </details>
    <button id="go" type="submit">Find routes</button>
  </form>
  <section>
    <div class="panel" id="results"><div class="empty">Set your silver, weight and focus, then <b>Find routes</b>.</div></div>
    <div class="panel detail" id="detail" hidden></div>
  </section>
</main>
<script>
const $ = (s) => document.querySelector(s);
const form = $("#form");
const KEY = "albion-planner-settings";
const fmt = (n) => Math.round(n).toLocaleString("en-US");
const short = (n) => Math.abs(n) >= 1e6 ? (n / 1e6).toFixed(2) + "m" : Math.abs(n) >= 1e3 ? Math.round(n / 1e3) + "k" : fmt(n);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const query = new URLSearchParams(location.search);  // e.g. ?max_age=120&focus=30000&run=1
if (["light", "dark"].includes(query.get("theme"))) document.documentElement.dataset.theme = query.get("theme");
const SHOWN = 12;
let routes = [], here = null, showAll = false, lastData = null, selected = 0;

function load() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
  for (const [k, v] of Object.entries(saved)) {
    const el = form.elements[k];
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
  }
  for (const [k, v] of query) {
    const el = form.elements[k];
    if (!el) continue;
    if (el.type === "checkbox") el.checked = v === "1" || v === "true"; else el.value = v;
  }
}
function settings() {
  const out = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    out[el.name] = el.type === "checkbox" ? el.checked : el.value.replace(/[,\s]/g, "");
  }
  try { localStorage.setItem(KEY, JSON.stringify(out)); } catch (e) {}
  out.avoid = [out.avoid_caerleon && "Caerleon", out.avoid_brecilien && "Brecilien"].filter(Boolean);
  return out;
}
function tierBadge(label) {
  const [t, ...rest] = label.split(" ");
  const tier = t.slice(1, 2);
  return `<span class="tier t${tier}">${esc(t)}</span>${esc(rest.join(" "))}`;
}
function ago(min) {
  return min < 1 ? "just now" : min < 60 ? Math.round(min) + " min ago" : min < 2880 ? Math.round(min / 60) + " h ago" : Math.round(min / 1440) + " days ago";
}

async function refreshStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    here = s.here;
    const chips = [`<span class="chip ${s.collector ? "on" : "off"}">Collector ${s.collector ? "running" : "not running"}</span>`];
    if (s.here) chips.push(`<span class="chip">You're in <b>${esc(s.here)}</b></span>`);
    for (const c of s.cities) chips.push(`<span class="chip"><b>${esc(c.city)}</b> ${c.items} items &middot; ${ago(c.minutes_ago)}</span>`);
    if (!s.cities.length) chips.push(`<span class="chip">Nothing captured yet: open some markets in-game</span>`);
    $("#chips").innerHTML = chips.join("");
    const origin = $("#origin"), keep = origin.value;
    if (origin.options.length === 1) {
      for (const c of s.cities_known) origin.add(new Option(c, c));
      load();
      if (query.has("run")) form.requestSubmit();
    } else origin.value = keep;
  } catch (e) {
    $("#chips").innerHTML = `<span class="chip off">Can't reach the planner; is ui.py still running?</span>`;
  }
}

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const go = $("#go");
  go.disabled = true;
  go.innerHTML = `<span class="spinner"></span>Checking routes&hellip;`;
  $("#detail").hidden = true;
  try {
    const res = await fetch("/api/plan", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(settings())});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "something went wrong");
    renderRoutes(data);
  } catch (e) {
    $("#results").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  } finally {
    go.disabled = false;
    go.textContent = "Find routes";
  }
});

function renderRoutes(data, keep) {
  lastData = data;
  if (!keep) showAll = false;
  routes = data.routes;
  if (!routes.length) {
    $("#results").innerHTML = `<div class="note">${esc(data.margin)}</div><div class="empty">No profitable route in what you've captured.<br>
      Open more markets in-game, or raise <b>Use captures from the last</b>.</div>`;
    return;
  }
  const rows = routes.slice(0, showAll ? routes.length : SHOWN).map((r, i) => `<tr data-i="${i}">
      <td>${esc(r.name)}${r.red ? '<span class="red">RED ZONE</span>' : ""}${data.here && r.stops[0] === data.here ? '<span class="mine">FROM YOU</span>' : ""}</td>
      <td class="num">${r.hops}</td><td class="num">${Math.round(r.minutes)}</td>
      <td class="num">${short(r.profit)}</td><td class="num"><b>${short(r.per_hour)}</b></td>
      ${data.here ? `<td class="num muted">${r.from_you == null ? "" : Math.round(r.from_you) + " min"}</td>` : ""}</tr>`).join("");
  $("#results").innerHTML = `<div class="note">${esc(data.margin)} Showing ${routes.length} of ${fmt(data.total)} profitable routes.</div>
    <div class="table-wrap"><table class="routes"><thead><tr><th>Route</th><th class="num">Hops</th><th class="num">Min</th>
    <th class="num">Profit</th><th class="num">Silver/hr</th>${data.here ? '<th class="num">From you</th>' : ""}</tr></thead><tbody>${rows}</tbody></table></div>
    ${routes.length > SHOWN && !showAll ? `<button class="more" id="more">Show all ${routes.length} routes</button>` : ""}`;
  for (const tr of document.querySelectorAll(".routes tbody tr")) tr.addEventListener("click", () => select(+tr.dataset.i));
  const more = $("#more");
  if (more) more.addEventListener("click", () => { showAll = true; renderRoutes(lastData, true); });
  if (keep) highlight(); else select(0);
}

function highlight() {
  for (const tr of document.querySelectorAll(".routes tbody tr")) tr.classList.toggle("sel", +tr.dataset.i === selected);
}

function select(i) {
  const r = routes[i];
  selected = i;
  highlight();
  const stops = r.stops.map((city, s) => {
    const rows = r.rows[s].map((row) => row.verb === "refine"
      ? `<tr><td class="verb refine">refine</td><td>${tierBadge(row.label)}</td><td class="num">${fmt(row.units)} crafts</td>
         <td class="num muted">fees ${fmt(row.silver)}</td><td class="muted">${esc(row.note)}</td></tr>`
      : `<tr><td class="verb ${row.verb}">${row.verb}</td><td>${tierBadge(row.label)}</td><td class="num">${fmt(row.units)}</td>
         <td class="num">${fmt(row.silver)}</td><td class="muted">~${fmt(row.silver / row.units)} each ${esc(row.note.replace("->", "→"))}</td></tr>`).join("");
    const next = s < r.stops.length - 1 ? r.stops[s + 1] : null;
    const red = next && (city === "Caerleon" || next === "Caerleon") ? ' <span class="red">RED ZONE</span>' : "";
    const leg = next ? `<div class="leg">&darr; ${Math.round(r.legs[s])} min to ${esc(next)} &middot; carrying ${fmt(r.load[s])} kg &middot; silver left at least ${fmt(r.cash[s])}${red}</div>` : "";
    const cols = '<colgroup><col class="c-verb"><col class="c-item"><col class="c-units"><col class="c-silver"><col></colgroup>';
    const body = rows ? `<div class="table-wrap"><table>${cols}${rows}</table></div>` : `<div class="passing">Just passing through</div>`;
    return `<div class="stop"><h3><span class="n">${s + 1}</span>${esc(city)}</h3>${body}${leg}</div>`;
  }).join("");
  const later = r.later >= 1 ? `<span>plus <b>${fmt(r.later)}</b> as sell orders fill</span>` : "";
  $("#detail").innerHTML = `<h2>${esc(r.stops.join(" → "))}${r.red ? '<span class="red">RED ZONE</span>' : ""}</h2>
    <div class="summary"><span>Profit <b>${fmt(r.profit)}</b></span><span><b>${fmt(r.per_hour)}</b>/hr</span>
    <span>about <b>${Math.round(r.minutes)}</b> min</span>${r.focus ? `<span>focus <b>${fmt(r.focus)}</b></span>` : ""}
    <span class="muted">${fmt(r.captured)} if captured prices hold</span>${later}</div>${stops}
    <div class="note">Prices shown are what you captured. Reopen each market before you trade.</div>`;
  $("#detail").hidden = false;
}

load();
refreshStatus();
setInterval(refreshStatus, 20000);
</script>
</body>
</html>
"""


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--no-open", action="store_true", help="don't open the browser")
    args = p.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Trade planner on {url} (Ctrl+C to stop)", flush=True)
    if not args.no_open:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
