import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


class DisplayStyleTests(unittest.TestCase):
    def test_valid_styles_per_type(self):
        self.assertEqual(server.clean_display_style("rating", "faces"), "faces")
        self.assertEqual(server.clean_display_style("select", "grid"), "grid")
        self.assertEqual(server.clean_display_style("select", "scale"), "scale")

    def test_wrong_type_or_junk_means_original_look(self):
        self.assertIsNone(server.clean_display_style("select", "faces"))
        self.assertIsNone(server.clean_display_style("text", "buttons"))
        self.assertIsNone(server.clean_display_style("rating", "<script>"))
        self.assertIsNone(server.clean_display_style("rating", None))
        self.assertIsNone(server.clean_display_style("rating", ""))

    def test_answers_validate_the_same_way(self):
        sel = {"field_key": "nps", "field_type": "select", "required": True,
               "options": [{"value": str(n)} for n in range(11)], "display_style": "scale"}
        self.assertEqual(server.validate_dynamic_value(sel, "9"), ("9", None))
        self.assertIsNotNone(server.validate_dynamic_value(sel, "11")[1])
        rat = {"field_key": "r", "field_type": "rating", "required": True, "display_style": "faces"}
        self.assertEqual(server.validate_dynamic_value(rat, 4), (4, None))
        self.assertIsNotNone(server.validate_dynamic_value(rat, 6)[1])


if __name__ == "__main__":
    unittest.main()
