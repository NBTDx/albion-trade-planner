#!/usr/bin/env python3
"""Local receiver for the Albion Data Client.

Run the data client with its public uploader pointing here instead of the
Albion Online Data Project:

    sudo ./albiondata-client -i http://127.0.0.1:8766

Every market page you open in-game is then POSTed only to this server and
stored in market.db next to this file. refine.py reads the latest snapshot
from there.

    python3 collector.py            # run the server
    python3 collector.py status     # what has been captured recently
"""
import json
import os
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8766
DB_PATH = Path(os.environ.get("ALBION_DB") or Path(__file__).resolve().parent / "market.db")

# Market location IDs from the game's world data (ao-bin-dumps formatted/world.txt).
# Cities also get their zone and portal IDs in case the client reports those.
CITIES = {
    "Thetford": ["0007", "0000", "0301"],
    "Lymhurst": ["1002", "1000", "1301"],
    "Bridgewatch": ["2004", "2000", "2301"],
    "Martlock": ["3008", "3004", "3301"],
    "Fort Sterling": ["4002", "4000", "4301"],
    "Caerleon": ["3005", "3003"],
    "Black Market": ["3013-Auction2"],
    "Brecilien": ["5003", "5000", "5001"],
}
CITY_BY_ID = {loc: city for city, ids in CITIES.items() for loc in ids}

# The game sends prices in 1/10000 silver.
PRICE_SCALE = 10_000


def connect():
    db = sqlite3.connect(DB_PATH)
    db.execute("""CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY,
        item TEXT NOT NULL,
        location_id TEXT NOT NULL,
        city TEXT,
        quality INTEGER,
        enchant INTEGER,
        price REAL NOT NULL,
        amount INTEGER NOT NULL,
        side TEXT NOT NULL,          -- 'offer' = sell order, 'request' = buy order
        expires TEXT,
        seen_at REAL NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS orders_lookup ON orders (item, city, side, seen_at)")
    # Every time an order is seen, not just the last: how prices move between your looks (drift.py).
    db.execute("""CREATE TABLE IF NOT EXISTS sightings (
        order_id INTEGER NOT NULL,
        item TEXT NOT NULL,
        city TEXT,
        side TEXT NOT NULL,
        price REAL NOT NULL,
        amount INTEGER NOT NULL,
        seen_at REAL NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS sightings_lookup ON sightings (item, city, side, seen_at)")
    return db


def normalize_item(item, enchant):
    # API ids carry the enchantment as '@N' (T6_HIDE_LEVEL1@1); make sure ours do too.
    if enchant and "@" not in item:
        return f"{item}@{enchant}"
    return item


def store_orders(orders):
    now = time.time()
    rows = []
    for o in orders:
        loc = str(o.get("LocationId") or "")
        enchant = int(o.get("EnchantmentLevel") or 0)
        rows.append((int(o["Id"]), normalize_item(o["ItemTypeId"], enchant), loc, CITY_BY_ID.get(loc),
                     int(o.get("QualityLevel") or 0), enchant, o["UnitPriceSilver"] / PRICE_SCALE,
                     int(o.get("Amount") or 0), str(o.get("AuctionType", "")).lower(), o.get("Expires"), now))
    with connect() as db:
        db.executemany("INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.executemany("INSERT INTO sightings VALUES (?,?,?,?,?,?,?)",
                       [(r[0], r[1], r[3], r[8], r[6], r[7], r[10]) for r in rows])
    return rows


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        topic = self.path.strip("/")
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.end_headers()
        if topic != "marketorders.ingest":
            return  # history, gold etc. aren't used yet
        try:
            rows = store_orders(json.loads(body).get("Orders") or [])
        except (ValueError, KeyError, TypeError) as e:
            print(f"{time.strftime('%H:%M:%S')}  could not read upload: {e}", flush=True)
            return
        if not rows:
            return
        locs = {r[3] or f"unknown location {r[2]}" for r in rows}
        sides = {"offer": "sell", "request": "buy"}
        side = "/".join(sorted({sides.get(r[8], r[8]) for r in rows}))
        items = sorted({r[1] for r in rows})
        shown = ", ".join(items[:4]) + (f" +{len(items) - 4} more" if len(items) > 4 else "")
        print(f"{time.strftime('%H:%M:%S')}  {', '.join(sorted(locs))}: {len(rows)} {side} orders  ({shown})",
              flush=True)

    def log_message(self, *args):
        pass  # one line per upload is printed above instead


def latest_book(max_age_minutes=180):
    """Best prices from the most recent look at each item in each city.
    Returns {item: {city: {'sell': (price, age_h, units), 'buy': (price, age_h, units)}}}."""
    if not DB_PATH.exists():
        return {}
    now = time.time()
    book = {}
    with connect() as db:
        # Orders from the last time each (item, city, side) was seen; older rows may be filled or cancelled.
        snapshot = db.execute("""
            SELECT o.item, o.city, o.side, o.price, o.amount, o.seen_at
            FROM orders o JOIN (
                SELECT item, city, side, MAX(seen_at) AS last FROM orders
                WHERE city IS NOT NULL AND seen_at > ? GROUP BY item, city, side) l
            ON o.item = l.item AND o.city = l.city AND o.side = l.side AND o.seen_at > l.last - 120
            """, (now - max_age_minutes * 60,)).fetchall()
    for item, city, side, price, amount, seen in snapshot:
        key = {"offer": "sell", "request": "buy"}.get(side)
        if key is None:
            continue
        entry = book.setdefault(item, {}).setdefault(city, {"sell": None, "buy": None})
        best = entry[key]
        better = best is None or (price < best[0] if key == "sell" else price > best[0])
        age = (now - seen) / 3600
        if better:
            entry[key] = (price, age, amount)
        elif price == best[0]:
            entry[key] = (price, best[1], best[2] + amount)
    return book


def status():
    book = latest_book()
    if not book:
        print(f"Nothing captured in the last 3 hours ({DB_PATH}).")
        return
    for item in sorted(book):
        for city, e in sorted(book[item].items()):
            cells = [f"{side} {p[0]:>10,.0f} x{p[2]:<5} ({p[1] * 60:.0f}m ago)" if p else f"{side} {'-':>10}"
                     for side, p in (("sell", e["sell"]), ("buy", e["buy"]))]
            print(f"{item:<24} {city:<14} {'   '.join(cells)}")


def main():
    if sys.argv[1:] == ["status"]:
        status()
        return
    db = connect()
    with db:
        db.execute("DELETE FROM sightings WHERE seen_at < ?", (time.time() - 30 * 86400,))  # drift.py uses 14 days
    db.close()
    print(f"Collector listening on http://127.0.0.1:{PORT}, saving to {DB_PATH}", flush=True)
    print("Open market pages in-game (Buy tab = sell orders, Sell tab = buy orders).", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
