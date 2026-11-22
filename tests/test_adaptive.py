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


if __name__ == "__main__":
    unittest.main()
