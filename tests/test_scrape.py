"""Pure-function tests. No network: run with  ./.venv/bin/python -m unittest discover tests"""
import os
import sys
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scrape  # noqa: E402


def seg(frm, to, h=10, m=0):
    return NS(from_airport=frm, to_airport=to, departure=NS(time=(h, m)), arrival=NS(time=(h + 1, m)),
              duration=60, plane_type="A320")


class SplitLegs(unittest.TestCase):
    def test_direct_round_trip(self):
        out, back = scrape._split_legs([seg("BLR", "MAA"), seg("MAA", "BLR")], "BLR", "MAA")
        self.assertEqual(len(out), 1)
        self.assertEqual(len(back), 1)

    def test_connection_past_midnight_stays_in_outbound(self):
        # The Gulf Air bug: BLR-BAH-DXB must not be cut after the first hop.
        segs = [seg("BLR", "BAH"), seg("BAH", "DXB"), seg("DXB", "BLR")]
        out, back = scrape._split_legs(segs, "BLR", "DXB")
        self.assertEqual([s.to_airport for s in out], ["BAH", "DXB"])
        self.assertEqual(len(back), 1)

    def test_outbound_only_payload_has_no_inbound(self):
        # What Google actually sends: the return is picked in a second step.
        out, back = scrape._split_legs([seg("BLR", "MAA")], "BLR", "MAA")
        self.assertEqual(len(out), 1)
        self.assertEqual(back, [])

    def test_never_reaching_destination_returns_nothing(self):
        self.assertEqual(scrape._split_legs([seg("BLR", "BAH")], "BLR", "DXB"), ([], []))


class Describe(unittest.TestCase):
    def test_times_are_hh_mm_strings(self):
        d = scrape._describe([seg("BLR", "MAA", 9, 5)])[0]
        self.assertEqual(d["departTime"], "09:05")
        self.assertEqual(d["from"], "BLR")


class Dates(unittest.TestCase):
    def test_pairs_are_sorted_unique_and_in_the_future(self):
        pairs = scrape.sample_dates()
        self.assertEqual(pairs, sorted(set(pairs)))
        self.assertTrue(all(dep < ret for dep, ret in pairs))


class RouteStateTest(unittest.TestCase):
    def test_goes_dead_after_consecutive_misses(self):
        st = scrape.RouteState()
        self.assertFalse(st.dead)
        st.empty_streak = scrape.DEAD_AFTER
        self.assertTrue(st.dead)

    def walk(self, st, days, priced=False, nonstop=False):
        for day in days:
            for _ in range(3):  # 3, 5 and 7 nights from one departure
                st.next_departure(day)
                if st.dead:
                    return
                st.record(day, priced, nonstop, True)
        st.next_departure("9999-12-31")

    def test_one_empty_departure_day_is_not_a_dead_route(self):
        # The first three sorted pairs share a departure date. A route that does
        # not fly that one day must survive to the next.
        st = scrape.RouteState()
        self.walk(st, ["2026-10-15"])
        self.assertFalse(st.dead)
        self.assertFalse(st.nonstop_dead)

    def test_dead_after_n_empty_departure_days(self):
        st = scrape.RouteState()
        self.walk(st, [f"2026-1{i}-15" for i in range(scrape.DEAD_AFTER)])
        self.assertTrue(st.dead)

    def test_a_priced_day_resets_the_streak(self):
        st = scrape.RouteState()
        self.walk(st, ["2026-10-15", "2026-10-28"])
        self.walk(st, ["2026-11-15"], priced=True)
        self.assertEqual(st.empty_streak, 0)


class BreakerTest(unittest.TestCase):
    def setUp(self):
        scrape.BREAKER.update(streak=0, tripped=False, healthy=0)
        self._sleep = scrape.time.sleep
        scrape.time.sleep = lambda s: None

    def tearDown(self):
        scrape.time.sleep = self._sleep
        scrape.BREAKER.update(streak=0, tripped=False, healthy=0)

    def bad(self, n):
        for _ in range(n):
            scrape._breaker_bad()

    def test_first_wall_cools_down_second_stops(self):
        self.bad(scrape.BREAKER_PAUSE)
        scrape.breaker_check()  # cooldown, not an abort
        self.assertTrue(scrape.BREAKER["tripped"])
        self.bad(scrape.BREAKER_PAUSE)
        with self.assertRaises(scrape.Blocked):
            scrape.breaker_check()

    def test_a_healed_trip_does_not_abort_a_later_blip(self):
        # The old breaker never forgot a cooldown: one blip hours later in a
        # 15h pass aborted it.
        self.bad(scrape.BREAKER_PAUSE)
        scrape.breaker_check()
        for _ in range(scrape.BREAKER_RECOVER):
            scrape._breaker_ok()
        self.assertFalse(scrape.BREAKER["tripped"])
        self.bad(scrape.BREAKER_PAUSE)
        scrape.breaker_check()  # cools down again rather than raising

    def test_short_runs_do_nothing(self):
        self.bad(scrape.BREAKER_PAUSE - 1)
        scrape.breaker_check()
        self.assertFalse(scrape.BREAKER["tripped"])


class SearchTimeoutTest(unittest.TestCase):
    def setUp(self):
        scrape.BREAKER.update(streak=0, tripped=False, healthy=0)
        self._fetch, self._sleep = scrape._fetch_html, scrape.time.sleep
        scrape.time.sleep = lambda s: None
        self.calls = 0

        def hung(query, proxy):
            self.calls += 1
            raise scrape._HttpTimeout("timed out")

        scrape._fetch_html = hung

    def tearDown(self):
        scrape._fetch_html, scrape.time.sleep = self._fetch, self._sleep
        scrape.BREAKER.update(streak=0, tripped=False, healthy=0)

    def test_timeout_is_retried_once_then_skipped_and_counted(self):
        place = scrape.Place("bangalore", "BLR", "Bengaluru")
        dest = scrape.Place("kuala-lumpur", "KUL", "Kuala Lumpur")
        before = scrape.STATS["timeouts"]
        self.assertEqual(scrape._search(place, dest, "2026-11-15", "2026-11-18", 0), [])
        self.assertEqual(self.calls, 2)
        self.assertEqual(scrape.STATS["timeouts"] - before, 2)
        self.assertEqual(scrape.BREAKER["streak"], 2)


if __name__ == "__main__":
    unittest.main()
