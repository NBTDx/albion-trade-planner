#!/usr/bin/env python3
"""Best single trip, priced on the order lists you captured in-game (market.db).

A trip = buy in one city, carry it once, and sell in the city you arrive in.
If the destination is a refining bonus city (Thetford for bars, Martlock for
leather, Fort Sterling for planks, Lymhurst for cloth, Bridgewatch for stone blocks),
the trip can also refine there without focus and sell the result there.
Only instant trades: buy from sell orders, sell into buy orders. Every unit is
priced by working through the actual orders, so quantities are what was on the
market when you looked.

    python3 trip.py --silver 5000000 --kg 5000
    python3 trip.py --silver 5000000 --kg 5000 --from "Fort Sterling"
"""
import argparse
import itertools
import re
import sqlite3
import time

from collector import DB_PATH
from refine import BASE_BONUS, CITY_BONUS, NAMES, WEIGHT, label, recipes, return_rate

KINDS = f"({'|'.join(NAMES)})"


def load_books(max_age_minutes, seen=None):
    """books[(item, city, side)] = [[price, units], ...] best price first, from your latest look.
    If given, seen[(item, city, side)] is set to when you last looked (unix time)."""
    db = sqlite3.connect(DB_PATH)
    rows = db.execute("""
        SELECT o.item, o.city, o.side, o.price, SUM(o.amount), l.last FROM orders o JOIN (
            SELECT item, city, side, MAX(seen_at) AS last FROM orders
            WHERE city IS NOT NULL AND seen_at > ? GROUP BY item, city, side) l
        ON o.item = l.item AND o.city = l.city AND o.side = l.side AND o.seen_at > l.last - 120
        GROUP BY o.item, o.city, o.side, o.price""", (time.time() - max_age_minutes * 60,)).fetchall()
    books = {}
    for item, city, side, price, units, last in rows:
        if re.fullmatch(rf"T\d_{KINDS}(_LEVEL\d@\d)?", item):
            books.setdefault((item, city, side), []).append([price, units])
            if seen is not None:
                seen[(item, city, side)] = last
    for (_, _, side), levels in books.items():
        levels.sort(key=lambda lv: lv[0], reverse=side == "request")
    return books


def kg_of(item):
    return WEIGHT[int(item[1])]


def take(levels, units):
    """Buy/sell `units` from a price list (consuming it). Returns silver, or None if it runs out."""
    silver = 0.0
    while units > 1e-9:
        if not levels:
            return None
        n = min(units, levels[0][1])
        silver += n * levels[0][0]
        units -= n
        levels[0][1] -= n
        if levels[0][1] <= 1e-9:
            levels.pop(0)
    return silver


def flip_chunks(books, item, a, b, tax):
    """Marginal pieces of buying `item` in a and selling it into b's buy orders, best first."""
    sell = [lv[:] for lv in books.get((item, a, "offer"), [])]
    buy = [lv[:] for lv in books.get((item, b, "request"), [])]
    chunks = []
    while sell and buy:
        cost, revenue = sell[0][0], buy[0][0] * (1 - tax)
        if revenue <= cost:
            break
        n = min(sell[0][1], buy[0][1])
        chunks.append(dict(key=("flip", item), units=n, cost=n * cost, profit=n * (revenue - cost),
                           kg=n * kg_of(item), buys=[(a, item, n, cost)], sells=[(b, item, n, buy[0][0])]))
        take(sell, n)
        take(buy, n)
    return chunks


def refine_chunks(books, rec, a, home, tax, station_fee, batch=5):
    """Marginal batches of: buy raw in a, carry to the bonus city, buy the lower-tier input
    there (or in a, whichever is cheaper to start), refine without focus, sell into buy orders."""
    rrr = return_rate(BASE_BONUS + CITY_BONUS)
    raw = [lv[:] for lv in books.get((rec["raw"], a, "offer"), [])]
    lows = {c: [lv[:] for lv in books.get((rec["low"], c, "offer"), [])] for c in {a, home}}
    low_city = min((c for c in lows if lows[c]), key=lambda c: lows[c][0][0], default=None)
    out = [lv[:] for lv in books.get((rec["out"], home, "request"), [])]
    if not raw or low_city is None or not out:
        return []
    low = lows[low_city]
    station = rec["item_value"] * 0.1125 * station_fee / 100
    chunks = []
    while True:
        n_raw, n_low = batch * rec["raw_count"] * (1 - rrr), batch * (1 - rrr)
        snapshot = ([lv[:] for lv in raw], [lv[:] for lv in low], [lv[:] for lv in out])
        c_raw, c_low, revenue = take(raw, n_raw), take(low, n_low), take(out, batch)
        if None in (c_raw, c_low, revenue):
            raw, low, out = snapshot
            break
        cost = c_raw + c_low + batch * station
        profit = revenue * (1 - tax) - cost
        if profit <= 0:
            break
        carried = n_raw * WEIGHT[rec["tier"]] + (n_low * WEIGHT[rec["tier"] - 1] if low_city == a else 0)
        chunks.append(dict(key=("refine", rec["out"]), units=batch, cost=cost, profit=profit, kg=carried,
                           buys=[(a, rec["raw"], n_raw, c_raw / n_raw), (low_city, rec["low"], n_low, c_low / n_low)],
                           sells=[(home, rec["out"], batch, revenue / batch)]))
    return chunks


def allocate(chunks, silver, kg):
    """Greedy by profit per weighted share of silver and kg; try several weightings, keep the best."""
    best = []
    for w in (0, 0.25, 0.5, 0.75, 1):
        def score(c):
            use = w * c["cost"] / silver + (1 - w) * c["kg"] / kg
            return c["profit"] / use if use > 0 else float("inf")
        left_s, left_k, picked = silver, kg, []
        for c in sorted(chunks, key=score, reverse=True):
            frac = min(1, left_s / c["cost"], left_k / c["kg"] if c["kg"] else 1)
            if frac <= 0:
                continue
            if frac < 1:  # partial chunk: scale it down
                c = dict(c, units=c["units"] * frac, cost=c["cost"] * frac, profit=c["profit"] * frac, kg=c["kg"] * frac,
                         buys=[(ct, it, n * frac, p) for ct, it, n, p in c["buys"]],
                         sells=[(ct, it, n * frac, p) for ct, it, n, p in c["sells"]])
            left_s -= c["cost"]
            left_k -= c["kg"]
            picked.append(c)
        if sum(c["profit"] for c in picked) > sum(c["profit"] for c in best):
            best = picked
    return best


def summarize(lines):
    """Merge (city, item, units, price) rows into per item totals with average and worst price."""
    merged = {}
    for city, item, n, price in lines:
        m = merged.setdefault((city, item), [0, 0.0, price, price])
        m[0] += n
        m[1] += n * price
        m[2], m[3] = min(m[2], price), max(m[3], price)
    return merged


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--silver", type=float, default=5_000_000)
    p.add_argument("--kg", type=float, default=5000, help="what you can carry on the trip")
    p.add_argument("--from", dest="origin", help="only trips starting here")
    p.add_argument("--to", dest="dest", help="only trips ending here")
    p.add_argument("--max-age", type=float, default=60, help="use captures from the last N minutes (default 60)")
    p.add_argument("--exclude", nargs="+", default=[], metavar="ITEM",
                   help='leave items out, written like the output, e.g. --exclude "T6.2 hide" "T7.0 hide"')
    p.add_argument("--no-premium", action="store_true")
    p.add_argument("--station-fee", type=float, default=500)
    args = p.parse_args()

    tax = 0.08 if args.no_premium else 0.04
    books = load_books(args.max_age)
    cities = sorted({city for _, city, _ in books})
    excluded = {x.lower() for x in args.exclude}
    items = sorted({item for item, _, _ in books if label(item).lower() not in excluded})
    recs = [r for r in recipes() if not {label(r[k]).lower() for k in ("raw", "low", "out")} & excluded]
    print(f"Captured cities (last {args.max_age:g} min): {', '.join(cities) or 'none'}")

    trips = []
    for a, b in itertools.permutations(cities, 2):
        if (args.origin and a != args.origin) or (args.dest and b != args.dest):
            continue
        chunks = [c for item in items for c in flip_chunks(books, item, a, b, tax)]
        chunks += [c for rec in recs if rec["home"] == b
                   for c in refine_chunks(books, rec, a, b, tax, args.station_fee)]
        picked = allocate(chunks, args.silver, args.kg) if chunks else []
        trips.append((sum(c["profit"] for c in picked), a, b, picked))
    trips.sort(key=lambda t: t[0], reverse=True)
    if not trips or trips[0][0] <= 0:
        print("No profitable trip in your captured data.")
        return

    print(f"\n{'trip':<32} {'profit':>10} {'silver in':>11} {'kg':>6}")
    for profit, a, b, picked in trips[:6]:
        print(f"{a + ' -> ' + b:<32} {profit:>10,.0f} {sum(c['cost'] for c in picked):>11,.0f} "
              f"{sum(c['kg'] for c in picked):>6,.0f}")

    profit, a, b, picked = trips[0]
    print(f"\n=== Best trip: {a} -> {b} ===")
    buys = summarize(line for c in picked for line in c["buys"])
    sells = summarize(line for c in picked for line in c["sells"])
    for city in (a, b):
        rows = [(item, m) for (ct, item), m in buys.items() if ct == city]
        if rows:
            print(f"Buy in {city}:")
            for item, (n, total, lo, hi) in sorted(rows, key=lambda r: -r[1][1]):
                print(f"  {label(item):<16} {n:>7,.0f} units  {total:>11,.0f} silver  (pay {lo:,.0f} up to {hi:,.0f})")
    refined = [c for c in picked if c["key"][0] == "refine"]
    if refined:
        print(f"Refine in {b} (no focus):")
        crafts = {}
        for c in refined:
            crafts[c["key"][1]] = crafts.get(c["key"][1], 0) + c["units"]
        for item, n in crafts.items():
            print(f"  {label(item):<16} {n:>7,.0f} crafts")
    print(f"Sell in {b} (instantly into buy orders):")
    for (_, item), (n, total, lo, hi) in sorted(sells.items(), key=lambda r: -r[1][1]):
        print(f"  {label(item):<16} {n:>7,.0f} units  {total * (1 - tax):>11,.0f} silver after tax  "
              f"(down to {lo:,.0f})")
    # Flips and refines are priced separately, so two of them can lean on the same orders.
    for side, merged in (("offer", buys), ("request", sells)):
        for (city, item), (n, *_) in merged.items():
            available = sum(units for _, units in books.get((item, city, side), []))
            if n > available + 0.5:
                print(f"  ! {label(item)} in {city}: plan uses {n:,.0f} but only {available:,.0f} were listed")
    spent = sum(c["cost"] for c in picked)
    print(f"Total: {spent:,.0f} silver in, {sum(c['kg'] for c in picked):,.0f} kg carried -> "
          f"profit {profit:,.0f} ({profit / spent:.0%})")
    print("Orders change: reopen the destination's buy orders before you sell.")


if __name__ == "__main__":
    main()
