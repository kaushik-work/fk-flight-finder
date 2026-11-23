"""The two-step search, without a network: a fake searcher stands in for Google."""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scrape  # noqa: E402

BLR = scrape.Place("bangalore", "BLR", "Bengaluru", "India")
BKK = scrape.Place("bangkok", "BKK", "Bangkok", "Thailand")


def fare(price: int) -> dict:
    return {"price": price}


class CoarseDates(unittest.TestCase):
    def test_spread_through_each_month_in_the_future_and_sorted(self):
        d = scrape.coarse_dates(date(2026, 11, 22))
        self.assertEqual(d, sorted(set(d)))
        self.assertIn("2026-12-02", d)  # the near-term date, ten days out
        for m in ("2026-12", "2027-01", "2027-02", "2027-03"):
            self.assertGreaterEqual(sum(1 for x in d if x.startswith(m)), 4)

    def test_each_month_spans_several_weekdays_not_one(self):
        # Dates seven days apart share a weekday, which would hide that midweek fares are cheaper.
        d = scrape.coarse_dates(date(2026, 11, 22))
        for m in ("2026-12", "2027-01", "2027-02", "2027-03"):
            weekdays = {date.fromisoformat(x).weekday() for x in d if x.startswith(m)}
            self.assertGreaterEqual(len(weekdays), 4, m)

    def test_the_current_month_is_not_left_empty(self):
        d = scrape.coarse_dates(date(2026, 11, 22))
        self.assertTrue(any(x.startswith("2026-11") for x in d))  # the date four days out

    def test_nothing_inside_three_days(self):
        today = date(2026, 11, 22)
        for x in scrape.coarse_dates(today):
            self.assertGreater(date.fromisoformat(x), today + scrape.timedelta(days=3))


class TwoSteps(unittest.TestCase):
    def run_route(self, prices, dates):
        asked = []

        def search(o, d, dep, ret, state):
            asked.append((dep, ret))
            p = prices(dep, ret)
            # the real scrape_route records every answer on the route's state; the fake has to do the same
            state.record(dep, priced=bool(p), nonstop=False, nonstop_asked=False, answered=True)
            return fare(p) if p else None

        found = scrape.scrape_destination(BLR, BKK, dates, search=search, pause=lambda: None)
        return found, asked

    def test_other_lengths_are_priced_only_on_each_months_cheapest_date(self):
        dates = ["2026-12-04", "2026-12-11", "2026-12-18", "2027-01-04", "2027-01-11"]
        cheap = {"2026-12-11": 20000, "2027-01-04": 21000}
        found, asked = self.run_route(lambda dep, ret: cheap.get(dep, 30000), dates)
        primary = {d for d, r in asked if (date.fromisoformat(r) - date.fromisoformat(d)).days == scrape.PRIMARY_NIGHTS}
        self.assertEqual(primary, set(dates))
        fine_dates = {d for d, r in asked if (date.fromisoformat(r) - date.fromisoformat(d)).days != scrape.PRIMARY_NIGHTS}
        self.assertEqual(fine_dates, {"2026-12-11", "2027-01-04"})
        others = len(scrape.NIGHTS_LIST) - 1
        self.assertEqual(len(asked), len(dates) + 2 * others)
        # every trip length exists on the best date of each month
        for best in fine_dates:
            lengths = {(date.fromisoformat(r) - date.fromisoformat(d)).days for d, r, _ in found if d == best}
            self.assertEqual(lengths, set(scrape.NIGHTS_LIST))

    def test_a_route_with_no_flights_stops_early_and_skips_step_two(self):
        dates = [f"2027-0{m}-{d:02d}" for m in (1, 2, 3) for d in (4, 11, 18, 25)]
        found, asked = self.run_route(lambda dep, ret: None, dates)
        self.assertEqual(found, [])
        self.assertLessEqual(len(asked), scrape.DEAD_AFTER + 2)

    def test_a_missing_length_on_the_best_day_does_not_kill_the_route(self):
        dates = ["2026-12-04", "2026-12-11"]
        found, asked = self.run_route(
            lambda dep, ret: 20000 if (date.fromisoformat(ret) - date.fromisoformat(dep)).days == scrape.PRIMARY_NIGHTS else None,
            dates,
        )
        self.assertEqual(len(found), 2)
        self.assertEqual(len(asked), 2 + len(scrape.NIGHTS_LIST) - 1)

    def test_request_estimate_is_an_upper_bound(self):
        d = scrape.coarse_dates(date(2026, 11, 22))
        months = len({x[:7] for x in d})
        self.assertEqual(scrape.estimate_requests(10, d), 10 * (len(d) + months * (len(scrape.NIGHTS_LIST) - 1)))


class SecondChance(unittest.TestCase):
    def test_a_route_written_off_is_asked_again_once_and_the_better_answer_is_kept(self):
        from unittest import mock
        import argparse
        calls = []

        def fake(origin, dest, dates, state=None, **kw):
            calls.append(dest.code)
            first = calls.count(dest.code) == 1
            if dest.code == "DXB" and first:
                state.empty_streak = scrape.DEAD_AFTER  # looks dead: Google answered "no flights" three days running
                return []
            return [("2026-12-11", "2026-12-16", {"price": 20000, "airline": "x", "stops": 0, "segments": []})] * (3 if dest.code == "DXB" else 2)

        posted = {}
        with mock.patch.object(scrape, "scrape_destination", fake), \
             mock.patch.object(scrape, "google_is_answering", lambda *a, **k: True), \
             mock.patch.object(scrape, "to_fare", lambda o, d, dep, ret, f, t: {"dest": d.code}), \
             mock.patch.object(scrape, "post_fares", lambda slug, fares, dry: posted.setdefault("fares", fares) is not None):
            args = argparse.Namespace(limit=0, dry_run=True)
            scrape._run_origins([BLR], [BKK, scrape.Place("dubai", "DXB", "Dubai", "UAE")], [("2026-12-04", "2026-12-09")], args, {"origins": {}})
        self.assertEqual(calls.count("DXB"), 2)  # asked, written off, asked again
        self.assertEqual(calls.count("BKK"), 1)  # a healthy route is not asked twice
        self.assertEqual(sum(1 for f in posted["fares"] if f["dest"] == "DXB"), 3)


class EmptySpells(unittest.TestCase):
    def run_city(self, canary_answers):
        """A city of one route that looks dead, with the canary answering from `canary_answers` (True = prices)."""
        from unittest import mock
        import argparse
        slept, scraped = [], []
        answers = iter(canary_answers)

        def fake_destination(origin, dest, dates, state=None, **kw):
            scraped.append(dest.code)
            if len(scraped) == 1:
                state.empty_streak = scrape.DEAD_AFTER  # written off the first time
                return []
            return [("2026-12-11", "2026-12-16", {"price": 20000})]

        def fake_canary(origin, dests, dates, **kw):
            return next(answers)

        posted = {}
        dubai = scrape.Place("dubai", "DXB", "Dubai", "UAE")
        with mock.patch.object(scrape, "scrape_destination", fake_destination), \
             mock.patch.object(scrape, "google_is_answering", fake_canary), \
             mock.patch.object(scrape.time, "sleep", lambda s: slept.append(s)), \
             mock.patch.object(scrape, "to_fare", lambda o, d, dep, ret, f, t: {"dest": d.code}), \
             mock.patch.object(scrape, "post_fares", lambda slug, fares, dry: posted.setdefault("fares", fares) is not None):
            scrape._run_origins([BLR], [BKK], [("2026-12-04", "2026-12-09")], argparse.Namespace(limit=0, dry_run=True), {"origins": {}})
        return slept, scraped, posted

    def test_waits_while_the_canary_is_empty_then_asks_the_route_again(self):
        slept, scraped, posted = self.run_city([False, False, True])
        self.assertEqual(slept, [scrape.WAVE_PAUSE, scrape.WAVE_PAUSE])  # two empty checks, two waits
        self.assertEqual(scraped, ["BKK", "BKK"])  # judged again from scratch after the spell
        self.assertEqual(len(posted["fares"]), 1)

    def test_a_healthy_canary_means_the_route_really_is_dead(self):
        slept, scraped, posted = self.run_city([True])
        self.assertEqual(slept, [])  # no waiting
        self.assertIn("BKK", scraped)

    def test_canary_needs_two_empties_in_a_row_to_call_it_a_spell(self):
        calls = []

        def search(o, d, dep, ret, state):
            calls.append(dep)
            return {"price": 1} if len(calls) == 2 else None  # empty once, then prices

        dests = [scrape.Place("dubai", "DXB", "Dubai", "UAE"), BKK]
        self.assertTrue(scrape.google_is_answering(BLR, dests, ["2026-12-04", "2026-12-11", "2026-12-18"], search=search, pause=lambda: None))
        self.assertEqual(len(calls), 2)


class BaggageLinks(unittest.TestCase):
    @unittest.skipUnless(__import__("importlib.util").util.find_spec("selectolax"), "selectolax is only installed on the droplets")
    def test_links_are_read_from_a_real_google_page(self):
        from test_pipeline import fixture
        its = scrape._parse_page(fixture("blr_kul_nonstop"))
        self.assertTrue(its)
        links = its[0].baggage_links
        self.assertEqual(links.get("IndiGo"), "https://www.goindigo.in/baggage/baggage-allowance.html")
        self.assertIn("AirAsia", links)

    def test_a_payload_without_the_block_gives_no_links(self):
        self.assertEqual(scrape._baggage_links([None] * 5), {})
        self.assertEqual(scrape._baggage_links(None), {})


if __name__ == "__main__":
    unittest.main()
