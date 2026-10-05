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


if __name__ == "__main__":
    unittest.main()
