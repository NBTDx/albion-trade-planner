#!/usr/bin/env python3
"""Best trade route between the cities, ranked by silver per hour, priced on the
order lists you captured in-game (market.db).

A route walks city to city. Neighbouring Royal Cities are 1 hop apart (--hop-min).
Once you've captured Caerleon or Brecilien, they are 1 hop from every Royal City,
with their own travel times (--caerleon-min, --brecilien-min). Caerleon's roads go
through red zones, so routes through it are marked.

At every stop you first sell what you brought for that city, then buy what sells
better further along, so each sale pays for the next buys. Raw resources can be
refined in their bonus city on the way (bars in Thetford, leather in Martlock,
planks in Fort Sterling, cloth in Lymhurst, stone blocks in Bridgewatch), with or
without focus. Only instant trades: buy from sell orders, sell into buy orders.
Every unit is priced by working through the orders, and all trades on a route draw
on the same order lists, so no order is counted twice.

Safety margin: the prices you captured are older by the time you reach a stop. Each
one is moved against you by how much that market's price typically moves in that
time (see drift.py), so a plan should hold about --confidence of the time (default
0.8; 0.5 turns the margin off). Routes are ranked on those prices; the plan lists
the prices you'll see in-game.

With --orders, what you carry can also be sold by listing sell orders: priced at the
city's cheapest sell order (no higher than its 7-day average traded price), after tax
and the setup fee, and capped at a share of what that city sells per day (price history
from the Albion Online Data Project; only downloaded). That silver arrives as the orders
fill, so it can't pay for buys later on the same route.

    python3 loop.py --silver 6000000 --focus 30000 --kg 5000
    python3 loop.py --from Bridgewatch --hop-min 8 --stop-min 4
    python3 loop.py --route "Fort Sterling" Lymhurst Bridgewatch Martlock Thetford
    python3 loop.py --avoid Caerleon --confidence 0.9
    python3 loop.py --orders --days 1 --share 0.2
"""
import argparse
import heapq
import itertools
import math
import re
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from collector import CITY_BY_ID
from drift import moves
from refine import (BASE_BONUS, CITY_BONUS, FOCUS_BONUS, RING, SETUP_FEE, WEIGHT, history, label, recipes,
                    return_rate)
from trip import load_books, take

LOG_PATH = Path(__file__).resolve().parent / "albiondata-client.log"
CAERLEON, BRECILIEN = "Caerleon", "Brecilien"
CITIES = RING + [CAERLEON, BRECILIEN]
RED_ZONE = {CAERLEON}
SHORT = {"Thetford": "Thet", "Fort Sterling": "FS", "Lymhurst": "Lym", "Bridgewatch": "BW", "Martlock": "Mart",
         CAERLEON: "Caer", BRECILIEN: "Brec"}
BATCH = 5  # crafts per refining step
# (silver, kg, focus) weightings tried when ranking trades; the best plan wins.
WEIGHTINGS = [(1, 0, 0), (0, 1, 0), (0, 0, 1), (.5, .5, 0), (.5, 0, .5), (0, .5, .5), (1 / 3, 1 / 3, 1 / 3)]


def city_name(text):
    """A city from its name or short name, any case."""
    for c in CITIES:
        if text.strip().lower() in (c.lower(), SHORT[c].lower()):
            return c
    raise argparse.ArgumentTypeError(f"unknown city {text!r} (one of {', '.join(CITIES)})")


def current_city():
    """The city the data client last reported you in, or None."""
    try:
        with open(LOG_PATH, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    locations = re.findall(r"Updating player location to (\S+)", tail)
    return CITY_BY_ID.get(locations[-1].rstrip(".")) if locations else None


def travel_map(cities, args):
    """minutes[a][b] for every 1-hop move: ring neighbours, plus Caerleon and Brecilien
    from every Royal City when you've captured them or asked for them. --avoid cities are left out."""
    minutes = {c: {} for c in RING}
    for i, c in enumerate(RING):
        for d in (1, -1):
            minutes[c][RING[(i + d) % len(RING)]] = args.hop_min
    asked = set(args.route or []) | {args.origin}
    for hub, m in ((CAERLEON, args.caerleon_min), (BRECILIEN, args.brecilien_min)):
        if hub in cities or hub in asked:
            minutes[hub] = {}
            for c in RING:
                minutes[c][hub] = minutes[hub][c] = args.hop_min if m is None else m
    for c in args.avoid:
        minutes.pop(c, None)
        for to in minutes.values():
            to.pop(c, None)
    return minutes


def shortest(minutes, a, b):
    """Fastest travel time from a to b in minutes, or None if there's no way."""
    best, todo = {a: 0.0}, [(0.0, a)]
    while todo:
        d, c = heapq.heappop(todo)
        if c == b:
            return d
        if d > best[c]:
            continue
        for n, m in minutes.get(c, {}).items():
            if d + m < best.get(n, math.inf):
                best[n] = d + m
                heapq.heappush(todo, (d + m, n))
    return None


def walks(start, hops, minutes):
    """Every way to walk `hops` 1-hop moves from start, visiting Caerleon and Brecilien at most once each."""
    paths = [[start]]
    for _ in range(hops):
        paths = [p + [c] for p in paths for c in minutes[p[-1]] if c in RING or c not in p]
    return paths


def tier(item):
    return int(item[1])


def listings(books, days, share, band):
    """books[(item, city, "list")] = [[price, units]]: a sell order you could list there, at the cheapest
    sell order but no higher than the average traded price, for up to `share` of `days` of its sales."""
    vol, ref = history({item for item, _, _ in books})
    out = {}
    for (item, city, side), levels in books.items():
        r, sold = ref.get(item, {}).get(city), vol.get(item, {}).get(city, 0)
        if side == "offer" and r and sold and r * (1 - band) <= levels[0][0] <= r * (1 + band / 2):
            out[(item, city, "list")] = [[min(levels[0][0], r), sold * days * share]]
    return out


def net(side, args):
    """Share of the price you keep: instant sales pay tax, listings tax plus the setup fee."""
    return 1 - args.tax - (SETUP_FEE if side == "list" else 0)


def margins(stops, legs, seen, move, args):
    """adj(key, s): what a price on order list `key` is planned at when you trade at stop s, as a
    factor of the captured price. Above 1 for buys, below 1 for sales; 1 with no margin."""
    if not move:
        return lambda key, s: 1.0
    eta = [0.0]  # minutes from now until you reach each stop
    for m in legs:
        eta.append(eta[-1] + args.stop_min + m)
    cache = {}

    def adj(key, s):
        if (key, s) not in cache:
            item, city, side = key
            base = (item, city, "offer") if side == "list" else key  # listings are priced off the sell orders
            hours = max(0.0, args.now - seen.get(base, args.now)) / 3600 + eta[s] / 60
            f = math.exp(move.get(key, 0.0) * math.sqrt(hours))
            cache[(key, s)] = f if side == "offer" else 1 / f
        return cache[(key, s)]
    return adj


def options(books, stops, recs, args):
    """Every trade the route allows that makes money at the captured prices."""
    n, opts = len(stops), []
    sides = ("request", "list") if args.orders else ("request",)
    for item in {item for item, _, _ in books}:
        for (i, j), side in itertools.product(itertools.combinations(range(n), 2), sides):
            sell, buy = books.get((item, stops[i], "offer")), books.get((item, stops[j], side))
            if stops[i] != stops[j] and sell and buy and buy[0][0] * net(side, args) > sell[0][0]:
                opts.append(dict(kind="flip", item=item, i=i, j=j, side=side))
    for rec in recs:
        for h in (h for h in range(n) if stops[h] == rec["home"]):
            for i, l, k in itertools.product(range(h + 1), range(h + 1), range(h, n)):
                for side in sides:
                    if (books.get((rec["raw"], stops[i], "offer")) and books.get((rec["low"], stops[l], "offer"))
                            and books.get((rec["out"], stops[k], side))):
                        for with_focus in ((False, True) if args.focus else (False,)):
                            opts.append(dict(kind="refine", rec=rec, i=i, l=l, h=h, k=k, focus=with_focus, side=side))
    return opts


def price_of(levels, units):
    """Silver for `units` off the top of a price list, leaving the list as it is; None if it runs out."""
    silver = 0.0
    for price, n in levels:
        k = min(units, n)
        silver += k * price
        units -= k
        if units <= 1e-9:
            return silver
    return None


def uses_of(o, stops):
    """The order lists trade o reads."""
    if o["kind"] == "flip":
        return {(o["item"], stops[o["i"]], "offer"), (o["item"], stops[o["j"]], o["side"])}
    rec = o["rec"]
    return {(rec["raw"], stops[o["i"]], "offer"), (rec["low"], stops[o["l"]], "offer"),
            (rec["out"], stops[o["k"]], o["side"])}


def sale(s, side, item, units, planned, captured, price):
    """The cash flow (at planned prices) and plan line (at captured prices) for selling at stop s.
    Listed sales pay out after the route (stop None)."""
    if side == "list":
        return (None, planned), (s, "list", item, units, captured, f"as sell orders at {price:,.0f}")
    return (s, planned), (s, "sell", item, units, captured, "")


def step(books, stops, o, args, focus_left, adj):
    """The next batch of trade o at the current top of the order lists, or None if it no longer pays
    once the safety margin is applied. 'profit' is with the margin, 'captured' at captured prices."""
    legs = [0.0] * (len(stops) - 1)
    if o["kind"] == "flip":
        src, dst = (o["item"], stops[o["i"]], "offer"), (o["item"], stops[o["j"]], o["side"])
        sell, buy = books.get(src), books.get(dst)
        if not sell or not buy:
            return None
        cost0, revenue0 = sell[0][0], buy[0][0] * net(o["side"], args)
        cost, revenue = cost0 * adj(src, o["i"]), revenue0 * adj(dst, o["j"])
        if revenue <= cost:
            return None
        n = min(sell[0][1], buy[0][1])
        for s in range(o["i"], o["j"]):
            legs[s] = n * WEIGHT[tier(o["item"])]
        flow, line = sale(o["j"], o["side"], o["item"], n, n * revenue, n * revenue0, buy[0][0])
        return dict(profit=n * (revenue - cost), captured=n * (revenue0 - cost0), flows=[(o["i"], -n * cost), flow],
                    legs=legs, focus=0, take=[(src, n), (dst, n)],
                    lines=[(o["i"], "buy", o["item"], n, n * cost0, f"-> sell in {stops[o['j']]}"), line])

    rec = o["rec"]
    rrr = args.rrr_focus if o["focus"] else args.rrr_plain
    keys = dict(raw=(rec["raw"], stops[o["i"]], "offer"), low=(rec["low"], stops[o["l"]], "offer"),
                out=(rec["out"], stops[o["k"]], o["side"]))
    lists = {k: books.get(key, []) for k, key in keys.items()}
    crafts = min(BATCH, focus_left / rec["focus"]) if o["focus"] else BATCH
    per_craft = dict(raw=rec["raw_count"] * (1 - rrr), low=1 - rrr, out=1)
    if any(price_of(lists[k], crafts * per_craft[k]) is None for k in lists):  # a list runs short: use what's left
        crafts = min(crafts, *(sum(u for _, u in lists[k]) / per_craft[k] for k in lists))
    if crafts < 1e-6:
        return None
    n_raw, n_low = crafts * per_craft["raw"], crafts * per_craft["low"]
    c_raw0, c_low0, revenue0 = (price_of(lists["raw"], n_raw), price_of(lists["low"], n_low),
                                price_of(lists["out"], crafts))
    if None in (c_raw0, c_low0, revenue0):
        return None
    price = revenue0 / crafts
    revenue0 *= net(o["side"], args)
    c_raw, c_low = c_raw0 * adj(keys["raw"], o["i"]), c_low0 * adj(keys["low"], o["l"])
    revenue = revenue0 * adj(keys["out"], o["k"])
    station = crafts * rec["item_value"] * 0.1125 * args.station_fee / 100
    profit = revenue - c_raw - c_low - station
    if profit <= 0:
        return None
    for s in range(o["i"], o["h"]):
        legs[s] += n_raw * WEIGHT[rec["tier"]]
    for s in range(o["l"], o["h"]):
        legs[s] += n_low * WEIGHT[rec["tier"] - 1]
    for s in range(o["h"], o["k"]):
        legs[s] += crafts * WEIGHT[rec["tier"]]
    home, how = stops[o["h"]], "with focus" if o["focus"] else "no focus"
    flow, line = sale(o["k"], o["side"], rec["out"], crafts, revenue, revenue0, price)
    return dict(profit=profit, captured=revenue0 - c_raw0 - c_low0 - station,
                flows=[(o["i"], -c_raw), (o["l"], -c_low), (o["h"], -station), flow],
                legs=legs, focus=crafts * rec["focus"] if o["focus"] else 0,
                take=[(keys["raw"], n_raw), (keys["low"], n_low), (keys["out"], crafts)],
                lines=[(o["i"], "buy", rec["raw"], n_raw, c_raw0, f"-> refine in {home}"),
                       (o["l"], "buy", rec["low"], n_low, c_low0, f"-> refine in {home}"),
                       (o["h"], "refine", rec["out"], crafts, station, how), line])


def plan(books0, stops, opts, args, weights, adj):
    """Greedy: keep taking the trade batch with the most profit per weighted use of silver,
    carrying capacity and focus, while every stop still has the silver to pay for its buys."""
    books = {k: [lv[:] for lv in v] for k, v in books0.items()}
    n = len(stops)
    cash = [args.silver] * n  # silver in hand after trading at each stop
    load = [0.0] * (n - 1)
    focus_left = args.focus
    ws, wk, wf = weights
    uses = [uses_of(o, stops) for o in opts]
    picked, alive, later, captured = [], list(range(len(opts))), 0.0, 0.0
    cache = {}  # option index -> its next batch; dropped when an order list it uses changes
    while True:
        for idx in alive:
            if idx not in cache:
                st = step(books, stops, opts[idx], args, focus_left, adj)
                if st is not None:
                    need = [0.0] * n
                    st["later"] = 0.0  # from sell orders, paid out after the route
                    for s, amount in st["flows"]:
                        if s is None:
                            st["later"] += amount
                            continue
                        for t in range(s, n):
                            need[t] -= amount
                    st["need"] = need
                    st["need_at"] = [(t, v) for t, v in enumerate(need) if v > 1e-9]
                    st["legs_at"] = [(s, kg) for s, kg in enumerate(st["legs"]) if kg > 1e-9]
                    use = (ws * max(need) / args.silver + wk * sum(st["legs"]) / (args.kg * (n - 1))
                           + (wf * st["focus"] / args.focus if args.focus else 0))
                    st["score"] = st["profit"] / use if use > 0 else float("inf")
                cache[idx] = st
        # top-of-list margins only shrink, so a trade that stopped paying won't pay later either
        alive = sorted((idx for idx in alive if cache[idx] is not None), key=lambda i: cache[i]["score"], reverse=True)
        best = None
        for idx in alive:  # the best-scoring trade that still fits
            st = cache[idx]
            f = 1.0
            for t, v in st["need_at"]:
                f = min(f, cash[t] / v)
            for s, kg in st["legs_at"]:
                f = min(f, (args.kg - load[s]) / kg)
            if st["focus"]:
                f = min(f, focus_left / st["focus"])
            if st["later"] and args.max_in_orders is not None:
                f = min(f, (args.max_in_orders - later) / st["later"])
            if f > 1e-6:
                best = (st, f)
                break
        if best is None:
            break
        st, f = best
        need = st["need"]
        touched = {key for key, _ in st["take"]}
        for key, units in st["take"]:
            take(books[key], units * f)
        for t in range(n):
            cash[t] -= need[t] * f
        for s in range(n - 1):
            load[s] += st["legs"][s] * f
        focus_left -= st["focus"] * f
        later += st["later"] * f
        captured += st["captured"] * f
        for idx in alive:  # focus only limits a batch once there's less left than a full one needs
            o = opts[idx]
            if touched & uses[idx] or (st["focus"] and o.get("focus") and focus_left < BATCH * o["rec"]["focus"]):
                cache.pop(idx, None)
        picked += [(s, verb, item, units * f, silver * f, note) for s, verb, item, units, silver, note in st["lines"]]
    profit = cash[-1] + later - args.silver
    return dict(profit=profit, captured=captured, later=later, lines=picked, cash=cash, load=load,
                focus=args.focus - focus_left)


def best_plan(books, stops, legs, recs, args, seen, move):
    opts = options(books, stops, recs, args)
    if not opts:
        return None
    adj = margins(stops, legs, seen, move, args)
    weightings = WEIGHTINGS if args.focus else [w for w in WEIGHTINGS if w[2] == 0]
    result = max((plan(books, stops, opts, args, w, adj) for w in weightings), key=lambda p: p["profit"])
    trade_stops = {s for s, *_ in result["lines"]}
    minutes = sum(legs) + len(trade_stops) * args.stop_min
    return dict(result, stops=stops, legs=legs, minutes=minutes, per_hour=result["profit"] / minutes * 60,
                red=any(c in RED_ZONE for c in stops))


_shared = {}


def _share(ctx):
    _shared.update(ctx)


def _plan_route(stops):
    legs = [_shared["minutes"][a][b] for a, b in zip(stops, stops[1:])]
    return best_plan(_shared["books"], stops, legs, _shared["recs"], _shared["args"], _shared["seen"],
                     _shared["move"])


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--silver", type=float, default=5_000_000)
    p.add_argument("--kg", type=float, default=5000, help="what you can carry on each leg")
    p.add_argument("--focus", type=float, default=0, help="focus points you can spend on refining")
    p.add_argument("--focus-cost", nargs="+", default=[], metavar="TIER[.ENCH]=COST",
                   help="your focus cost per craft, e.g. 6.2=400 (default: base cost, no specialization)")
    p.add_argument("--hop-min", type=float, default=10, help="minutes per hop between neighbouring Royal Cities")
    p.add_argument("--caerleon-min", type=float, help="minutes between a Royal City and Caerleon (default: --hop-min)")
    p.add_argument("--brecilien-min", type=float,
                   help="minutes between a Royal City and Brecilien (default: --hop-min; fast-travel fees aren't counted)")
    p.add_argument("--stop-min", type=float, default=3, help="minutes of trading at each city you trade in")
    p.add_argument("--max-hops", type=int, default=5)
    p.add_argument("--from", dest="origin", type=city_name,
                   help="only routes starting here (default: all, marked from where you are)")
    p.add_argument("--route", nargs="+", type=city_name, metavar="CITY", help="plan just this route")
    p.add_argument("--avoid", nargs="+", type=city_name, default=[], metavar="CITY",
                   help="never go through these cities, e.g. --avoid Caerleon")
    p.add_argument("--max-age", type=float, default=60, help="use captures from the last N minutes (default 60)")
    p.add_argument("--confidence", type=float, default=0.8,
                   help="how sure the plan should be that prices hold until you get there (default 0.8; 0.5 = no margin)")
    p.add_argument("--no-download", action="store_true",
                   help="don't download price history for the safety margin; use only your own captures")
    p.add_argument("--no-premium", action="store_true")
    p.add_argument("--station-fee", type=float, default=500)
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--orders", action="store_true", help="also sell by listing sell orders (see above)")
    p.add_argument("--days", type=float, default=1, help="with --orders: days you'll wait for orders to fill")
    p.add_argument("--share", type=float, default=0.2,
                   help="with --orders: most of a city's daily sales you plan to take (default 0.2)")
    p.add_argument("--max-in-orders", type=float, metavar="SILVER",
                   help="with --orders: most silver to have waiting in sell orders, so you keep cash for the next run")
    p.add_argument("--band", type=float, default=0.3,
                   help="with --orders: skip listings this far from the average traded price (default 0.3)")
    return p


def plan_routes(args, log=print):
    """Everything the CLI and the UI show: what's captured, where you are, the margin used,
    and the routes ranked by silver per hour (or just the --route one)."""
    if not 0 < args.confidence < 1:
        raise ValueError("--confidence must be between 0 and 1, e.g. 0.8")
    args.tax = 0.08 if args.no_premium else 0.04
    args.rrr_plain = return_rate(BASE_BONUS + CITY_BONUS)
    args.rrr_focus = return_rate(BASE_BONUS + CITY_BONUS + FOCUS_BONUS)
    args.margin = args.confidence > 0.5
    args.now = time.time()
    focus_cost = {}
    for spec in args.focus_cost:
        key, _, cost = spec.partition("=")
        t, _, e = key.partition(".")
        focus_cost[(int(t), int(e or 0))] = float(cost)
    recs = list(recipes())
    for rec in recs:
        rec["focus"] = focus_cost.get((rec["tier"], rec["ench"]), rec["focus"])

    seen = {}
    books = load_books(args.max_age, seen)
    if args.orders:
        books.update(listings(books, args.days, args.share, args.band))
    captured = sorted({city for _, city, _ in books})
    minutes = travel_map(captured, args)
    here = current_city()
    keys = list(seen) + [key for key in books if key[2] == "list"]
    move, margin = moves(keys, args.confidence, not args.no_download) if args.margin and seen else ({}, None)
    out = dict(captured=captured, here=here, margin=margin, minutes=minutes, routes=[])

    if args.route:
        legs = [shortest(minutes, a, b) for a, b in zip(args.route, args.route[1:])]
        if None in legs:
            raise ValueError("can't get between some of those cities (check --avoid)")
        r = best_plan(books, args.route, legs, recs, args, seen, move)
        out["routes"] = [r] if r and r["profit"] > 0 else []
    else:
        starts = [args.origin] if args.origin else list(minutes)
        all_stops = [stops for start in starts if start in minutes
                     for hops in range(1, args.max_hops + 1) for stops in walks(start, hops, minutes)]
        log(f"Checking {len(all_stops):,} routes...")
        ctx = dict(books=books, recs=recs, args=args, seen=seen, move=move, minutes=minutes)
        with ProcessPoolExecutor(initializer=_share, initargs=(ctx,)) as pool:
            routes = list(pool.map(_plan_route, all_stops, chunksize=8))
        out["routes"] = sorted((r for r in routes if r and r["profit"] > 0), key=lambda r: r["per_hour"], reverse=True)
    for r in out["routes"]:
        r["from_you"] = shortest(minutes, here, r["stops"][0]) if here in minutes else None
    return out


def route_name(stops):
    return " > ".join(SHORT[c] for c in stops)


def stop_rows(r):
    """For each stop of route r, the trades to make there (sells first), merged per item."""
    merged = {}
    for s, verb, item, units, silver, note in r["lines"]:
        m = merged.setdefault((s, verb, item, note), [0.0, 0.0])
        m[0] += units
        m[1] += silver
    order = {"sell": 0, "refine": 1, "list": 2, "buy": 3}
    out = []
    for s in range(len(r["stops"])):
        rows = [dict(verb=v, item=it, label=label(it), note=note, units=m[0], silver=m[1])
                for (st, v, it, note), m in merged.items() if st == s and m[0] >= 0.5]
        out.append(sorted(rows, key=lambda row: (order[row["verb"]], -row["silver"])))
    return out


def margin_text(res, args):
    m = res["margin"]
    if not args.margin:
        return "No safety margin (--confidence 0.5)."
    if not m:
        return "No safety margin: nothing captured to measure."
    parts = [f"{m[k]} from {what}" for k, what in (("captures", "your captures"), ("history", "Data Project history"),
                                                    ("typical", "the typical value"), ("none", "no data")) if m[k]]
    t = m["typical_move"]
    typical = (f"; typical move against you per sqrt(hour): {t['offer']:.1%} up for buys, {t['request']:.1%} down"
               f" for sales" if "offer" in t and "request" in t else "")
    offline = " (history download failed, so this is offline)" if m["download_failed"] else ""
    return (f"Safety margin at {args.confidence:.0%} confidence; price movement for {', '.join(parts)}"
            f"{typical}{offline}.")


def print_route(r, args):
    stops = r["stops"]
    print(f"\n=== {' > '.join(stops)} ===" + ("   (red zone: full loot)" if r["red"] else ""))
    print(f"{len(stops) - 1} hops, about {r['minutes']:.0f} min -> profit {r['profit']:,.0f} ({r['per_hour']:,.0f}/hr)"
          + (f", {r['captured']:,.0f} if captured prices hold" if args.margin else ""))
    for s, (city, rows) in enumerate(zip(stops, stop_rows(r))):
        print(f"{s + 1}. {city}" + ("" if rows else "  (just passing)"))
        for row in rows:
            if row["verb"] == "refine":
                print(f"     refine {row['label']:<14} {row['units']:>7,.0f} crafts  {row['note']}, "
                      f"station fees {row['silver']:,.0f}")
            else:
                what = {"buy": "for", "sell": "after tax", "list": "after fees"}[row["verb"]]
                print(f"     {row['verb']:<6} {row['label']:<14} {row['units']:>7,.0f}  {what} {row['silver']:>11,.0f}  "
                      f"(~{row['silver'] / row['units']:,.0f} each)  {row['note']}")
        if s < len(stops) - 1:
            red = "  (red zone)" if {city, stops[s + 1]} & RED_ZONE else ""
            print(f"     silver left at least {r['cash'][s]:,.0f}, carrying {r['load'][s]:,.0f} kg to "
                  f"{stops[s + 1]} ({r['legs'][s]:g} min){red}")
    if r["focus"]:
        print(f"Focus used: {r['focus']:,.0f}")
    print(f"You end with at least {r['cash'][-1]:,.0f} silver"
          + (f", plus {r['later']:,.0f} as your sell orders fill (sized to {args.days:g} day(s) at most "
             f"{args.share:.0%} of each city's sales)." if r["later"] >= 1 else "."))
    print("Prices shown are what you captured; reopen each market before you trade.")


def main():
    args = parser().parse_args()
    try:
        res = plan_routes(args)
    except ValueError as e:
        raise SystemExit(str(e))
    here = res["here"]
    print(f"Captured cities (last {args.max_age:g} min): {', '.join(res['captured']) or 'none'}")
    print(f"{args.silver:,.0f} silver, {args.kg:,.0f} kg per leg, {args.focus:,.0f} focus; "
          f"{args.hop_min:g} min per hop + {args.stop_min:g} min per trading stop"
          + (f"; you're in {here}" if here else "")
          + (f"\nSell orders allowed, sized to {args.days:g} day(s) at most {args.share:.0%} of each city's sales"
             if args.orders else ""))
    print(margin_text(res, args))
    routes = res["routes"]
    if not routes:
        print("Nothing profitable on that route." if args.route else "No profitable route in your captured data.")
        return
    if args.route:
        print_route(routes[0], args)
        return

    print(f"\n{'route':<34} {'hops':>4} {'min':>4} {'profit':>10} {'silver/hr':>10} {'focus':>6}"
          + (f" {'in orders':>10}" if args.orders else "") + ("  from you" if here else ""))
    shown = routes[:args.top]
    if here and not args.origin:
        shown += [r for r in routes if r["stops"][0] == here and r not in shown][:3]
    for r in shown:
        name = route_name(r["stops"]) + (" *" if r["red"] else "")
        print(f"{name:<34} {len(r['stops']) - 1:>4} {r['minutes']:>4.0f} {r['profit']:>10,.0f} "
              f"{r['per_hour']:>10,.0f} {r['focus']:>6,.0f}"
              + (f" {r['later']:>10,.0f}" if args.orders else "")
              + (f"  {r['from_you']:.0f} min" if r["from_you"] is not None else ""))
    if any(r["red"] for r in shown):
        print("* goes through Caerleon's red zones (full loot); --avoid Caerleon to skip them")
    print_route(routes[0], args)
    if here and routes[0]["stops"][0] != here:
        mine = next((r for r in routes if r["stops"][0] == here), None)
        if mine:
            print(f"\nBest route starting where you are ({here}):")
            print_route(mine, args)


if __name__ == "__main__":
    main()
