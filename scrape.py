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

# A second, non-stop-only query per date pair. It was added when the capped
# page seemed to hide direct flights; the real cause was the unread "best
# flights" block (see _parse_page). The 5-6 Oct 2026 run over five origins,
# both blocks read, logged nonstop_cheaper=0: the extra query never once beat
# the capped page. Off by default, which halves a pass. SCRAPE_NONSTOP_QUERY=1
# brings it back.
NONSTOP_QUERY = os.environ.get("SCRAPE_NONSTOP_QUERY", "0") == "1"

# Fraction taken off every scraped price before it is stored. The protobuf
# endpoint never returns Google's cheapest tier, which a person sees in a
# browser on a home connection; measured gaps ran 9.6-12.4% (browser_scrape.py,
# 18 Sep 2026). On 8 Oct 2026 the owner chose a flat 10% cut to bring stored
# prices in line with what travellers see. The raw figure is kept beside it as
# scrapedPrice, so the cut can be re-measured or undone. 0 turns it off.
PRICE_ADJUST = float(os.environ.get("SCRAPE_PRICE_ADJUST", "0.10"))

# Optional rotating proxies, comma-separated. Empty means direct from the
# droplet IP, which is the verified-working default.
PROXY_URLS = [p.strip() for p in os.environ.get("PROXY_URLS", "").split(",") if p.strip()]


# Date pairs in a row with nothing priced before a route is written off for the
# rest of the pass, and before the non-stop query is dropped for it. Three is
# enough to span more than one month of departures, so a route that only
# flies some days is not mistaken for one that never does.
DEAD_AFTER = int(os.environ.get("SCRAPE_DEAD_AFTER", "3"))

# Routes with no real service. PNQ<->BOM is a 150km hop; all 17 requests per
# pass came back empty. Kept here rather than in routes.json because that file
# is generated.
SKIP_ROUTES = {("PNQ", "BOM"), ("BOM", "PNQ")}

# A request that has not answered in this long is hung, not slow. The library
# sets no timeout of its own, and one hung socket used to stall the whole pass
# until the 20h stuck-warning in run.sh — hours of silence for one request.
# Enforced inside the HTTP client (primp), not with SIGALRM: a Python signal
# handler only runs once the native call returns, so it could never interrupt
# the hung request it was meant for.
REQUEST_TIMEOUT = int(os.environ.get("SCRAPE_REQUEST_TIMEOUT", "75"))

# Circuit breaker. Consecutive responses we could not read at all are the
# signature of a block or a payload change, not of empty routes. Past
# BREAKER_PAUSE the pass cools down and tries again; a second run of
# BREAKER_PAUSE before it has recovered stops the pass and leaves the stored
# fares alone. Hammering through a CAPTCHA wall is how an IP gets burned for
# days, and a pass of garbage is worse than no pass.
#
# BREAKER_RECOVER readable responses after a cooldown clear it. Without that, a
# blip hours later in a 15h pass was judged against one cooldown long healed,
# and aborted the whole pass.
BREAKER_PAUSE = int(os.environ.get("SCRAPE_BREAKER_PAUSE", "8"))
BREAKER_RECOVER = int(os.environ.get("SCRAPE_BREAKER_RECOVER", "50"))
BREAKER_COOLDOWN = int(os.environ.get("SCRAPE_BREAKER_COOLDOWN", "1200"))

# A round trip outside this range is a parse error, not a fare. The floor is
# below the cheapest real domestic return; the ceiling is above any economy
# long-haul return we price.
MIN_PRICE = 1500
MAX_PRICE = 400_000

# Requests actually sent to Google this pass, printed at the end so the health
# baselines in the README can be compared against a number, not a feeling.
STATS = {"requests": 0, "skipped_dead": 0, "skipped_nonstop": 0, "unreadable": 0, "empty": 0,
         "timeouts": 0, "rejected_prices": 0, "failed": 0, "captcha": 0,
         "nonstop_cheaper": 0, "unanswered": 0, "empty_retried": 0, "empty_recovered": 0}

# Share of an origin's queries that may fail outright (timeout or unreadable,
# after the retry) before its fares are withheld. The backend replaces an
# origin's fares wholesale, so posting a pass that was half blocked would drop
# every destination the block hid. Stale prices beat missing ones. The breaker
# only catches failures in a row; this catches a block that comes and goes.
ORIGIN_MAX_FAIL = float(os.environ.get("SCRAPE_ORIGIN_MAX_FAIL", "0.15"))

# POST attempts and the wait before each retry. Only network errors and 5xx are
# retried; a 4xx is the backend refusing the payload and will refuse it again.
POST_RETRY_WAITS = (30, 120)
BREAKER = {"streak": 0, "tripped": False, "healthy": 0}


def _breaker_ok() -> None:
    """A readable response: the streak ends, and enough of them heal a trip."""
    BREAKER["streak"] = 0
    BREAKER["healthy"] += 1
    if BREAKER["healthy"] >= BREAKER_RECOVER:
        BREAKER["tripped"] = False


def _breaker_bad() -> None:
    BREAKER["streak"] += 1
    BREAKER["healthy"] = 0


def breaker_check() -> None:
    """Between date pairs: cool down on the first wall, stop on the second."""
    if BREAKER["streak"] < BREAKER_PAUSE:
        return
    if BREAKER["tripped"]:
        print(f"   ! {BREAKER['streak']} unreadable responses in a row after a cooldown: "
              "Google is blocking us or the payload changed. Stopping, stored fares untouched.", flush=True)
        raise Blocked()
    BREAKER["tripped"] = True
    print(f"   ! {BREAKER['streak']} unreadable responses in a row: cooling down "
          f"{BREAKER_COOLDOWN // 60} min before trying again", flush=True)
    time.sleep(BREAKER_COOLDOWN)
    BREAKER["streak"] = 0


# primp's own timeout error; older builds lack the class, and then a timeout
# surfaces as an ordinary exception and is handled as unreadable.
try:
    from primp import TimeoutError as _HttpTimeout
except ImportError:  # pragma: no cover
    class _HttpTimeout(Exception):  # type: ignore[no-redef]
        pass


def _is_captcha(html: str) -> bool:
    """Google's "unusual traffic" interstitial, served in place of the results.

    Seen live on 5 Oct 2026: a 3.6KB page with a reCAPTCHA form, no data block.
    It parses as unreadable either way; naming it in the log is what tells an
    operator to slow down rather than to go looking for a payload change.
    """
    return "captcha-form" in html or "detected unusual traffic" in html


def _fetch_html(query: Any, proxy: str | None) -> str:
    """The library's own fetch, plus the timeout it does not set.

    Impersonation mirrors fast_flights.fetcher.fetch_flights_html (3.1.0);
    keep the two in step if the library is upgraded.
    """
    from fast_flights.fetcher import URL
    from primp import Client

    client = Client(
        impersonate="chrome_145",
        impersonate_os="macos",
        referer=True,
        proxy=proxy,
        cookie_store=True,
        timeout=REQUEST_TIMEOUT,
    )
    return client.get(URL, params=query.params()).text


class Unanswered(ValueError):
    """A results page sent before Google had results: not "no flights"."""


# A complete answer, results or none, has a payload of 31-32 slots. Seen live
# on the droplet, 5 Oct 2026: HYD->MUC 15-20 Jan 2027, up to 2 stops, came
# back with 24 slots and no results — a route with plenty of one-stops.
# Three of those in a row marked the route dead and it lost January and
# February. A short payload with no results is "not answered", never "empty".
COMPLETE_PAYLOAD_MIN = 28


class Blocked(Exception):
    """Too many unreadable responses in a row; the pass should stop."""


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


# ── Page parse ───────────────────────────────────────────────────────────────
# Our own parser, used for every page (see _parse_page for the block the
# library never reads). The history of why it exists at all:
#
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
# So this parses the payload itself and skips the unpriced entries instead of
# discarding the page. Field indices mirror the library's own parser; if Google
# changes the payload shape both will break together and the error will say so.

def _clock(raw: Any) -> str | None:
    """"HH:MM" from the raw time the payload carries, or None.

    Google drops zero components: [None, 20] is 00:20 and [2] is 02:00. Both
    used to come back None, so any flight leaving between midnight and 1am, or
    on the hour, lost its time.
    """
    if not isinstance(raw, (tuple, list)) or not raw:
        return None
    hh, mm = (list(raw) + [None, None])[:2]
    try:
        return f"{int(hh or 0):02d}:{int(mm or 0):02d}"
    except (TypeError, ValueError):
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


def _parse_page(html: str) -> list[_Itinerary]:
    """Every priced itinerary on a results page.

    This is the parser, not a fallback. The library's reads only payload[3]
    ("other flights"). When Google splits the list, payload[2] ("best
    flights") holds the rest — and on BLR->KUL 15-18 Nov, 5 Oct 2026, it held
    every non-stop, AirAsia at 29,828 included, while payload[3] held only the
    connections from 38,690 up. Reading one block is what published 61,372
    via Saigon on a route flown non-stop for half that.

    Also skips unpriced itineraries instead of failing the page (the library
    raises on one, losing every priced result with it), and returns [] for a
    page Google answered with no flights. Raises ValueError for a page with no
    data block at all, which is a block, not an answer.
    """
    from selectolax.lexbor import LexborHTMLParser

    node = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if node is None:
        # Not "no flights": the page has no data block at all. Raise so the
        # caller can tell this apart from a route that is genuinely empty.
        raise ValueError("no data block in response")
    raw = node.text().split("data:", 1)[1].rsplit(",", 1)[0]
    if raw.endswith("errorHasStatus: true"):
        return []
    payload = json.loads(raw)

    # A search with nothing to show (e.g. non-stop only on a route nobody
    # flies direct) sends both blocks as null. That is an answer — no flights
    # — not a broken page; the library crashes on it with TypeError, and it
    # used to be counted as unreadable, as if Google had blocked us.
    items: list[Any] = []
    for slot in (2, 3):
        block = payload[slot] if len(payload) > slot else None
        if block and block[0]:
            items.extend(block[0])
    if not items:
        if len(payload) < COMPLETE_PAYLOAD_MIN:
            raise Unanswered(f"no results in a {len(payload)}-slot payload")
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


class RouteState:
    """What one route has taught us so far this pass.

    A route with no flights costs two requests per date pair for nothing, and a
    route with no non-stop service costs one. After DEAD_AFTER consecutive
    departure DATES with nothing the pass stops asking. Backing off a route
    Google is not answering is also the polite behaviour when the cause is a
    block rather than an empty route.

    Misses are counted per departure date, not per date pair. The pairs are
    sorted, so the first three are 3, 5 and 7 nights from the same day; a
    per-pair count wrote a route off after one departure, which kills every
    international route that does not fly on the 15th of this month.

    Days Google did not answer (Unanswered) count only for a route that has
    not priced this pass. The 5-6 Oct run had PNQ->RUN/MXP/KIX/FCO and HYD->RUN
    answer with a short page on every date of every month — Google's way of
    saying there is nothing on the route — and, never counted, they cost ~40
    requests each per pass. A route that has priced keeps the protection: a
    run of short pages there is Google, not the route (HYD->MUC, Jan 2027).
    """

    def __init__(self) -> None:
        self.empty_streak = 0
        self.silent_streak = 0
        self.nonstop_misses = 0
        self._dep: str | None = None
        self._dep_priced = False
        self._dep_nonstop = False
        self._dep_nonstop_asked = False
        self._dep_answered = False
        self.ever_priced = False

    @property
    def dead(self) -> bool:
        if self.empty_streak >= DEAD_AFTER:
            return True
        return not self.ever_priced and self.empty_streak + self.silent_streak >= DEAD_AFTER

    @property
    def nonstop_dead(self) -> bool:
        return self.nonstop_misses >= DEAD_AFTER

    def record(self, dep: str, priced: bool, nonstop: bool, nonstop_asked: bool, answered: bool = True) -> None:
        """Note one date pair's outcome; streaks move when the departure day changes.

        A pair Google did not answer says nothing about the route: a day made
        only of those neither breaks nor extends the empty streak.
        """
        self.next_departure(dep)
        self._dep = dep
        self._dep_answered |= answered or priced
        self._dep_priced |= priced
        self.ever_priced |= priced
        self._dep_nonstop |= nonstop
        self._dep_nonstop_asked |= nonstop_asked

    def _close_day(self) -> None:
        if self._dep is None:
            return
        if self._dep_priced:
            self.empty_streak = self.silent_streak = 0
        elif self._dep_answered:
            self.empty_streak += 1
        else:
            self.silent_streak += 1
        if self._dep_nonstop_asked:
            self.nonstop_misses = 0 if self._dep_nonstop else self.nonstop_misses + 1
        self._dep_priced = self._dep_nonstop = self._dep_nonstop_asked = self._dep_answered = False

    def next_departure(self, dep: str) -> None:
        """Called before pricing a pair, so a day's misses count before the dead check."""
        if self._dep is not None and dep != self._dep:
            self._close_day()
            self._dep = None


def _search(origin: Place, dest: Place, dep: str, ret: str, max_stops: int) -> list[Any] | None:
    """Priced itineraries Google returns for one date pair at one stop cap.

    [] means Google answered: no flights. None means it did not answer (see
    Unanswered); the caller must not read that as an empty route.
    """
    STATS["requests"] += 1
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
        max_stops=max_stops,
    )

    # One retry. Failures here are mostly transient — a timeout, a dropped
    # connection, a page Google served in a shape nobody can read — and the
    # same query usually succeeds moments later. A pair that fails twice is
    # skipped rather than retried harder; hammering a failing route is how an
    # IP starts collecting CAPTCHAs.
    results: list[Any] = []
    for attempt in (0, 1):
        last = attempt == 1
        try:
            html = _fetch_html(query, _proxy_for(attempt))
        except _HttpTimeout:
            STATS["timeouts"] += 1
            _breaker_bad()
            if last:
                STATS["failed"] += 1
                print(f"      ! {origin.code}->{dest.code} {dep}: no answer in {REQUEST_TIMEOUT}s", flush=True)
                return []
            time.sleep(DELAY)
            continue
        except Exception as exc:  # noqa: BLE001 — one dead route must not end the run
            _breaker_bad()
            if last:
                STATS["unreadable"] += 1
                STATS["failed"] += 1
                print(f"      ! {origin.code}->{dest.code} {dep}: fetch failed ({type(exc).__name__})", flush=True)
                return []
            time.sleep(DELAY)
            continue

        if _is_captcha(html):
            STATS["captcha"] += 1
            _breaker_bad()
            if last:
                STATS["unreadable"] += 1
                STATS["failed"] += 1
                print(f"      ! {origin.code}->{dest.code} {dep}: CAPTCHA (Google is rate-limiting this IP)", flush=True)
                return []
            time.sleep(DELAY)
            continue

        try:
            results = _parse_page(html)
        except Unanswered as exc:
            _breaker_ok()  # a readable page, just an early one; not a block
            if last:
                STATS["unanswered"] += 1
                stops = "non-stop" if max_stops == 0 else f"up to {max_stops} stops"
                print(f"      ? {origin.code}->{dest.code} {dep}: unanswered ({stops}; {exc}); skipping, "
                      "not counting it as no flights", flush=True)
                return None
            time.sleep(DELAY)
            continue
        except Exception as exc:  # noqa: BLE001
            # "unreadable": a page with no usable data block, the one that
            # signals a block or a payload change. Worth one retry.
            _breaker_bad()
            if last:
                STATS["unreadable"] += 1
                STATS["failed"] += 1
                print(f"      ! {origin.code}->{dest.code} {dep}: unreadable ({type(exc).__name__})", flush=True)
                return []
            time.sleep(DELAY)
            continue
        _breaker_ok()
        if not results:
            # "empty": the page parsed and held no priced flight — an ordinary
            # route with no service. Asking again will not change it.
            STATS["empty"] += 1
            stops = "non-stop" if max_stops == 0 else f"up to {max_stops} stops"
            print(f"      - {origin.code}->{dest.code} {dep}: no flights ({stops})", flush=True)
            return []
        break

    priced = [r for r in results if isinstance(getattr(r, "price", None), (int, float)) and r.price > 0]
    sane = [r for r in priced if MIN_PRICE <= r.price <= MAX_PRICE]
    if len(sane) != len(priced):
        STATS["rejected_prices"] += len(priced) - len(sane)
        print(f"      ! {origin.code}->{dest.code} {dep}: dropped {len(priced) - len(sane)} "
              f"price(s) outside INR {MIN_PRICE:,}-{MAX_PRICE:,}", flush=True)
    return sane


def scrape_route(origin: Place, dest: Place, dep: str, ret: str, state: RouteState | None = None) -> dict[str, Any] | None:
    """Cheapest round-trip for one date pair, or None when there is nothing.

    One capped query by default. History: on 30 Sep 2026 BLR->KUL 15-18 Nov
    published 61,372 via Saigon while AirAsia flew it non-stop for ~30,000. A
    second, non-stop-only query was added to surface direct flights; the real
    cause turned out to be the "best flights" block nobody read (_parse_page).
    With it read, the capped page carries the non-stops too, and the extra
    query never beat it (nonstop_cheaper=0 over five origins, 5-6 Oct), so it
    is off unless SCRAPE_NONSTOP_QUERY=1.
    """
    state = state or RouteState()
    priced: list[Any] = []
    first = True
    nonstop_found = False
    nonstop_asked = False
    answered = False
    capped_empty = False
    cheapest: dict[int, int] = {}
    for cap in (sorted({0, MAX_STOPS}) if NONSTOP_QUERY else [MAX_STOPS]):
        if cap == 0 and state.nonstop_dead and MAX_STOPS != 0:
            STATS["skipped_nonstop"] += 1
            continue
        if not first:
            time.sleep(DELAY + random.uniform(0, DELAY * 0.4))
        first = False
        got = _search(origin, dest, dep, ret, cap)
        if got is None:
            continue  # not answered: no evidence either way
        answered = True
        if cap == 0:
            nonstop_asked = True
            nonstop_found = bool(got)
        elif not got:
            capped_empty = True
        if got:
            cheapest[cap] = min(r.price for r in got)
        priced.extend(got)
    # Google sometimes sends a complete-looking page with no results for a
    # route that plainly has them — BLR->DEL and DEL->MAA, busy trunk routes,
    # came back "no flights (up to 2 stops)" on whole departure days in the 5-6
    # Oct passes. The page itself cannot be told from a real empty answer; the
    # context can. When the non-stop query found flights that same day, or the
    # route has priced earlier this pass, ask the capped query once more.
    # Without the non-stop query there is no same-day evidence, so every empty
    # capped answer gets one retry; a route with no service still dies after
    # DEAD_AFTER days, at one extra request per pair.
    if capped_empty and (nonstop_found or state.ever_priced or not NONSTOP_QUERY):
        STATS["empty_retried"] += 1
        time.sleep(DELAY * 2 + random.uniform(0, DELAY))
        again = _search(origin, dest, dep, ret, MAX_STOPS)
        if again:
            STATS["empty_recovered"] += 1
            print(f"      + {origin.code}->{dest.code} {dep}: capped query empty, then "
                  f"{len(again)} priced on retry", flush=True)
            answered = True
            cheapest[MAX_STOPS] = min(r.price for r in again)
            priced.extend(again)
    # The non-stop query exists because the capped page seemed to hide direct
    # flights. With both result blocks read it should not: the capped page's
    # "best flights" carried every non-stop on BLR->KUL. Count the pairs where
    # the extra query still found something cheaper. A full pass with this at
    # zero means SCRAPE_MAX_STOPS's sibling query can go, halving the pass.
    if 0 in cheapest and MAX_STOPS in cheapest and MAX_STOPS != 0 and cheapest[0] < cheapest[MAX_STOPS]:
        STATS["nonstop_cheaper"] += 1
        print(f"      * {origin.code}->{dest.code} {dep}: non-stop query found INR {cheapest[0]:,}, "
              f"capped page only {cheapest[MAX_STOPS]:,}", flush=True)
    state.record(dep, bool(priced), nonstop_found, nonstop_asked and MAX_STOPS != 0, answered)
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


def adjusted_price(scraped: int) -> int:
    """The price that is stored and shown: the scraped price less PRICE_ADJUST."""
    return int(round(scraped * (1 - PRICE_ADJUST)))


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
        "price": adjusted_price(fare["price"]),
        "scrapedPrice": fare["price"],
        "priceAdjustment": PRICE_ADJUST,
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
    for attempt, wait in enumerate((0, *POST_RETRY_WAITS)):
        if wait:
            print(f"   retrying POST in {wait}s", flush=True)
            time.sleep(wait)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return 200 <= response.status < 300
        except urllib.error.HTTPError as exc:
            print(f"   ! POST failed: HTTP {exc.code} {exc.read()[:160].decode(errors='replace')}", flush=True)
            if exc.code < 500:
                return False  # refused, not broken: sending it again changes nothing
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
    report: dict[str, Any] = {"startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "origins": {}, "blocked": False}
    try:
        _run_origins(origins, dests, date_pairs, args, report)
    except Blocked:
        report["blocked"] = True
    report["seconds"] = int(time.time() - started)
    report["stats"] = dict(STATS)
    try:
        with open(os.path.join(HERE, "last_pass.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    except OSError:
        pass
    print(f"done in {report['seconds']}s | requests={STATS['requests']} "
          f"skipped_dead_routes={STATS['skipped_dead']} skipped_nonstop={STATS['skipped_nonstop']} "
          f"unreadable={STATS['unreadable']} empty={STATS['empty']} timeouts={STATS['timeouts']} "
          f"rejected_prices={STATS['rejected_prices']} failed={STATS['failed']} captcha={STATS['captcha']} "
          f"nonstop_cheaper={STATS['nonstop_cheaper']} unanswered={STATS['unanswered']} "
          f"empty_retried={STATS['empty_retried']} empty_recovered={STATS['empty_recovered']}" + (" BLOCKED" if report["blocked"] else ""))
    return 3 if report["blocked"] else 0


def _run_origins(origins: list[Place], dests: list[Place], date_pairs: list[tuple[str, str]],
                 args: argparse.Namespace, report: dict[str, Any]) -> None:
    for origin in origins:
        print(f"[{origin.slug}] {origin.code}", flush=True)
        fares: list[dict[str, Any]] = []
        sent_before, failed_before = STATS["requests"], STATS["failed"]
        targets = [d for d in dests if d.code != origin.code and (origin.code, d.code) not in SKIP_ROUTES]
        if args.limit:
            targets = targets[: args.limit]

        for dest in targets:
            found: list[tuple[str, str, dict[str, Any]]] = []
            state = RouteState()
            for n, (dep, ret) in enumerate(date_pairs):
                state.next_departure(dep)
                if state.dead:
                    left = len(date_pairs) - n
                    STATS["skipped_dead"] += left
                    print(f"   {dest.code} — no fares on {DEAD_AFTER} departure dates running, skipping {left} more", flush=True)
                    break
                fare = scrape_route(origin, dest, dep, ret, state)
                breaker_check()
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

        sent = STATS["requests"] - sent_before
        failed = STATS["failed"] - failed_before
        if sent and failed / sent > ORIGIN_MAX_FAIL:
            report["origins"][origin.slug] = {"fares": len(fares), "stored": False, "withheld": True,
                                              "failed": failed, "requests": sent}
            print(f"   -> {failed} of {sent} queries failed ({failed / sent:.0%}, limit {ORIGIN_MAX_FAIL:.0%}): "
                  "withholding this origin, stored fares untouched\n", flush=True)
        elif fares:
            ok = post_fares(origin.slug, fares, args.dry_run)
            report["origins"][origin.slug] = {"fares": len(fares), "stored": ok}
            print(f"   -> {len(fares)} fares, stored={ok}\n", flush=True)
        else:
            # Leaves the previous fares in place. An outage must look like stale
            # prices, never like no flights.
            print("   -> nothing usable; leaving stored fares alone\n", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
