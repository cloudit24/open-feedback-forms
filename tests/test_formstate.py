import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import formstate  # noqa: E402

TODAY = date(2026, 6, 10)


def st(form, n=0):
    return formstate.form_state(form, n, today=TODAY)["state"]


class StateTests(unittest.TestCase):
    def test_plain_form_is_open(self):
        self.assertEqual(st({}), "open")

    def test_opening_date(self):
        self.assertEqual(st({"opens_on": "2026-06-11"}), "scheduled")
        self.assertEqual(st({"opens_on": "2026-06-10"}), "open")
        self.assertEqual(st({"opens_on": date(2026, 7, 1)}), "scheduled")

    def test_expiry_keeps_old_behaviour(self):
        self.assertEqual(st({"expiry_date": "2026-06-10"}), "closed_date")   # same day = closed
        self.assertEqual(st({"expiry_date": "2026-06-11"}), "open")

    def test_limit(self):
        self.assertEqual(st({"max_responses": 5}, 4), "open")
        self.assertEqual(st({"max_responses": 5}, 5), "closed_limit")
        self.assertEqual(st({"max_responses": 5}, 9), "closed_limit")
        self.assertEqual(st({"max_responses": None}, 999), "open")
        self.assertEqual(st({"max_responses": 0}, 999), "open")

    def test_scheduled_wins_then_date_then_limit(self):
        self.assertEqual(st({"opens_on": "2026-07-01", "max_responses": 1}, 5), "scheduled")
        self.assertEqual(st({"expiry_date": "2026-06-01", "max_responses": 1}, 5), "closed_date")


if __name__ == "__main__":
    unittest.main()
