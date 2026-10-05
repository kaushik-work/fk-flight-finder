"""End-to-end tests on real Google responses. No network.

The fixtures are the data block of two live responses for BLR->KUL, 15-18 Nov
2026, captured 5 Oct 2026 — the route where the 2-stop query alone published
61,372 via Saigon while AirAsia flew it non-stop for about half. They are
trimmed to the one <script> both parsers read.

Run:  ./.venv/bin/python -m unittest discover tests
"""
import argparse
import gzip
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scrape  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
BLR = scrape.Place("bangalore", "BLR", "Bengaluru", "India")
KUL = scrape.Place("kuala-lumpur", "KUL", "Kuala Lumpur", "Malaysia")
CAPTCHA = "<html><body>Our systems have detected unusual traffic from your computer network.</body></html>"


def fixture(name: str) -> str:
    with open(os.path.join(FIX, f"{name}.html.gz"), "rb") as fh:
        return gzip.decompress(fh.read()).decode()


def edit_payload(html: str, fn) -> str:
    """Apply fn to the parsed payload and put it back, keeping the wrapper."""
    head, _, rest = html.partition("data:")
    raw, sep, tail = rest.rpartition(",")
    payload = json.loads(raw)
    fn(payload)
    return head + "data:" + json.dumps(payload) + sep + tail


class Base(unittest.TestCase):
    """Fresh counters, no sleeping, and a fake network."""

    def setUp(self):
        scrape.BREAKER.update(streak=0, tripped=False, healthy=0)
        for k in scrape.STATS:
            scrape.STATS[k] = 0
        patches = [
            mock.patch.object(scrape.time, "sleep", lambda s: None),
            mock.patch.object(scrape, "_fetch_html", side_effect=self.fetch),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.pages: list[str] = []
        self.fetches = 0

    def fetch(self, query, proxy):
        self.fetches += 1
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


class RealResponses(Base):
    def test_both_parsers_agree_on_real_pages(self):
        from fast_flights.parser import parse
        for name in ("blr_kul_nonstop", "blr_kul_2stop"):
            html = fixture(name)
            lib = [(r.price, [(f.from_airport.code, f.to_airport.code) for f in r.flights]) for r in parse(html)]
            ours = [(r.price, [(f.from_airport, f.to_airport) for f in r.flights]) for r in scrape._tolerant_parse(html)]
            self.assertEqual(lib, ours, name)
            self.assertTrue(lib, name)

    def test_nonstop_query_wins_on_the_route_that_was_wrong(self):
        # Order of queries is sorted({0, MAX_STOPS}): non-stop first.
        self.pages = [fixture("blr_kul_nonstop"), fixture("blr_kul_2stop")]
        fare = scrape.scrape_route(BLR, KUL, "2026-11-15", "2026-11-18")
        self.assertEqual(fare["price"], 29828)
        self.assertEqual(fare["outStops"], 0)
        self.assertEqual([(l["from"], l["to"]) for l in fare["outLegs"]], [("BLR", "KUL")])
        self.assertEqual(self.fetches, 2)

    def test_two_stop_query_alone_overprices(self):
        # The bug, kept as a test: without the non-stop query the cheapest
        # thing Google shows is a connection, well above the real fare.
        with mock.patch.object(scrape, "MAX_STOPS", 2):
            self.pages = [fixture("blr_kul_2stop")]
            got = scrape._search(BLR, KUL, "2026-11-15", "2026-11-18", 2)
        self.assertGreater(min(r.price for r in got), 29828)
        self.assertTrue(all(len(r.flights) > 1 for r in got))

    def test_fare_record_never_claims_an_unknown_inbound(self):
        # Google's round-trip payload carries the outbound only; the inbound
        # must stay unknown (None), never become "Direct".
        self.pages = [fixture("blr_kul_nonstop"), fixture("blr_kul_2stop")]
        fare = scrape.scrape_route(BLR, KUL, "2026-11-15", "2026-11-18")
        rec = scrape.to_fare(BLR, KUL, "2026-11-15", "2026-11-18", fare, "2026-10-05T00:00:00Z")
        self.assertIsNone(rec["inbound"]["stops"])
        self.assertEqual(rec["nights"], 3)
        self.assertEqual(rec["currency"], "INR")
        self.assertEqual(rec["price"], 29828)


class DamagedResponses(Base):
    def test_unpriced_itinerary_does_not_cost_the_rest(self):
        # The 18 Sep failure: one itinerary with an empty price block made the
        # library discard the whole page.
        def unprice_first(p):
            p[3][0][0][1][0] = []
        html = edit_payload(fixture("blr_kul_nonstop"), unprice_first)
        self.pages = [html]
        got = scrape._search(BLR, KUL, "2026-11-15", "2026-11-18", 0)
        self.assertEqual(sorted(r.price for r in got), [30992, 38887, 38887])
        self.assertEqual(self.fetches, 1)  # recovered from the same page, no refetch
        self.assertEqual(scrape.STATS["failed"], 0)

    def test_implausible_price_is_dropped(self):
        def cheapen(p):
            p[3][0][0][1][0][1] = 99  # a currency or parse slip, not a fare
        self.pages = [edit_payload(fixture("blr_kul_nonstop"), cheapen)]
        got = scrape._search(BLR, KUL, "2026-11-15", "2026-11-18", 0)
        self.assertNotIn(99, [r.price for r in got])
        self.assertEqual(scrape.STATS["rejected_prices"], 1)

    def test_captcha_page_is_a_failure_not_an_empty_route(self):
        self.pages = [CAPTCHA, CAPTCHA]
        self.assertEqual(scrape._search(BLR, KUL, "2026-11-15", "2026-11-18", 0), [])
        self.assertEqual(self.fetches, 2)  # one retry
        self.assertEqual(scrape.STATS["failed"], 1)
        self.assertEqual(scrape.STATS["unreadable"], 1)
        self.assertEqual(scrape.BREAKER["streak"], 2)

    def test_captcha_then_good_page_recovers(self):
        self.pages = [CAPTCHA, fixture("blr_kul_nonstop")]
        got = scrape._search(BLR, KUL, "2026-11-15", "2026-11-18", 0)
        self.assertEqual(min(r.price for r in got), 29828)
        self.assertEqual(scrape.STATS["failed"], 0)
        self.assertEqual(scrape.BREAKER["streak"], 0)


class OriginGuard(Base):
    def run_origin(self, pages, n_dests=4):
        self.pages = list(pages)
        dests = [scrape.Place(f"d{i}", f"D{i:02d}", f"D{i}") for i in range(n_dests)]
        report = {"origins": {}}
        args = argparse.Namespace(limit=0, dry_run=False)
        with mock.patch.object(scrape, "post_fares", return_value=True) as post, \
             mock.patch.object(scrape, "sample_dates", return_value=[("2026-11-15", "2026-11-18")]):
            try:
                scrape._run_origins([BLR], dests, [("2026-11-15", "2026-11-18")], args, report)
            except scrape.Blocked:
                report["blocked"] = True
        return post, report

    def test_healthy_origin_is_posted(self):
        good = [fixture("blr_kul_nonstop"), fixture("blr_kul_2stop")] * 4
        post, report = self.run_origin(good)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(report["origins"]["bangalore"]["stored"])

    def test_half_blocked_origin_is_withheld(self):
        # Dests 0-1 fine; 2-3 hit a wall on both queries (2 fetches each, with
        # the retry). Never 8 in a row, so the breaker does not see it.
        good = [fixture("blr_kul_nonstop"), fixture("blr_kul_2stop")] * 2
        post, report = self.run_origin(good + [CAPTCHA] * 8)
        post.assert_not_called()
        self.assertTrue(report["origins"]["bangalore"]["withheld"])

    def test_solid_wall_stops_the_pass_without_posting(self):
        post, report = self.run_origin([CAPTCHA] * 200, n_dests=12)
        post.assert_not_called()
        self.assertTrue(report.get("blocked"))


class PostRetry(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(scrape.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        s = mock.patch.object(scrape, "SECRET", "x")
        s.start()
        self.addCleanup(s.stop)

    @staticmethod
    def http_error(code):
        return urllib.error.HTTPError("u", code, "m", {}, io.BytesIO(b"no"))

    @staticmethod
    def ok():
        resp = mock.MagicMock()
        resp.__enter__.return_value.status = 200
        return resp

    def test_5xx_then_success_is_stored(self):
        with mock.patch.object(scrape.urllib.request, "urlopen",
                               side_effect=[self.http_error(503), self.ok()]) as up:
            self.assertTrue(scrape.post_fares("bangalore", [{}], False))
        self.assertEqual(up.call_count, 2)

    def test_network_error_retried_until_attempts_run_out(self):
        with mock.patch.object(scrape.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("down")) as up:
            self.assertFalse(scrape.post_fares("bangalore", [{}], False))
        self.assertEqual(up.call_count, 1 + len(scrape.POST_RETRY_WAITS))

    def test_4xx_is_not_retried(self):
        # 409 is the backend refusing the payload; resending changes nothing.
        with mock.patch.object(scrape.urllib.request, "urlopen",
                               side_effect=self.http_error(409)) as up:
            self.assertFalse(scrape.post_fares("bangalore", [{}], False))
        self.assertEqual(up.call_count, 1)


if __name__ == "__main__":
    unittest.main()
