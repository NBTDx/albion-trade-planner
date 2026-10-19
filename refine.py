#!/usr/bin/env python3
"""Plan refining (bars, leather, planks, cloth, stone blocks) and resource flips in the Royal Cities
using live prices from the Albion Online Data Project (Europe server).

Refining route = where you buy the raw resource, where you buy the lower-tier
refined input, refining in the bonus city, and where you sell. Flip = buy a
resource in one city and sell it in another (or in the same city via orders).
Each step can be an instant trade or a market order.

Game constants come from the game's own data (ao-data/ao-bin-dumps): recipes,
item values, focus costs and weights from items.xml; refining bonuses from
craftingmodifiers.xml; the focus bonus, market tax, setup fee and station
nutrition factor from gamedata.xml. Two figures aren't in the dumps and are
community-documented: premium halving the sales tax to 4%, and the return rate
formula bonus / (1 + bonus).
"""
import argparse
import hashlib
import itertools
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

HOST = "https://europe.albion-online-data.com/api/v2/stats"
CACHE_DIR = Path(__file__).resolve().parent / ".cache"
CACHE_MINUTES = 10  # re-running scenarios within this window reuses the last download

# The five Royal Cities form a ring; neighbours are 1 hop apart, everything else 2.
RING = ["Thetford", "Fort Sterling", "Lymhurst", "Bridgewatch", "Martlock"]

# Bonus cities from craftingmodifiers.xml. Stone blocks have no enchanted versions in items.xml.
FAMILIES = {
    "bar": dict(raw="ORE", refined="METALBAR", bonus_city="Thetford", max_enchant=4),
    "leather": dict(raw="HIDE", refined="LEATHER", bonus_city="Martlock", max_enchant=4),
    "planks": dict(raw="WOOD", refined="PLANKS", bonus_city="Fort Sterling", max_enchant=4),
    "cloth": dict(raw="FIBER", refined="CLOTH", bonus_city="Lymhurst", max_enchant=4),
    "block": dict(raw="ROCK", refined="STONEBLOCK", bonus_city="Bridgewatch", max_enchant=0),
}
NAMES = {"ORE": "ore", "HIDE": "hide", "WOOD": "wood", "FIBER": "fiber", "ROCK": "stone",
         "METALBAR": "bar", "LEATHER": "leather", "PLANKS": "planks", "CLOTH": "cloth", "STONEBLOCK": "block"}

RAW_COUNT = {4: 2, 5: 3, 6: 4, 7: 5, 8: 5}  # raw resources per craft, plus 1 lower-tier refined
# Focus cost and item value depend only on tier + enchantment level.
FOCUS_BY_STEP = {4: 54, 5: 94, 6: 164, 7: 287, 8: 503, 9: 880, 10: 1539, 11: 2694, 12: 4714}
WEIGHT = {3: 0.34, 4: 0.51, 5: 0.76, 6: 1.14, 7: 1.71, 8: 2.56}  # kg per unit, raw and refined alike

BASE_BONUS = 0.18   # any Royal City refining station (craftingmodifiers.xml refiningbonus)
CITY_BONUS = 0.40   # extra in the resource's bonus city (craftingmodifiers.xml, e.g. ore in Thetford)
FOCUS_BONUS = 0.59  # extra when refining with focus (gamedata.xml ActionFocus CraftingEfficiency)
SETUP_FEE = 0.025   # charged when placing a buy or sell order (gamedata.xml MarketPlace setupfee)
PREMIUM_FOCUS_PER_DAY = 10_000


def hops(a, b):
    d = abs(RING.index(a) - RING.index(b))
    return min(d, len(RING) - d)


def return_rate(bonus):
    return bonus / (1 + bonus)


def item_id(tier, kind, ench=0):
    return f"T{tier}_{kind}_LEVEL{ench}@{ench}" if ench else f"T{tier}_{kind}"


def label(item):
    tier, kind, ench = re.fullmatch(r"T(\d)_([A-Z]+?)(?:_LEVEL(\d)@\d)?", item).groups()
    return f"T{tier}.{ench or 0} {NAMES[kind]}"


def recipe(tier, ench, fam):
    low_ench = ench if tier > 4 else 0  # T3 resources have no enchanted versions
    return dict(tier=tier, ench=ench, raw=item_id(tier, fam["raw"], ench), raw_count=RAW_COUNT[tier],
                low=item_id(tier - 1, fam["refined"], low_ench), out=item_id(tier, fam["refined"], ench),
                item_value=16 * 2 ** (tier - 4 + ench), focus=FOCUS_BY_STEP[tier + ench],
                kg_in=RAW_COUNT[tier] * WEIGHT[tier] + WEIGHT[tier - 1], kg_out=WEIGHT[tier])


def recipes(tiers=range(4, 9), enchants=range(5), families=FAMILIES.values()):
    """Every refining recipe the game has for these tiers, enchantments and families,
    each with the city it gets the refining bonus in ('home')."""
    for tier, ench, fam in itertools.product(tiers, enchants, families):
        if ench <= fam["max_enchant"]:
            yield dict(recipe(tier, ench, fam), home=fam["bonus_city"])


def get_json(url):
    cached = CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    if CACHE_MINUTES and cached.exists() and time.time() - cached.stat().st_mtime < CACHE_MINUTES * 60:
        return json.loads(cached.read_text())
    # curl rather than urllib: python.org builds on macOS often lack CA certificates.
    out = subprocess.run(["curl", "-sf", "--compressed", "--max-time", "60", url],
                         capture_output=True, check=True, text=True)
    if CACHE_MINUTES:
        CACHE_DIR.mkdir(exist_ok=True)
        cached.write_text(out.stdout)
    return json.loads(out.stdout)


def api(endpoint, items, params="", locations=RING):
    locs = ",".join(c.replace(" ", "%20") for c in locations)
    items, rows = sorted(items), []
    for i in range(0, len(items), 40):
        rows += get_json(f"{HOST}/{endpoint}/{','.join(items[i:i + 40])}.json?locations={locs}&qualities=1{params}")
    return rows


def age_hours(stamp, now):
    if stamp.startswith("0001"):
        return None
    return (now - datetime.fromisoformat(stamp)).total_seconds() / 3600


def build_book(rows, now, max_age):
    """book[item][city] = {'sell': (price, age) | None, 'buy': (price, age) | None}"""
    book = {}
    for r in rows:
        entry = book.setdefault(r["item_id"], {}).setdefault(r["city"], {"sell": None, "buy": None})
        for side, price_key, date_key in (("sell", "sell_price_min", "sell_price_min_date"),
                                          ("buy", "buy_price_max", "buy_price_max_date")):
            age = age_hours(r[date_key], now)
            if r[price_key] > 0 and age is not None and age <= max_age:
                entry[side] = (r[price_key], age)
    return book


def history(items):
    """From the last 7 days with history: vol[item][city] = average units sold per day,
    ref[item][city] = average traded price. History lags behind live prices, so treat
    these as the rough size of each market and a sanity check for order prices."""
    vol, ref = {}, {}
    for s in api("history", items, "&time-scale=24"):
        pts = s["data"][-7:]
        sold = sum(p["item_count"] for p in pts)
        if sold:
            vol.setdefault(s["item_id"], {})[s["location"]] = sold / len(pts)
            ref.setdefault(s["item_id"], {})[s["location"]] = sum(p["avg_price"] * p["item_count"] for p in pts) / sold
    return vol, ref


def apply_overrides(book, specs, default_tier):
    """Replace API prices with ones read in-game, e.g. 'Lymhurst:ore=1063/1002 t7hide=4100/3900 t6leather.1=9000/8500'.
    Each value is cheapest sell order / highest buy order; '-' or 0 means no order.
    Names: ore, hide, wood, fiber, stone, bar, leather, planks, cloth, block, optionally
    with a tier prefix (t7hide) and an enchantment suffix (.1 to .4); without a prefix
    they mean the default tier."""
    kinds = {name: kind for kind, name in NAMES.items()}

    def price(s):
        s = s.strip().replace(".", "").replace(",", "")
        return (int(s), 0.0) if s.isdigit() and int(s) > 0 else None

    for spec in specs:
        city_part, _, pairs = spec.partition(":")
        city = next((c for c in RING if c.lower() == city_part.strip().lower()), None)
        if city is None:
            raise SystemExit(f"--set: unknown city {city_part!r} (one of {', '.join(RING)})")
        for pair in re.split(r"[\s;]+", pairs.strip()):
            name, _, prices = pair.lower().partition("=")
            m = re.fullmatch(rf"(?:t(\d))?({'|'.join(kinds)})(?:[.@](\d))?", name)
            if not m or "/" not in prices:
                raise SystemExit(f"--set: can't read {pair!r}, expected e.g. ore=1063/1002 or t7hide.1=9000/8500")
            item = item_id(int(m.group(1) or default_tier), kinds[m.group(2)], int(m.group(3) or 0))
            sell, buy = prices.split("/", 1)
            book.setdefault(item, {})[city] = {"sell": price(sell), "buy": price(buy)}


def buy_options(book, item, market):
    """Ways to acquire units: instant-buy from sell orders, or place a buy order.
    Yields (city, mode, market price, planned cost per unit incl. fees, age).
    The API only shows the best price, not how many units sit there, so unless
    market.optimistic is set a big instant buy is planned at the traded average when
    that's higher, and buy orders far below the traded average are skipped (they won't fill)."""
    for city, e in book.get(item, {}).items():
        r = market.ref(item, city)
        if r is None:
            continue
        if e["sell"] and abs(e["sell"][0] - r) <= market.band * r:
            price, age = e["sell"]
            yield city, "instant", price, price if market.optimistic else max(price, r), age
        if e["buy"] and r * (1 - market.band / 2) <= e["buy"][0] <= r * (1 + market.band):
            price, age = e["buy"]
            yield city, "order", price, price * (1 + SETUP_FEE), age


def sell_options(book, item, tax, market):
    """Ways to sell units: instant-sell into buy orders, or list a sell order.
    Yields (city, mode, market price, planned net silver per unit after fees, age).
    Unless market.optimistic is set, selling volume is planned at no more than the
    traded average, and listings far above it are skipped (they won't sell)."""
    for city, e in book.get(item, {}).items():
        r = market.ref(item, city)
        if r is None:
            continue
        if e["buy"] and abs(e["buy"][0] - r) <= market.band * r:
            price, age = e["buy"]
            yield city, "instant", price, (price if market.optimistic else min(price, r)) * (1 - tax), age
        if e["sell"] and r * (1 - market.band) <= e["sell"][0] <= r * (1 + market.band / 2):
            price, age = e["sell"]
            net = price if market.optimistic else min(price, r)
            yield city, "order", price, net * (1 - tax - SETUP_FEE), age


def find_routes(book, rec, home, tax, rrr_plain, rrr_focus, station, cap, market, args):
    tier = rec["tier"]
    routes = []
    for (rc, rm, rp, rcost, ra), (lc, lm, lp, lcost, la), (sc, sm, sp, snet, sa) in itertools.product(
            buy_options(book, rec["raw"], market), buy_options(book, rec["low"], market),
            sell_options(book, rec["out"], tax, market)):
        if args.instant_only and "order" in (rm, lm, sm) or args.instant_buy and "order" in (rm, lm):
            continue
        n_hops = hops(rc, home) + hops(lc, home) + hops(home, sc)
        if args.max_hops is not None and n_hops > args.max_hops:
            continue
        inputs = rec["raw_count"] * rcost + lcost
        r = dict(raw=(rc, rm, rp), low=(lc, lm, lp), sell=(sc, sm, sp), hops=n_hops, oldest=max(ra, la, sa))
        for key, rrr in (("plain", rrr_plain), ("focus", rrr_focus)):
            r[key] = dict(
                profit=snet - inputs * (1 - rrr) - station,
                capital=inputs * (1 - rrr) + station,  # silver tied up per craft, re-refining the returns
                # crafts you can do before taking more than your share of any of the three markets
                max_crafts=min(cap(rec["out"], sc),
                               cap(rec["raw"], rc) / (rec["raw_count"] * (1 - rrr)),
                               cap(rec["low"], lc) / (1 - rrr)),
                # kg carried between cities per craft (nothing to carry if bought/sold in the bonus city)
                kg=(rc != home) * rec["raw_count"] * WEIGHT[tier] * (1 - rrr)
                   + (lc != home) * WEIGHT[tier - 1] * (1 - rrr) + (sc != home) * WEIGHT[tier])
        r["per_focus"] = (r["focus"]["profit"] - r["plain"]["profit"]) / rec["focus"]
        routes.append(r)
    return routes


def find_flips(book, items, tax, cap, market, args):
    """Buy an item in one city and sell it in another, or buy and sell it via orders in one city."""
    flips = []
    for item in items:
        for (bc, bm, bp, bcost, ba), (sc, sm, sp, snet, sa) in itertools.product(
                buy_options(book, item, market), sell_options(book, item, tax, market)):
            if args.instant_only and "order" in (bm, sm) or args.instant_buy and bm == "order":
                continue
            if bc == sc and (bm, sm) != ("order", "order"):
                continue
            if max(ba, sa) > args.flip_max_age or snet <= bcost:
                continue
            tier = int(item[1])
            flips.append(dict(item=item, buy=(bc, bm, bp), sell=(sc, sm, sp), hops=hops(bc, sc),
                              profit=snet - bcost, capital=bcost, max_units=min(cap(item, bc), cap(item, sc)),
                              kg=WEIGHT[tier] if bc != sc else 0, oldest=max(ba, sa)))
    return flips


def fmt_age(h):
    if h == 0:
        return "you"
    if h < 1:
        return f"{h * 60:.0f}m"
    return f"{h:.0f}h" if h < 48 else f"{h / 24:.0f}d"


def merge_local(book, items, max_age_hours):
    """Use prices captured by collector.py (your own market visits) wherever they're newer
    than the API's. Returns how many prices came from there and the newest one's age."""
    try:
        from collector import latest_book
    except ImportError:
        return 0, None
    used, newest = 0, None
    for item, cities in latest_book(max_age_hours * 60).items():
        if item not in items:
            continue
        for city, sides in cities.items():
            if city not in RING:
                continue
            entry = book.setdefault(item, {}).setdefault(city, {"sell": None, "buy": None})
            for side, local in sides.items():
                if local and (entry[side] is None or local[1] < entry[side][1]):
                    entry[side] = (local[0], local[1])
                    used += 1
                    newest = local[1] if newest is None else min(newest, local[1])
    return used, newest


def route_str(*legs):
    return " -> ".join(f"{c} {m} {p:,.0f}" for c, m, p in legs)


def print_routes(rec, routes, args, budget):
    print(f"=== {label(rec['out'])} (refine in {rec['home']}, focus {rec['focus']:g}/craft) ===")
    routes = sorted(routes, key=lambda r: r["focus" if budget else "plain"]["profit"], reverse=True)
    tier = int(rec["out"][1])
    print(f"  {'buy raw':<29} {'buy T' + str(tier - 1):<29} {'sell':<29} {'hops':>4} "
          f"{'profit':>7} {'w/focus':>8} {'/focus':>6} {'oldest':>6}")
    for r in routes[:args.top]:
        cells = [f"{c} {m} {p:,.0f}" for c, m, p in (r["raw"], r["low"], r["sell"])]
        print(f"  {cells[0]:<29} {cells[1]:<29} {cells[2]:<29} {r['hops']:>4} "
              f"{r['plain']['profit']:>7,.0f} {r['focus']['profit']:>8,.0f} {r['per_focus']:>6.1f} "
              f"{fmt_age(r['oldest']):>6}")
    print()


def print_summary(products, vol):
    print("=== Refining overview (best route per product) ===")
    print(f"  {'product':<16} {'sold/day':>8} {'w/focus':>8} {'/focus':>6} {'max crafts':>10} "
          f"{'no focus':>8} {'ROI':>5} {'oldest':>6}")
    for p in sorted(products, key=lambda p: p["focus_route"]["per_focus"], reverse=True):
        f, n = p["focus_route"], p["plain_route"]
        sold = sum(vol.get(p["rec"]["out"], {}).values())
        print(f"  {label(p['rec']['out']):<16} {sold:>8,.0f} {f['focus']['profit']:>8,.0f} {f['per_focus']:>6.1f} "
              f"{f['focus']['max_crafts']:>10,.0f} {n['plain']['profit']:>8,.0f} "
              f"{n['plain']['profit'] / n['plain']['capital']:>5.0%} {fmt_age(f['oldest']):>6}")
    print("  sold/day = all five cities; max crafts = with focus, before taking more than your share of a market\n")


def print_flips(flips, top):
    print("=== Best flips (by total profit within your share of both markets) ===")
    print(f"  {'item':<16} {'buy':<26} {'sell':<26} {'hops':>4} {'profit':>7} {'margin':>6} "
          f"{'units':>6} {'total':>9} {'kg/u':>5} {'oldest':>6}")
    for f in sorted(flips, key=lambda f: f["profit"] * f["max_units"], reverse=True)[:top]:
        buy, sell = (f"{c} {m} {p:,.0f}" for c, m, p in (f["buy"], f["sell"]))
        print(f"  {label(f['item']):<16} {buy:<26} {sell:<26} {f['hops']:>4} {f['profit']:>7,.0f} "
              f"{f['profit'] / f['capital']:>6.1%} {f['max_units']:>6,.0f} {f['profit'] * f['max_units']:>9,.0f} "
              f"{f['kg']:>5.2f} {fmt_age(f['oldest']):>6}")
    print()


def plan_options(products, flips):
    """Everything the budget can be spent on. Options with the same share_key sell into (or buy
    from) the same markets, so they share one volume cap."""
    options = []
    for p in products:
        out = p["rec"]["out"]
        for key, kind, focus in (("focus", "refine+focus", p["rec"]["focus"]), ("plain", "refine", 0)):
            route = p[key + "_route"]
            v = route[key]
            if v["profit"] > 0:
                options.append(dict(what=label(out), kind=kind, share_key=out, profit=v["profit"],
                                    capital=v["capital"], focus=focus, kg=v["kg"], cap=v["max_crafts"],
                                    legs=(route["raw"], route["low"], route["sell"])))
    best = {}
    for f in flips:  # one flip per item, the one that can earn the most in total
        if f["profit"] * f["max_units"] > best.get(f["item"], {}).get("total", 0):
            best[f["item"]] = dict(f, total=f["profit"] * f["max_units"])
    for f in best.values():
        options.append(dict(what=label(f["item"]), kind="flip", share_key=f["item"], profit=f["profit"],
                            capital=f["capital"], focus=0, kg=f["kg"], cap=f["max_units"],
                            legs=(f["buy"], f["sell"])))
    return options


def allocate(options, limits, weights):
    """Greedy fill, ranking options by profit per weighted share of silver, focus and kg used."""
    def score(o):
        use = sum(w * o[k] / limits[k] for k, w in weights.items() if limits[k] not in (0, float("inf")))
        return o["profit"] / use if use > 0 else float("inf")

    left, used, steps = dict(limits), {}, []
    for o in sorted(options, key=score, reverse=True):
        caps = [o["cap"] - used.get(o["share_key"], 0)]
        caps += [left[k] / o[k] for k in ("capital", "focus", "kg") if o[k] > 0]
        n = int(min(caps))
        if n <= 0:
            continue
        for k in ("capital", "focus", "kg"):
            left[k] -= n * o[k]
        used[o["share_key"]] = used.get(o["share_key"], 0) + n
        steps.append(dict(o, n=n))
    return steps


def plan_budget(options, silver, focus, max_kg):
    """Silver, focus and haul weight all run out, so try many ways of weighing them
    against each other and keep the plan that earns the most."""
    limits = dict(capital=silver, focus=focus, kg=max_kg)
    best = []
    for ws, wf in itertools.product(range(11), repeat=2):
        if ws + wf > 10:
            continue
        steps = allocate(options, limits, dict(capital=ws / 10, focus=wf / 10, kg=(10 - ws - wf) / 10))
        if sum(s["n"] * s["profit"] for s in steps) > sum(s["n"] * s["profit"] for s in best):
            best = steps
    return best


def print_plan(steps, silver, focus, max_kg, args):
    print(f"=== Plan: {silver:,.0f} silver, {focus:,.0f} focus, {max_kg:,.0f} kg hauled, over {args.days:g} days, "
          f"taking at most {args.share:.0%} of each market ===")
    print(f"  {'what':<14} {'how':<12} {'units':>6} {'silver in':>11} {'profit':>9} {'ROI':>5} {'focus':>6} "
          f"{'kg':>6}  route")
    for s in sorted(steps, key=lambda s: s["n"] * s["profit"], reverse=True):
        spent, profit = s["n"] * s["capital"], s["n"] * s["profit"]
        print(f"  {s['what']:<14} {s['kind']:<12} {s['n']:>6,} {spent:>11,.0f} {profit:>9,.0f} "
              f"{profit / spent:>5.0%} {s['n'] * s['focus']:>6,.0f} {s['n'] * s['kg']:>6,.0f}  {route_str(*s['legs'])}")
    spent = sum(s["n"] * s["capital"] for s in steps)
    profit = sum(s["n"] * s["profit"] for s in steps)
    print(f"  Total: {spent:,.0f} silver used, {sum(s['n'] * s['focus'] for s in steps):,.0f} focus, "
          f"{sum(s['n'] * s['kg'] for s in steps):,.0f} kg hauled -> expected profit {profit:,.0f} "
          f"({profit / spent if spent else 0:.0%})")
    print("  Each silver is counted once; you can re-use it after selling, so a faster cycle earns more.\n")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tier", type=int, nargs="+", default=[6], choices=sorted(RAW_COUNT))
    p.add_argument("--enchant", type=int, nargs="+", default=[0], choices=range(5))
    p.add_argument("--family", nargs="+", default=sorted(FAMILIES), choices=sorted(FAMILIES),
                   help="which refines to plan (default all)")
    p.add_argument("--no-premium", action="store_true", help="8%% sales tax instead of 4%%")
    p.add_argument("--station-fee", type=float, default=500,
                   help="station usage fee in silver per 100 nutrition (shown at the station; default 500)")
    p.add_argument("--daily-bonus", type=int, default=0, choices=[0, 10, 20],
                   help="today's production bonus %% for this resource (Activities menu)")
    p.add_argument("--focus-cost", nargs="+", default=[], metavar="TIER[.ENCH]=COST",
                   help="your focus cost per craft, e.g. 6=120 7.1=300 (default: base cost, no specialization)")
    p.add_argument("--max-age", type=float, default=24, help="ignore prices older than this many hours")
    p.add_argument("--flip-max-age", type=float, default=6,
                   help="flips only use prices at most this many hours old (default 6)")
    p.add_argument("--band", type=float, default=0.3,
                   help="ignore prices more than this far from the recent traded average (default 0.3 = 30%%)")
    p.add_argument("--optimistic", action="store_true",
                   help="plan at the best listed prices instead of capping at recent traded averages")
    p.add_argument("--max-hops", type=int, default=None, help="only refining routes with at most this many hops")
    p.add_argument("--instant-only", action="store_true", help="no buy/sell orders, only instant trades")
    p.add_argument("--instant-buy", action="store_true",
                   help="buy only instantly; sell instantly or by listing a sell order")
    p.add_argument("--top", type=int, default=8)
    p.add_argument("--details", action="store_true", help="show the route table for every product")
    p.add_argument("--set", action="append", default=[], metavar="CITY:NAME=SELL/BUY ...",
                   help="override prices with in-game ones, e.g. --set 'Lymhurst:ore=1063/1002 t7hide=4100/3900'")
    p.add_argument("--no-local", action="store_true",
                   help="ignore prices captured in-game by collector.py (market.db)")
    p.add_argument("--fresh", action="store_true",
                   help=f"re-download instead of reusing data cached in the last {CACHE_MINUTES} minutes")
    p.add_argument("--silver", type=float, help="budget plan: silver you can spend")
    p.add_argument("--focus", type=float, help="budget plan: focus points you can spend")
    p.add_argument("--max-kg", type=float, default=3000,
                   help="budget plan: total kg you'll carry between cities over all trips (default 3000);"
                        " e.g. T8 ox (3,200 kg) + pork pie (+30%%) is ~4,200 kg per trip")
    p.add_argument("--days", type=float, default=3, help="days you'll spend selling (sizes the volume caps)")
    p.add_argument("--share", type=float, default=0.2,
                   help="max share of a market's daily volume you plan to take (default 0.2)")
    args = p.parse_args()

    if args.fresh:
        for f in CACHE_DIR.glob("*.json"):
            f.unlink()
    tiers, enchants = sorted(set(args.tier)), sorted(set(args.enchant))
    budget = args.silver is not None or args.focus is not None
    tax = 0.08 if args.no_premium else 0.04
    daily = args.daily_bonus / 100
    rrr_plain = return_rate(BASE_BONUS + CITY_BONUS + daily)
    rrr_focus = return_rate(BASE_BONUS + CITY_BONUS + FOCUS_BONUS + daily)
    focus_override = {}
    for spec in args.focus_cost:
        key, _, cost = spec.partition("=")
        t, _, e = key.partition(".")
        focus_override[(int(t), int(e or 0))] = float(cost)

    recs = list(recipes(tiers, enchants, [FAMILIES[f] for f in args.family]))
    for rec in recs:
        rec["focus"] = focus_override.get((rec["tier"], rec["ench"]), rec["focus"])
    items = {rec[k] for rec in recs for k in ("raw", "low", "out")}

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    book = build_book(api("prices", items), now, args.max_age)
    local_used, local_newest = (0, None) if args.no_local else merge_local(book, items, args.max_age)
    apply_overrides(book, args.set, tiers[0])
    vol, ref = history(items)

    def cap(item, city):
        return vol.get(item, {}).get(city, 0) * args.days * args.share

    def ref_price(item, city):
        by_city = ref.get(item, {})
        return by_city.get(city) or (sum(by_city.values()) / len(by_city) if by_city else None)

    market = argparse.Namespace(ref=ref_price, band=args.band, optimistic=args.optimistic)

    print(f"Europe prices fetched {now:%Y-%m-%d %H:%M} UTC (max age {args.max_age:g}h, "
          f"within {args.band:.0%} of recent traded prices{', optimistic' if args.optimistic else ''})")
    if local_used:
        print(f"Using {local_used} prices you captured in-game (newest {fmt_age(local_newest)} ago)")
    print(f"Sales tax {tax:.0%}, order setup fee {SETUP_FEE:.1%}, station fee {args.station_fee:g}/100 nutrition")
    print(f"Return rate in bonus city: {rrr_plain:.1%} without focus, {rrr_focus:.1%} with focus\n")

    products = []
    show_details = args.details or len(recs) <= 4
    focus_budget = args.focus if args.focus is not None else PREMIUM_FOCUS_PER_DAY
    for rec in recs:
        station = rec["item_value"] * 0.1125 * args.station_fee / 100
        routes = find_routes(book, rec, rec["home"], tax, rrr_plain, rrr_focus, station, cap, market, args)
        if not routes:
            if show_details:
                print(f"=== {label(rec['out'])}: no usable prices ===\n")
            continue
        if show_details:
            print_routes(rec, routes, args, budget)
        # Pick the route that earns the most in total, not just per unit: a great price on a tiny market is no use.
        products.append(dict(
            rec=rec,
            focus_route=max(routes, key=lambda r: r["focus"]["profit"] * min(r["focus"]["max_crafts"],
                                                                             focus_budget / rec["focus"])),
            plain_route=max(routes, key=lambda r: r["plain"]["profit"] * r["plain"]["max_crafts"])))

    if products and not show_details:
        print_summary(products, vol)

    flips = find_flips(book, items, tax, cap, market, args)
    if budget or not show_details:
        print_flips(flips, args.top)

    if budget:
        silver = args.silver if args.silver is not None else float("inf")
        focus = args.focus if args.focus is not None else 0
        steps = plan_budget(plan_options(products, flips), silver, focus, args.max_kg)
        print_plan(steps, silver, focus, args.max_kg, args)

    print("Profit is per unit after tax, setup and station fees. 'order' assumes your order fills at today's")
    print("best price, which can take hours. '/focus' = extra silver per focus point. 'hops' = ring steps")
    print("hauled (neighbours = 1). Volumes and traded prices come from the price history, which lags a few days.")


if __name__ == "__main__":
    main()
