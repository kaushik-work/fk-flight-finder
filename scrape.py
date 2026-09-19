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


def _load_env_file() -> None:
    """Read .env beside this script into the environment.

    Done here rather than relying on the caller so a cron line cannot quietly
    run without the ingest secret and then fail at the last step of a 2.5 hour
    pass. Real environment variables always win, so overriding one for a single
    run still works. No dependency: python-dotenv is not worth installing for
    six keys.
    """
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip("\"'")


_load_env_file()

# ── Configuration ────────────────────────────────────────────────────────────
API_BASE = os.environ.get("API_BASE", "https://flightklub.com")
SECRET = os.environ.get("FARE_INGEST_SECRET", "")

# Months ahead, counting the current one. 4 gives five months of coverage,
# which is what puts November and December on the page in September.
MONTHS_AHEAD = int(os.environ.get("SCRAPE_MONTHS", "4"))

# Trip lengths, in nights. Every value here multiplies the request count, so
# this is the most expensive dial in the file: three lengths is three passes
# over the whole origin x destination x date matrix.
#
# 5 leads because it is the default the board offers and the one most people
# search. 3 and 7 bracket it — a long weekend and a full week — which is the
# range the trip-length control exposes. Sampling every value from 3 to 7 is
# not worth 5x the runtime: 4 and 6 price within a few hundred rupees of their
# neighbours on these routes, and the control snaps to the nearest length we
# actually hold rather than showing an empty page.
NIGHTS_LIST = sorted({
    int(n) for n in os.environ.get("SCRAPE_NIGHTS", "3,5,7").split(",") if n.strip()
})

# The length the board defaults to, and the only one sampled on every departure
# day. Sampling all three lengths on both days would be 27 pairs per route and
# a 7.2h run, which overruns the 07:00 deadline from a 01:00 start. The
# alternates get one departure a month instead, so 5 nights keeps the date
# breadth people actually browse and 3 and 7 still exist in every month.
PRIMARY_NIGHTS = int(os.environ.get("SCRAPE_PRIMARY_NIGHTS", "5"))

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
            for nights in NIGHTS_LIST:
                # Alternates are sampled on the first departure day of the
                # month only; see PRIMARY_NIGHTS.
                if nights != PRIMARY_NIGHTS and day != SAMPLE_DAYS[0]:
                    continue
                out.append((dep.isoformat(), (dep + timedelta(days=nights)).isoformat()))
    return sorted(set(out))


def _proxy_for(attempt: int) -> str | None:
    if not PROXY_URLS:
        return None
    return PROXY_URLS[attempt % len(PROXY_URLS)]


def _sum_duration(segments: list[Any]) -> int | None:
    total = 0
    seen = False
    for seg in segments:
        minutes = getattr(seg, "duration", None)
        if isinstance(minutes, (int, float)) and minutes > 0:
            total += int(minutes)
            seen = True
    return total if seen else None


def _seg_time(segment: Any, which: str) -> str | None:
    """Local clock time of one leg's departure or arrival, as "HH:MM".

    The library hands back SimpleDatetime(date=(y, m, d), time=(hh, mm)), so
    the tuple is read directly — str() on it yields the dataclass repr, which
    is the trap the leg-date bug fell into.
    """
    raw = getattr(segment, which, None)
    if raw is None:
        return None
    parts = getattr(raw, "time", None)
    if isinstance(parts, (tuple, list)) and len(parts) >= 2:
        try:
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
        except (TypeError, ValueError):
            return None
    if isinstance(raw, str):  # the tolerant path already yields a string
        return raw
    return None


def _seg_aircraft(segment: Any) -> str | None:
    plane = getattr(segment, "plane_type", None)
    return plane if isinstance(plane, str) and plane.strip() else None


def _seg_airports(segment: Any) -> tuple[str | None, str | None]:
    """(from, to) IATA codes for one leg, from either parse path."""
    frm = getattr(segment, "from_airport", None)
    to = getattr(segment, "to_airport", None)
    frm = getattr(frm, "code", frm)
    to = getattr(to, "code", to)
    return (frm or None, to or None)


def _split_legs(
    segments: list[Any], origin_code: str, dest_code: str
) -> tuple[list[Any], list[Any]]:
    """Outbound and inbound legs of a round trip.

    Splitting on departure date — what this did before — loses any connection
    that departs on the next calendar day. A BLR-BAH-DXB itinerary whose second
    leg leaves after midnight kept only BLR-BAH, so the fare was published as a
    non-stop of the first leg's duration: Gulf Air "Direct" on 2026-12-28, a
    route Gulf Air only flies via Bahrain. Walking the legs in order and cutting
    where the itinerary first reaches the destination is true whatever the clock
    does.

    When the legs never reach the destination the shape is not what we think it
    is, so return nothing rather than guess: stops and duration then come back
    None and the page omits the claim instead of printing a wrong one.
    """
    out: list[Any] = []
    inbound: list[Any] = []
    arrived = False
    for seg in segments:
        _, to = _seg_airports(seg)
        if not arrived:
            out.append(seg)
            if to and to == dest_code:
                arrived = True
        else:
            inbound.append(seg)
            if to and to == origin_code:
                break
    if not arrived:
        return [], []
    return out, inbound


# ── Tolerant parse ───────────────────────────────────────────────────────────
# fast_flights.parser.parse_js does `price = k[1][0][1]` for every itinerary in
# the response. Google sometimes returns an itinerary with an empty price block
# (`k[1][0] == []`) — an option it will show but not price. That single entry
# raises IndexError, which aborts the parse and throws away EVERY result in the
# response, including the priced ones.
#
# Measured on the 18 Sep 2026 pass: 31 of 225 requests failed this way, and the
# losses were not small. BLR->DEL on 2026-11-15 carried 37 itineraries, 36 of
# them priced, and returned nothing at all. BLR->KIX on 2026-10-28 had 6, five
# priced, and returned nothing.
#
# So this re-parses the same payload and skips the unpriced entries instead of
# discarding the page. Field indices mirror the library's own parser; if Google
# changes the payload shape both will break together and the error will say so.

def _clock(raw: Any) -> str | None:
    """"HH:MM" from the raw [hh, mm] the payload carries, or None."""
    if isinstance(raw, (tuple, list)) and len(raw) >= 2:
        try:
            return f"{int(raw[0]):02d}:{int(raw[1] or 0):02d}"
        except (TypeError, ValueError):
            return None
    return None


class _Seg:
    __slots__ = ("departure", "arrival", "duration", "from_airport", "to_airport", "plane_type")

    def __init__(
        self,
        departure: str,
        duration: Any,
        from_airport: str | None,
        to_airport: str | None,
        arrival: str | None = None,
        plane_type: str | None = None,
    ) -> None:
        self.departure = departure
        self.arrival = arrival
        self.duration = duration
        self.from_airport = from_airport
        self.to_airport = to_airport
        self.plane_type = plane_type


class _Itinerary:
    __slots__ = ("price", "flights", "airlines")

    def __init__(self, price: int, flights: list[_Seg], airlines: list[str]) -> None:
        self.price = price
        self.flights = flights
        self.airlines = airlines


def _tolerant_parse(query: Any) -> list[_Itinerary]:
    """Re-parse a response the library rejected, skipping unpriced itineraries."""
    from fast_flights.fetcher import fetch_flights_html
    from selectolax.lexbor import LexborHTMLParser

    html = fetch_flights_html(query)
    html = getattr(html, "text", html)
    node = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if node is None:
        return []
    raw = node.text().split("data:", 1)[1].rsplit(",", 1)[0]
    if raw.endswith("errorHasStatus: true"):
        return []
    payload = json.loads(raw)

    items = payload[3][0]
    if not items:
        return []

    out: list[_Itinerary] = []
    for entry in items:
        try:
            box = entry[1]
            if not box or not isinstance(box[0], list) or len(box[0]) < 2:
                continue  # the unpriced itinerary that breaks the library
            price = box[0][1]
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            flight = entry[0]
            segs: list[_Seg] = []
            for sf in flight[2]:
                segs.append(
                    _Seg(
                        departure=_clock(sf[8]),
                        arrival=_clock(sf[10]),
                        duration=sf[11],
                        from_airport=sf[3],  # indices mirror the library parser
                        to_airport=sf[6],
                        plane_type=sf[17] if len(sf) > 17 else None,
                    )
                )
            out.append(_Itinerary(int(round(price)), segs, list(flight[1] or [])))
        except (IndexError, TypeError, ValueError):
            continue  # one malformed itinerary must not cost the rest
    return out


def _describe(segments: list[Any]) -> list[dict[str, Any]]:
    """Each leg as a plain dict, in order.

    The site only ever showed a stop count and a total duration, which is
    enough to sort by but not enough to decide on: "1 stop" says nothing about
    whether the connection is in Bahrain at 3am. The payload already carries
    departure and arrival times, both airport codes and the aircraft on every
    leg, so they are kept rather than thrown away.

    Flight numbers and terminals are not here because Google does not send
    them on this endpoint — neither is recoverable, so neither is promised.
    """
    out: list[dict[str, Any]] = []
    for seg in segments:
        frm, to = _seg_airports(seg)
        out.append({
            "from": frm,
            "to": to,
            "departTime": _seg_time(seg, "departure"),
            "arriveTime": _seg_time(seg, "arrival"),
            "durationMinutes": getattr(seg, "duration", None) if isinstance(getattr(seg, "duration", None), int) else None,
            "aircraft": _seg_aircraft(seg),
        })
    return out


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
                # Before giving up, try the tolerant parse: an IndexError here
                # is usually one unpriced itinerary taking the whole page down.
                try:
                    recovered = _tolerant_parse(query)
                except Exception:  # noqa: BLE001
                    recovered = []
                if recovered:
                    print(f"      ~ {origin.code}->{dest.code} {dep}: recovered "
                          f"{len(recovered)} priced via tolerant parse", flush=True)
                    results = recovered
                    break
                print(f"      ! {origin.code}->{dest.code} {dep}: {type(exc).__name__}", flush=True)
                return None
            time.sleep(DELAY)

    priced = [r for r in results if isinstance(getattr(r, "price", None), (int, float)) and r.price > 0]
    if not priced:
        return None
    best = min(priced, key=lambda r: r.price)

    segments = list(getattr(best, "flights", []) or [])
    out_segs, in_segs = _split_legs(segments, origin.code, dest.code)
    airlines = list(getattr(best, "airlines", []) or [])

    return {
        "price": int(round(best.price)),
        "outLegs": _describe(out_segs),
        "inLegs": _describe(in_segs),
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
        # Unknown stays null rather than becoming 0. Defaulting to zero is how
        # "we could not read the legs" turned into "Direct" on the page, which
        # is a claim about someone's itinerary we had no basis for.
        "outbound": {
            "from": origin.code, "to": dest.code, "date": dep,
            "stops": fare["outStops"],
            "durationMinutes": fare["outDuration"],
            "legs": fare.get("outLegs") or [],
            "airlineName": fare["airline"],
        },
        "inbound": {
            "from": dest.code, "to": origin.code, "date": ret,
            "stops": fare["inStops"],
            "durationMinutes": fare["inDuration"],
            "legs": fare.get("inLegs") or [],
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
    print(f"{len(origins)} origins x {len(dests)} destinations x {len(date_pairs)} date pairs "
          f"(trip lengths: {', '.join(str(n) for n in NIGHTS_LIST)} nights)")
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
