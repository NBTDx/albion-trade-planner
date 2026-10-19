"""How fast captured prices go stale, for the planners' safety margin.

The price you captured at a later stop is older by the time you get there. For every
order list (item, city, side) this looks at how its price moved over past intervals,
scaled to one hour (a move over t hours counts as move / sqrt(t), as for a random walk),
and takes the move against you that was only beaten 1 - confidence of the time. A buy
t hours old is then planned at price * exp(move * sqrt(t)), a sale at
price / exp(move * sqrt(t)). With confidence 0.8: in 4 out of 5 past intervals the
price moved less against you than the plan assumes.

The past moves come from, in order:
  1. your own repeated looks at the same market (the sightings table in market.db),
     so this gets better the more you capture,
  2. the Albion Online Data Project's hourly average traded prices (only downloaded),
  3. the typical (median) move of the order lists above, for lists with neither.
"""
import math
import subprocess
import time
from datetime import datetime
from statistics import median

from collector import connect
from refine import api

LOOK_GAP = 120          # seconds: uploads this close together are one look at a market
MIN_GAP_H = 5 / 60      # pairs of looks closer than this say little about drift
MAX_GAP_H = 12
MIN_MOVES = 6           # fewer past moves than this and a list gets the typical move instead
HISTORY_MIN_TRADES = 5  # hours with fewer trades than this are too noisy to use


def quantile(xs, q):
    xs = sorted(xs)
    pos = q * (len(xs) - 1)
    lo = int(pos)
    return xs[lo] + (xs[min(lo + 1, len(xs) - 1)] - xs[lo]) * (pos - lo)


def adverse(moves, side, confidence):
    """The per-sqrt(hour) log move against you that past moves beat only 1 - confidence of the time:
    a rise for sell orders you buy from ('offer'), a fall for buy orders you sell into ('request')
    and for sell orders you list yourself ('list', priced off the offers)."""
    signed = moves if side == "offer" else [-x for x in moves]
    return max(0.0, quantile(signed, confidence))


def scaled(points):
    """[(time in hours, price)] -> log price changes per sqrt(hour) between neighbouring points."""
    return [math.log(b / a) / math.sqrt(t2 - t1) for (t1, a), (t2, b) in zip(points, points[1:])
            if a > 0 and b > 0 and MIN_GAP_H <= t2 - t1 <= MAX_GAP_H]


def from_captures(days=14):
    """Past moves of each (item, city, side), from repeated looks at its best price."""
    with connect() as db:
        rows = db.execute("""SELECT item, city, side, price, seen_at FROM sightings
                             WHERE city IS NOT NULL AND seen_at > ? ORDER BY item, city, side, seen_at""",
                          (time.time() - days * 86400,)).fetchall()
    looks = {}  # key -> [[start time, best price], ...]
    for item, city, side, price, seen in rows:
        lst = looks.setdefault((item, city, side), [])
        if lst and seen - lst[-1][0] <= LOOK_GAP:
            lst[-1][1] = min(lst[-1][1], price) if side == "offer" else max(lst[-1][1], price)
        else:
            lst.append([seen, price])
    return {key: scaled([(t / 3600, p) for t, p in lst]) for key, lst in looks.items()}


def from_history(items, cities):
    """Past moves of each (item, city), from the last four days of hourly average traded prices."""
    out = {}
    for s in api("history", items, "&time-scale=1", locations=sorted(cities)):
        pts = [(datetime.fromisoformat(p["timestamp"]).timestamp() / 3600, p["avg_price"]) for p in s["data"][-96:]
               if p["item_count"] >= HISTORY_MIN_TRADES]
        out[(s["item_id"], s["location"])] = scaled(pts)
    return out


def moves(keys, confidence, download=True):
    """{key: per-sqrt(hour) log move to plan against} for every order list in keys, plus where
    the numbers came from."""
    def series(key):  # your listings are priced off the sell orders, so they move with them
        item, city, side = key
        return (item, city, "offer") if side == "list" else key

    own = {k: v for k, v in from_captures().items() if len(v) >= MIN_MOVES}
    want = {(item, city) for item, city, side in keys if series((item, city, side)) not in own}
    hist, failed = {}, False
    if want and download:
        try:
            hist = {k: v for k, v in from_history({i for i, _ in want}, {c for _, c in want}).items()
                    if len(v) >= MIN_MOVES}
        except (subprocess.CalledProcessError, OSError, ValueError):
            failed = True  # offline or the API is down: fall back to what we have
    out, kinds = {}, {}
    for key in keys:
        item, city, side = key
        if series(key) in own:
            out[key], kinds[key] = adverse(own[series(key)], side, confidence), "captures"
        elif (item, city) in hist:
            out[key], kinds[key] = adverse(hist[(item, city)], side, confidence), "history"
    typical = {side: median(v for k, v in out.items() if k[2] == side) for side in ("offer", "request", "list")
               if any(k[2] == side for k in out)}
    for key in keys:
        if key not in out:
            out[key], kinds[key] = (typical[key[2]], "typical") if key[2] in typical else (0.0, "none")
    source = {kind: sum(1 for v in kinds.values() if v == kind) for kind in ("captures", "history", "typical", "none")}
    return out, dict(source, typical_move=typical, download_failed=failed)
