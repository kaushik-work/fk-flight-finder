"""Price one route and one date pair several ways, to see what each setting does to the "cheapest" fare.

    ./.venv/bin/python compare_one.py BLR DPS 2027-01-12 2027-01-17
    ./.venv/bin/python compare_one.py BLR DPS 2027-01-12 2027-01-17 --browser 46,800

Prints, for each variant, the cheapest price Google returns, the price we would store (raw less PRICE_ADJUST), the airline
and the stops. `--browser` takes the price you see on google.com/travel/flights in your own browser (same dates, 1 adult,
economy, INR) and prints how far the scraper is from it.

Variants: the daily scrape's own settings; with separate tickets and self-transfers hidden; with basic economy excluded;
both; and non-stop only. One request each, spaced out, so it is safe to run next to a scrape on the same droplet.
"""
from __future__ import annotations

import argparse
import sys
import time

import scrape


def cheapest(origin: scrape.Place, dest: scrape.Place, dep: str, ret: str, max_stops: int, **flags: bool) -> dict | None:
    """One request. Returns the cheapest priced itinerary as a dict, {} when Google answered with none, None when it did not answer."""
    query = scrape.build_query(origin, dest, dep, ret, max_stops, **flags)
    try:
        html = scrape._fetch_html(query, scrape._proxy_for(0))
        if scrape._is_captcha(html):
            return None
        results = scrape._parse_page(html)
    except Exception:  # noqa: BLE001 - a failed request is "no answer", the same rule the scraper now follows
        return None
    priced = [r for r in results if isinstance(getattr(r, "price", None), (int, float)) and scrape.MIN_PRICE <= r.price <= scrape.MAX_PRICE]
    if not priced:
        return {}
    best = min(priced, key=lambda r: r.price)
    segs = list(getattr(best, "flights", []) or [])
    return {
        "price": int(best.price),
        "airline": (list(getattr(best, "airlines", []) or []) or ["?"])[0],
        "segments": len(segs),
        "count": len(priced),
    }


def cheapest_retrying(origin, dest, dep, ret, max_stops, wait: float, **flags):
    """Like the daily scrape: Google sometimes answers an empty page for a route that has flights, so ask once more."""
    r = cheapest(origin, dest, dep, ret, max_stops, **flags)
    if r == {} or r is None:
        time.sleep(wait * 2)
        again = cheapest(origin, dest, dep, ret, max_stops, **flags)
        if again:
            again["retried"] = True
            return again
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("origin"), ap.add_argument("dest"), ap.add_argument("dep"), ap.add_argument("ret")
    ap.add_argument("--browser", help="the price you see on Google Flights in your browser, in rupees (commas allowed)")
    ap.add_argument("--wait", type=float, default=7.0, help="seconds between requests")
    a = ap.parse_args()
    origin = scrape.Place(a.origin.lower(), a.origin.upper(), a.origin, "India")
    dest = scrape.Place(a.dest.lower(), a.dest.upper(), a.dest, "")

    variants = [
        ("daily scrape settings", scrape.MAX_STOPS, {}),
        ("hide separate tickets / self-transfer", scrape.MAX_STOPS, {"hide_self_transfer": True}),
        ("exclude basic economy", scrape.MAX_STOPS, {"exclude_basic": True}),
        ("both of the above", scrape.MAX_STOPS, {"hide_self_transfer": True, "exclude_basic": True}),
        ("non-stop only", 0, {}),
    ]
    print(f"{a.origin.upper()} -> {a.dest.upper()}  {a.dep} to {a.ret}  (1 adult, economy, INR; stored price = raw x {1 - scrape.PRICE_ADJUST:.2f})")
    base = None
    for i, (name, stops, flags) in enumerate(variants):
        if i:
            time.sleep(a.wait)
        r = cheapest_retrying(origin, dest, a.dep, a.ret, stops, a.wait, **flags)
        if r is None:
            print(f"  {name:<40} no answer")
            continue
        if not r:
            print(f"  {name:<40} no flights")
            continue
        if base is None and i == 0:
            base = r["price"]
        diff = f"  ({(r['price'] / base - 1) * 100:+.0f}% vs daily)" if base and i else ""
        print(f"  {name:<40} raw {r['price']:>8,}  stored {scrape.adjusted_price(r['price']):>8,}  {r['airline']}{diff}{'  (needed a retry)' if r.get('retried') else ''}")
    if a.browser and base:
        shown = int(a.browser.replace(",", ""))
        print(f"\n  browser shows {shown:,}: scraper raw is {(base / shown - 1) * 100:+.1f}% vs the browser, stored price {(scrape.adjusted_price(base) / shown - 1) * 100:+.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
