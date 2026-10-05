import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import summary  # noqa: E402


def sel(key, values, style=None):
    return {"field_key": key, "field_type": "select", "display_style": style,
            "options": [{"value": str(v), "label_en": str(v), "label_ar": str(v)} for v in values]}


class KindTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(summary.kind_of(sel("a", range(11))), "nps")
        self.assertEqual(summary.kind_of(sel("nps", ["x"])), "nps")
        self.assertEqual(summary.kind_of({"field_key": "r", "field_type": "rating"}), "csat")
        self.assertEqual(summary.kind_of(sel("a", range(1, 6), "scale")), "csat")
        self.assertEqual(summary.kind_of(sel("a", range(1, 11), "scale")), "scale")
        self.assertEqual(summary.kind_of(sel("a", ["x", "y"], "buttons")), "choice")
        self.assertEqual(summary.kind_of({"field_key": "c", "field_type": "checkbox"}), "checkbox")
        self.assertEqual(summary.kind_of({"field_key": "t", "field_type": "textarea"}), "text")

    def test_grid_groups_join_neighbours_with_same_answers(self):
        a, b, c = sel("a", [1, 2, 3], "grid"), sel("b", [1, 2, 3], "grid"), sel("c", ["x", "y"], "grid")
        mid = sel("m", [1, 2], "buttons")
        self.assertEqual(summary.grid_groups([a, b, c, mid, a]), [["a", "b"], ["c"], ["a"]])


class MathTests(unittest.TestCase):
    def test_nps(self):
        counts = {"10": 3, "9": 2, "8": 1, "7": 1, "6": 2, "0": 1}
        r = summary.nps_from_counts(counts)
        self.assertEqual((r["promoters"], r["passives"], r["detractors"], r["n"]), (5, 2, 3, 10))
        self.assertEqual(r["score"], 20)             # 50% - 30%
        self.assertIsNone(summary.nps_from_counts({}))

    def test_csat_and_average(self):
        counts = {"5": 2, "4": 2, "3": 1, "1": 1}
        self.assertEqual(summary.csat_from_counts(counts)["pct"], 66.7)
        self.assertEqual(summary.average_from_counts(counts), 3.67)
        self.assertIsNone(summary.csat_from_counts({}))

    def test_build_question_uses_answered_as_the_base(self):
        q = summary.build_question(sel("k", ["a", "b"]), {"a": 1, "b": 3}, 4)
        self.assertEqual([b["pct"] for b in q["bars"]], [25.0, 75.0])
        self.assertEqual(q["answered"], 4)

    def test_checkbox_counts_mariadb_style_one_as_yes(self):
        q = summary.build_question({"field_key": "c", "field_type": "checkbox"}, {"1": 3, "0": 1}, 4)
        self.assertEqual([b["n"] for b in q["bars"]], [3, 1])

    def test_nps_bars_are_coloured(self):
        q = summary.build_question(sel("nps", range(11), "scale"), {"3": 1, "7": 1, "10": 1}, 3)
        tones = {b["value"]: b["tone"] for b in q["bars"]}
        self.assertEqual((tones["6"], tones["7"], tones["8"], tones["9"]), ("bad", "mid", "mid", "good"))
        self.assertEqual(q["nps"]["score"], 0)


if __name__ == "__main__":
    unittest.main()
