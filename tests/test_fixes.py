"""27 Nov 2026 fixes: failed requests are not "no flights", the fare id ignores price, weekdays rotate."""
from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

import scrape

BLR = scrape.Place("bangalore", "BLR", "Bengaluru", "India")
DPS = scrape.Place("bali", "DPS", "Bali", "Indonesia")


class FailuresAreNotEmpty(unittest.TestCase):
    """A timeout, fetch error or CAPTCHA that survives the retry raises SearchFailed, never returns "no flights" ([])."""

    def run_search(self, fetch, parse=None):
        with mock.patch.object(scrape, "build_query", return_value=object()), \
             mock.patch.object(scrape, "_fetch_html", side_effect=fetch), \
             mock.patch.object(scrape.time, "sleep"), \
             mock.patch.object(scrape, "_proxy_for", return_value=None):
            if parse is not None:
                with mock.patch.object(scrape, "_parse_page", side_effect=parse):
                    return scrape._search(BLR, DPS, "2027-01-12", "2027-01-17", 2)
            return scrape._search(BLR, DPS, "2027-01-12", "2027-01-17", 2)

    def test_timeout_twice(self):
        with self.assertRaises(scrape.SearchFailed):
            self.run_search(scrape._HttpTimeout())

    def test_fetch_error_twice(self):
        with self.assertRaises(scrape.SearchFailed):
            self.run_search(ConnectionError("reset"))

    def test_captcha_twice(self):
        with mock.patch.object(scrape, "_is_captcha", return_value=True):
            with self.assertRaises(scrape.SearchFailed):
                self.run_search(lambda *a: "<html>captcha</html>")

    def test_unreadable_page_twice(self):
        with mock.patch.object(scrape, "_is_captcha", return_value=False):
            with self.assertRaises(scrape.SearchFailed):
                self.run_search(lambda *a: "<html></html>", parse=ValueError("no data block"))

    def test_a_real_empty_answer_is_still_an_empty_list(self):
        with mock.patch.object(scrape, "_is_captcha", return_value=False):
            self.assertEqual(self.run_search(lambda *a: "<html></html>", parse=lambda html: []), [])

    def test_failures_do_not_write_a_route_off(self):
        # Three departure dates in a row that failed to answer must not mark the route dead.
        state = scrape.RouteState()
        with mock.patch.object(scrape, "_search", side_effect=scrape.SearchFailed), mock.patch.object(scrape.time, "sleep"):
            for dep in ("2027-01-05", "2027-01-12", "2027-01-19", "2027-01-26"):
                state.next_departure(dep)
                self.assertIsNone(scrape.scrape_route(BLR, DPS, dep, scrape._return_date(dep, 5), state))
        self.assertFalse(state.dead)

    def test_failures_between_real_empty_days_do_not_extend_the_streak(self):
        # empty, failed, empty, failed: two real empty days only, so the route is alive.
        state = scrape.RouteState()
        empty_days = {"2027-01-05", "2027-01-19"}

        def fake(origin, dest, dep, ret, cap):
            if dep in empty_days:
                return []
            raise scrape.SearchFailed
        with mock.patch.object(scrape, "_search", side_effect=fake), mock.patch.object(scrape.time, "sleep"):
            for dep in ("2027-01-05", "2027-01-12", "2027-01-19", "2027-01-26"):
                state.next_departure(dep)
                scrape.scrape_route(BLR, DPS, dep, scrape._return_date(dep, 5), state)
            state.next_departure("2027-02-02")
        self.assertFalse(state.dead)

    def test_real_empty_answers_still_write_a_route_off(self):
        state = scrape.RouteState()
        with mock.patch.object(scrape, "_search", return_value=[]), mock.patch.object(scrape.time, "sleep"):
            for dep in ("2027-01-05", "2027-01-12", "2027-01-19", "2027-01-26"):
                state.next_departure(dep)
                scrape.scrape_route(BLR, DPS, dep, scrape._return_date(dep, 5), state)
        self.assertTrue(state.dead)


class EmptyRetries(unittest.TestCase):
    """An empty page is a coin toss on a route with fares, so a route that has priced gets more asks than one that has not."""

    def route_calls(self, ever_priced, answers):
        state = scrape.RouteState()
        state.ever_priced = ever_priced
        calls = []
        seq = iter(answers)

        def fake(o, d, dep, ret, cap):
            calls.append(cap)
            return next(seq)
        price = mock.Mock(price=40000, flights=[], airlines=["IndiGo"], baggage_links={})
        with mock.patch.object(scrape, "_search", side_effect=fake), mock.patch.object(scrape.time, "sleep"):
            with mock.patch.object(scrape, "_split_legs", return_value=([], [])):
                out = scrape.scrape_route(BLR, DPS, "2027-01-12", "2027-01-17", state)
        return out, len(calls)

    def test_a_route_that_priced_is_asked_up_to_three_more_times(self):
        out, n = self.route_calls(True, [[], [], [], [mock.Mock(price=40000, flights=[], airlines=["IndiGo"], baggage_links={})]])
        self.assertEqual(n, 4)
        self.assertIsNotNone(out)

    def test_it_stops_asking_once_priced(self):
        out, n = self.route_calls(True, [[], [mock.Mock(price=40000, flights=[], airlines=["IndiGo"], baggage_links={})]])
        self.assertEqual(n, 2)

    def test_a_route_that_never_priced_gets_one_retry(self):
        out, n = self.route_calls(False, [[], [], [], []])
        self.assertEqual(n, 2)
        self.assertIsNone(out)


class FareId(unittest.TestCase):
    def fare(self, price):
        f = {"price": price, "outStops": 0, "inStops": 0, "outDuration": 400, "inDuration": 400, "airline": "IndiGo"}
        return scrape.to_fare(BLR, DPS, "2027-01-12", "2027-01-17", f, "2026-11-27T00:00:00Z")

    def test_id_survives_a_price_change(self):
        self.assertEqual(self.fare(40000)["id"], self.fare(43000)["id"])

    def test_id_differs_by_trip(self):
        other = scrape.to_fare(BLR, DPS, "2027-01-13", "2027-01-18", {"price": 40000, "outStops": 0, "inStops": 0, "outDuration": 1, "inDuration": 1, "airline": "x"}, "t")
        self.assertNotEqual(self.fare(40000)["id"], other["id"])


class WeekdayRotation(unittest.TestCase):
    def test_four_different_weekdays_each_day(self):
        for n in range(14):
            d = date(2026, 11, 20 + n // 2) if False else date.fromordinal(date(2026, 11, 20).toordinal() + n)
            self.assertEqual(len(set(scrape.coarse_weekdays(d))), len(scrape.COARSE_WEEKDAYS))

    def test_every_weekday_is_sampled_across_a_week(self):
        seen = set()
        for n in range(7):
            seen.update(scrape.coarse_weekdays(date.fromordinal(date(2026, 11, 20).toordinal() + n)))
        self.assertEqual(seen, set(range(7)))

    def test_same_day_same_dates(self):
        self.assertEqual(scrape.coarse_dates(date(2026, 11, 20)), scrape.coarse_dates(date(2026, 11, 20)))


if __name__ == "__main__":
    unittest.main()
