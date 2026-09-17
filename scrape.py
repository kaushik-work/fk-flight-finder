#!/usr/bin/env python3
"""Fare scraper for fk-flight-finder.

Prices round-trip fares on Google Flights for a fixed route list and POSTs them
to the FlightKlub backend. Runs nightly on the Bangalore droplet; see README.

Two rules that the previous attempt got wrong, both deliberate here:

  1. EVERY priced date pair is kept, not just the cheapest. The loop pays for
     all of them either way, and throwing the rest away is what made future
     months look empty — a search for "Dubai in November" had nothing to find.
  2. An empty result set NEVER overwrites stored fares. A blocked scrape should
     look like stale prices, not like no flights.

No browser. fast-flights builds a protobuf query and parses the response, which
is both lighter and harder to detect than driving Chromium. Only add a browser
fallback if this path starts failing consistently.

Usage:
    python3 scrape.py                       # every origin
    python3 scrape.py --origins bangalore   # one origin
    python3 scrape.py --dry-run --limit 2   # price, print, write nothing
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

try:
    import fast_flights as ff
except ImportError:
    sys.exit("fast-flights is not installed. Run: ./.venv/bin/pip install fast-flights")

HERE = os.path.dirname(os.path.abspath(__file__))

# ── Configuration ────────────────────────────────────────────────────────────
API_BASE = os.environ.get("API_BASE", "https://flightklub.com")
SECRET = os.environ.get("FARE_INGEST_SECRET", "")

# Months ahead, counting the current one. 4 gives five months of coverage,
# which is what puts November and December on the page in September.
MONTHS_AHEAD = int(os.environ.get("SCRAPE_MONTHS", "4"))

# Trip length. Must match the cap the frontend enforces, or we store cards no
# search can return. See README.
NIGHTS = int(os.environ.get("SCRAPE_NIGHTS", "5"))

# Departure days sampled per month. 28 is deliberate: a 28 Sep -> 3 Oct trip is
# an ordinary September holiday and often the cheaper one, and a mid-month-only
# sample never sees it.
SAMPLE_DAYS = [int(d) for d in os.environ.get("SCRAPE_DAYS", "15,28").split(",") if d.strip()]

# Seconds between requests, jittered. Do NOT lower this to go faster — a burst
# is what earns an IP a CAPTCHA. The droplet's own IP works unproxied today
# precisely because the traffic shape is slow and steady.
DELAY = float(os.environ.get("SCRAPE_DELAY", "5"))

MAX_STOPS = int(os.environ.get("SCRAPE_MAX_STOPS", "2"))

# Optional rotating proxies, comma-separated. Empty means direct from the
# droplet IP, which is the verified-working default.
PROXY_URLS = [p.strip() for p in os.environ.get("PROXY_URLS", "").split(",") if p.strip()]


@dataclass(frozen=True)
class Place:
    slug: str
    code: str
    city: str
    country: str = ""

    def as_json(self) -> dict[str, str]:
        return {"slug": self.slug, "code": self.code, "city": self.city, "country": self.country}


def load_routes() -> tuple[list[Place], list[Place]]:
    with open(os.path.join(HERE, "routes.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    origins = [Place(o["slug"], o["code"], o.get("city", o["slug"]), "India") for o in data["origins"]]
    dests = [Place(d["slug"], d["code"], d.get("city", d["slug"]), d.get("country", "")) for d in data["destinations"]]
    return origins, dests


def sample_dates() -> list[tuple[str, str]]:
    """Departure/return pairs, from the current month forward.

    Starts at the current month rather than next: on the 15th there is still
    most of a month left to sell, and the near weeks convert best.
    """
    today = date.today()
    out: list[tuple[str, str]] = []
    for i in range(0, MONTHS_AHEAD + 1):
        year_offset, month_index = divmod(today.month - 1 + i, 12)
        for day in SAMPLE_DAYS:
            try:
                dep = date(today.year + year_offset, month_index + 1, day)
            except ValueError:
                continue  # e.g. 30 February
            # Three days' notice; sooner is rarely bookable at a sane fare.
            if dep <= today + timedelta(days=3):
                continue
            out.append((dep.isoformat(), (dep + timedelta(days=NIGHTS)).isoformat()))
    return sorted(set(out))


def _proxy_for(attempt: int) -> str | None:
    if not PROXY_URLS:
        return None
    return PROXY_URLS[attempt % len(PROXY_URLS)]


def _seg_date(segment: Any) -> str | None:
    raw = getattr(segment, "departure", None) or getattr(segment, "date", None)
    return str(raw)[:10] if raw else None


def _sum_duration(segments: list[Any]) -> int | None:
    total = 0
    seen = False
    for seg in segments:
        minutes = getattr(seg, "duration", None)
        if isinstance(minutes, (int, float)) and minutes > 0:
            total += int(minutes)
            seen = True
    return total if seen else None


def scrape_route(origin: Place, dest: Place, dep: str, ret: str) -> dict[str, Any] | None:
    """Cheapest round-trip for one date pair, or None when there is nothing."""
    query = ff.create_query(
        flights=[
            ff.FlightQuery(date=dep, from_airport=origin.code, to_airport=dest.code),
            ff.FlightQuery(date=ret, from_airport=dest.code, to_airport=origin.code),
        ],
        trip="round-trip",
        seat="economy",
        passengers=ff.Passengers(adults=1),
        currency="INR",
        language="en-US",
        max_stops=MAX_STOPS,
    )

    # One retry. Failures here are mostly transient — Google occasionally serves
    # a layout the parser does not recognise, surfacing as IndexError, and the
    # same query succeeds moments later. A route that fails twice is skipped
    # rather than retried harder; hammering a failing route is how an IP starts
    # collecting CAPTCHAs.
    results: list[Any] = []
    for attempt in (0, 1):
        proxy = _proxy_for(attempt)
        try:
            kwargs: dict[str, Any] = {}
            if proxy:
                kwargs["proxy"] = proxy
            results = list(ff.get_flights(query, **kwargs) if kwargs else ff.get_flights(query))
            break
        except ff.FlightsNotFound:
            return None  # genuinely no flights; not an error worth logging
        except TypeError:
            # This fast-flights build does not accept a proxy argument. Say so
            # once, loudly, rather than silently scraping direct while the
            # operator believes traffic is proxied.
            if proxy:
                print("      ! PROXY_URLS is set but this fast-flights build ignores it", flush=True)
            try:
                results = list(ff.get_flights(query))
                break
            except Exception:
                return None
        except Exception as exc:  # noqa: BLE001 — one dead route must not end the run
            if attempt == 1:
                print(f"      ! {origin.code}->{dest.code} {dep}: {type(exc).__name__}", flush=True)
                return None
            time.sleep(DELAY)

    priced = [r for r in results if isinstance(getattr(r, "price", None), (int, float)) and r.price > 0]
    if not priced:
        return None
    best = min(priced, key=lambda r: r.price)

    segments = list(getattr(best, "flights", []) or [])
    out_segs = [s for s in segments if _seg_date(s) == dep]
    in_segs = [s for s in segments if _seg_date(s) == ret]
    airlines = list(getattr(best, "airlines", []) or [])

    return {
        "price": int(round(best.price)),
        "outStops": max(len(out_segs) - 1, 0) if out_segs else None,
        "inStops": max(len(in_segs) - 1, 0) if in_segs else None,
        "outDuration": _sum_duration(out_segs),
        "inDuration": _sum_duration(in_segs),
        "airline": (airlines[0] if airlines else None) or "Multiple airlines",
    }


def to_fare(origin: Place, dest: Place, dep: str, ret: str, fare: dict[str, Any], retrieved_at: str) -> dict[str, Any]:
    ident = hashlib.sha1(f"gf|{origin.code}|{dest.code}|{dep}|{ret}|{fare['price']}".encode()).hexdigest()[:16]
    nights = (date.fromisoformat(ret) - date.fromisoformat(dep)).days
    deep_link = (
        "https://www.google.com/travel/flights?q="
        f"Flights%20to%20{dest.code}%20from%20{origin.code}%20on%20{dep}%20through%20{ret}"
    )
    return {
        "id": ident,
        "originSlug": origin.slug,
        "destinationSlug": dest.slug,
        "origin": origin.as_json(),
        "destination": dest.as_json(),
        "departureDate": dep,
        "returnDate": ret,
        "departureMonth": dep[:7],
        "nights": nights,
        "price": fare["price"],
        "currency": "INR",
        "outbound": {
            "from": origin.code, "to": dest.code, "date": dep,
            "stops": fare["outStops"] if fare["outStops"] is not None else 0,
            "durationMinutes": fare["outDuration"],
            "airlineName": fare["airline"],
        },
        "inbound": {
            "from": dest.code, "to": origin.code, "date": ret,
            "stops": fare["inStops"] if fare["inStops"] is not None else 0,
            "durationMinutes": fare["inDuration"],
            "airlineName": fare["airline"],
        },
        "deepLink": deep_link,
        "source": "google_flights",
        "retrievedAt": retrieved_at,
    }


def post_fares(origin_slug: str, fares: list[dict[str, Any]], dry_run: bool) -> bool:
    if dry_run:
        print(f"   [dry-run] would POST {len(fares)} fares for {origin_slug}", flush=True)
        return True
    if not SECRET:
        print("   ! FARE_INGEST_SECRET is not set; refusing to POST", flush=True)
        return False

    body = json.dumps({"originSlug": origin_slug, "fares": fares}).encode()
    request = urllib.request.Request(
        f"{API_BASE}/api/flight-fares",
        data=body,
        headers={"Content-Type": "application/json", "x-fare-secret": SECRET},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        print(f"   ! POST failed: HTTP {exc.code} {exc.read()[:160].decode(errors='replace')}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"   ! POST failed: {type(exc).__name__}", flush=True)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origins", nargs="*", help="Origin slugs; default is all.")
    parser.add_argument("--dry-run", action="store_true", help="Price and print without writing.")
    parser.add_argument("--limit", type=int, default=0, help="Stop after N destinations per origin.")
    args = parser.parse_args()

    all_origins, dests = load_routes()
    origins = [o for o in all_origins if not args.origins or o.slug in args.origins]
    if not origins:
        print(f"No matching origins. Known: {', '.join(o.slug for o in all_origins)}", file=sys.stderr)
        return 2

    date_pairs = sample_dates()
    months = sorted({dep[:7] for dep, _ in date_pairs})
    print(f"{len(origins)} origins x {len(dests)} destinations x {len(date_pairs)} date pairs")
    print(f"months: {', '.join(months)}")
    print(f"~{len(origins) * len(dests) * len(date_pairs)} requests, ~{DELAY}s apart"
          f"{' via ' + str(len(PROXY_URLS)) + ' proxies' if PROXY_URLS else ' direct'}\n", flush=True)

    started = time.time()
    for origin in origins:
        print(f"[{origin.slug}] {origin.code}", flush=True)
        fares: list[dict[str, Any]] = []
        targets = [d for d in dests if d.code != origin.code]
        if args.limit:
            targets = targets[: args.limit]

        for dest in targets:
            found: list[tuple[str, str, dict[str, Any]]] = []
            for dep, ret in date_pairs:
                fare = scrape_route(origin, dest, dep, ret)
                time.sleep(DELAY + random.uniform(0, DELAY * 0.4))
                if fare:
                    found.append((dep, ret, fare))
            if found:
                retrieved = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                for dep, ret, fare in found:
                    fares.append(to_fare(origin, dest, dep, ret, fare, retrieved))
                cheapest = min(found, key=lambda f: f[2]["price"])
                got = sorted({dep[:7] for dep, _, _ in found})
                print(f"   {dest.code} {len(found)} fares over {', '.join(got)} "
                      f"| cheapest {cheapest[0]} INR {cheapest[2]['price']:,}", flush=True)
            else:
                print(f"   {dest.code} — nothing", flush=True)

        if fares:
            ok = post_fares(origin.slug, fares, args.dry_run)
            print(f"   -> {len(fares)} fares, stored={ok}\n", flush=True)
        else:
            # Leaves the previous fares in place. An outage must look like stale
            # prices, never like no flights.
            print("   -> nothing usable; leaving stored fares alone\n", flush=True)

    print(f"done in {int(time.time() - started)}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
