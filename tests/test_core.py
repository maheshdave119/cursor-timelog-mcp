import unittest
from datetime import date, datetime

from cursor_timelog import server as s


class IntervalTests(unittest.TestCase):
    def test_merge_respects_gap(self):
        merged = s.merge_intervals([(0, 60), (100, 200), (4000, 4100)], gap=300)
        self.assertEqual(merged, [(0, 200), (4000, 4100)])

    def test_subtract_removes_overlap(self):
        self.assertEqual(s.subtract_intervals([(0, 100)], [(20, 40)]), [(0, 20), (40, 100)])
        self.assertEqual(s.subtract_intervals([(10, 20)], [(0, 100)]), [])

    def test_round_minutes_has_floor_and_steps(self):
        self.assertEqual(s.round_minutes(20), 5)
        self.assertEqual(s.round_minutes(38 * 60), 40)
        self.assertEqual(s.round_minutes(0, 0), 0)


class ParsingTests(unittest.TestCase):
    def test_parse_date_iso_and_words(self):
        self.assertEqual(s.parse_date("2026-11-03"), date(2026, 11, 3))
        today = datetime.now(s.LOCAL_TZ).date()
        self.assertEqual(s.parse_date("today"), today)
        self.assertEqual((today - s.parse_date("yesterday")).days, 1)
        self.assertEqual(s.parse_date("Nov 3").month, 11)

    def test_meta_turn_detection(self):
        for text in ("timlog", "toda's timelog", "timelog all project for today", "timlogwith hours"):
            self.assertTrue(s.is_meta_turn(text), text)
        self.assertFalse(s.is_meta_turn("Fix the checkout exception when placing an order"))

    def test_parse_hours_and_clock(self):
        self.assertAlmostEqual(s.parse_hours("1h 30m"), 1.5)
        self.assertAlmostEqual(s.parse_hours("2.25"), 2.25)
        day = date(2026, 11, 5)
        self.assertEqual(datetime.fromtimestamp(s.parse_clock("2:30 PM", day), s.LOCAL_TZ).hour, 14)

    def test_timestamps(self):
        self.assertAlmostEqual(s.parse_iso("2026-11-05T06:26:40.355Z"), 1793860000.355, delta=100000)
        self.assertEqual(s.parse_iso(1793860000000), 1793860000.0)

    def test_transcript_timestamp(self):
        ts = s.parse_transcript_timestamp("<timestamp>Thursday, Nov 5, 2026, 11:29 AM (UTC+5:30)</timestamp>")
        self.assertEqual(datetime.fromtimestamp(ts, s.LOCAL_TZ).strftime("%H:%M"), "11:29")


class CategoryTests(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(s.categorize(["Research options for payment"], [], {}), "Research & Analysis")
        self.assertEqual(s.categorize(["fix exception"], ["a/b.php"], {}), "Debugging & Fixes")
        self.assertEqual(s.categorize(["add button"], ["a/b.php"], {}), "Development")
        self.assertEqual(s.categorize(["prepare list"], ["docs/x.md"], {}), "Documentation")


if __name__ == "__main__":
    unittest.main()
