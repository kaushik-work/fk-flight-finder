"""Google Flights through a real browser. Diagnostic tool, not part of the
nightly run.

What this was built to test, and what it proved
----------------------------------------------
Our stored fares ran about 10% above what Google shows a person:

    BLR->DXB 2027-01-28   ours 24,267   Google 21,596   +12.4%
    BLR->SIN 2026-10-28   ours 33,589   Google 30,660   +9.6%

The first theory was that fast_flights hits a protobuf endpoint returning a
worse result set than the rendered page, and that a browser which waits for
the page to settle and opens the "Cheapest" tab would see the real prices.

That theory is wrong, and this module is the evidence. Running it on the
droplet returns 24,267 — the same figure the protobuf gives. Sampling the
page at 3, 6, 10, 15, 20, 30 and 45 seconds returns 24,267 every time, with
"Fetching results" gone by 20s. Waiting changes nothing.

The actual variable is the egress IP. Same URL, same minute:

    home connection      21,320  24,267  25,864  31,733  34,267  41,976
    DigitalOcean BLR             24,267  25,864  31,733  34,267

The droplet is served a strict subset. Every price it sees is on the home
list; the cheapest tier is withheld. Google prunes cheap fares for datacenter
IPs, and it does so whichever client asks — browser or protobuf.

So the fix is egress, not parsing and not patience: route the existing fast
scraper through a residential IP via PROXY_URLS, which scrape.py already
rotates. At 2.04 MB per query and 1,728 queries a night — 3.44 GB nightly,
~103 GB a month — per-GB rotating residential proxies are not viable; the
product that fits is a static residential (ISP) proxy priced per IP.

Keep this module for re-checking the gap from any given egress after a proxy
is in place, and for spot-checking a suspicious fare by hand.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

BASE = "https://www.google.com/travel/flights"

# Chromium on a 1 vCPU / 961MB droplet. --single-process keeps the footprint
# inside what is actually free once the protobuf pass and its swap are counted.
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-features=TranslateUI,BlinkGenPropertyTrees",
    "--no-first-run",
    "--no-default-browser-check",
]

BLOCKED_RESOURCES = {"image", "font", "stylesheet", "media"}

PRICE_RE = re.compile(r"(?:₹|Rs\.?|INR)\s*([\d,]+)")


@dataclass
class BrowserFare:
    price: int
    stops: int | None
    duration_minutes: int | None
    airline: str | None


def _to_int(raw: str) -> int | None:
    try:
        return int(raw.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_duration(text: str) -> int | None:
    """"7 hr 15 min" / "7 hrs 15 min" / "55 min" -> minutes."""
    if not text:
        return None
    hours = re.search(r"(\d+)\s*hr", text)
    mins = re.search(r"(\d+)\s*min", text)
    if not hours and not mins:
        return None
    return (int(hours.group(1)) * 60 if hours else 0) + (int(mins.group(1)) if mins else 0)


def _parse_stops(text: str) -> int | None:
    if not text:
        return None
    if "nonstop" in text.lower() or "non-stop" in text.lower():
        return 0
    m = re.search(r"(\d+)\s*stop", text, re.I)
    return int(m.group(1)) if m else None


def build_url(origin: str, dest: str, dep: str, ret: str) -> str:
    return (
        f"{BASE}?q=Flights%20to%20{dest}%20from%20{origin}"
        f"%20on%20{dep}%20through%20{ret}&curr=INR&hl=en-IN"
    )


class FlightBrowser:
    """One Chromium, reused. Use as a context manager."""

    def __init__(self, headless: bool = True, settle_seconds: float = 6.0, timeout_ms: int = 45000):
        self.headless = headless
        self.settle_seconds = settle_seconds
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        self._context = None

    def __enter__(self) -> "FlightBrowser":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless, args=LAUNCH_ARGS)
        self._context = self._browser.new_context(
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/141.0.0.0 Safari/537.36"
            ),
        )
        self._context.route("**/*", self._filter)
        return self

    def __exit__(self, *exc: Any) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    @staticmethod
    def _filter(route: Any, request: Any) -> None:
        if request.resource_type in BLOCKED_RESOURCES:
            route.abort()
        else:
            route.continue_()

    def cheapest(self, origin: str, dest: str, dep: str, ret: str) -> BrowserFare | None:
        """The cheapest round trip Google shows a person for this date pair."""
        page = self._context.new_page()
        try:
            page.goto(build_url(origin, dest, dep, ret), timeout=self.timeout_ms, wait_until="domcontentloaded")

            # Results stream in after load. Wait for a price to exist rather
            # than a fixed sleep, then settle briefly — the first number
            # rendered is often revised once the full set arrives.
            try:
                page.wait_for_function(
                    "() => /(₹|Rs\\.?|INR)\\s*[\\d,]{4,}/.test(document.body.innerText)",
                    timeout=self.timeout_ms,
                )
            except Exception:
                return None
            time.sleep(self.settle_seconds)

            # "Cheapest from ₹21,596" is rendered on the tab itself, so the
            # number is readable before any click.
            body = page.inner_text("body")
            m = re.search(r"Cheapest[\s\S]{0,60}?(?:₹|Rs\.?|INR)\s*([\d,]+)", body)
            target = _to_int(m.group(1)) if m else None

            # Open the Cheapest tab so the listed itineraries are the cheap
            # ones. Without this the rows belong to "Best", which ranks on
            # convenience and is what made us 10% high in the first place.
            for selector in ('[aria-label*="Cheapest"]', 'div[role="tab"]:has-text("Cheapest")', 'text=Cheapest'):
                try:
                    el = page.locator(selector).first
                    if el.count() > 0:
                        el.click(timeout=4000)
                        page.wait_for_timeout(2500)
                        break
                except Exception:
                    continue

            return self._read_top_row(page, target)
        except Exception:
            return None
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _read_top_row(self, page: Any, target: int | None) -> BrowserFare | None:
        """The listed itinerary matching the cheapest price."""
        rows = page.locator('li:has-text("hr"), [role="listitem"]')
        best: BrowserFare | None = None
        for i in range(min(rows.count(), 12)):
            try:
                text = rows.nth(i).inner_text(timeout=2000)
            except Exception:
                continue
            price = _to_int((PRICE_RE.search(text) or [None, ""])[1]) if PRICE_RE.search(text) else None
            if not price:
                continue
            fare = BrowserFare(
                price=price,
                stops=_parse_stops(text),
                duration_minutes=_parse_duration(text),
                airline=_first_airline(text),
            )
            if target and price == target:
                return fare
            if best is None or price < best.price:
                best = fare

        # The tab figure is authoritative for price even when no row matched.
        if target and (best is None or best.price != target):
            return BrowserFare(price=target, stops=None, duration_minutes=None, airline=None)
        return best


AIRLINE_HINTS = [
    "IndiGo", "Air India", "Emirates", "Gulf Air", "Oman Air", "Etihad", "Qatar Airways",
    "SpiceJet", "Vistara", "Akasa Air", "Singapore Airlines", "Malaysia Airlines",
    "Thai Airways", "AirAsia", "Sri Lankan", "SriLankan", "Cathay Pacific", "Saudia",
    "Kuwait Airways", "Turkish Airlines", "Ethiopian", "Air Arabia", "flydubai",
    "Scoot", "Batik Air", "Vietnam Airlines", "VietJet", "Korean Air", "ANA",
    "Japan Airlines", "China Southern", "China Eastern", "Bangkok Airways",
]


def _first_airline(text: str) -> str | None:
    for name in AIRLINE_HINTS:
        if name.lower() in text.lower():
            return name
    return None
