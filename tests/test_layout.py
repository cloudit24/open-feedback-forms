import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


class LayoutSettingsTests(unittest.TestCase):
    def test_old_forms_stay_single_page(self):
        # a form row from before 1.10.0 has no layout columns at all
        look = server.form_look({"name": "Old"})
        self.assertEqual(look["layoutMode"], "single_page")
        self.assertFalse(look["welcomeEnabled"])
        self.assertEqual(look["thanksUrl"], "")

    def test_unknown_layout_falls_back(self):
        self.assertEqual(server.form_look({"layout_mode": "weird"})["layoutMode"], "single_page")
        self.assertEqual(server.form_look({"layout_mode": "steps"})["layoutMode"], "steps")
        self.assertEqual(server.form_look({"layout_mode": "one_at_a_time"})["layoutMode"], "one_at_a_time")

    def test_thanks_link_must_be_http(self):
        self.assertEqual(server.form_look({"thanks_url": "https://example.com/a?b=1"})["thanksUrl"],
                         "https://example.com/a?b=1")
        for bad in ("javascript:alert(1)", "ftp://x.y", "//evil.com", "https://a b.com", 'https://x.com/"onclick='):
            self.assertEqual(server.form_look({"thanks_url": bad})["thanksUrl"], "", bad)

    def test_screen_text_is_cleaned_on_the_way_out(self):
        look = server.form_look({"welcome_text_en": '<b>Hi</b><img src=x onerror=alert(1)><script>x()</script>',
                                 "thanks_text_ar": '<a href="javascript:alert(1)">x</a>'})
        self.assertIn("<b>Hi</b>", look["welcomeTextEn"])
        self.assertNotIn("onerror", look["welcomeTextEn"])
        self.assertNotIn("<script", look["welcomeTextEn"])
        self.assertNotIn("javascript:", look["thanksTextAr"])


if __name__ == "__main__":
    unittest.main()
