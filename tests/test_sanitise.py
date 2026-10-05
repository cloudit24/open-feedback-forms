import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sanitise import sanitise  # noqa: E402


class SanitiseTests(unittest.TestCase):
    def test_keeps_allowed_formatting(self):
        s = "<p>Hi <b>there</b> <i>you</i> <u>all</u><br>ok</p><ul><li>a</li><li>b</li></ul>"
        self.assertEqual(sanitise(s), s)

    def test_strong_em_ol(self):
        s = "<strong>x</strong><em>y</em><ol><li>1</li></ol>"
        self.assertEqual(sanitise(s), s)

    def test_script_removed_with_content(self):
        out = sanitise("a<script>alert(1)</script>b")
        self.assertEqual(out, "ab")

    def test_style_and_iframe_removed_with_content(self):
        self.assertEqual(sanitise("<style>*{x:y}</style>t<iframe src=//x>zz</iframe>"), "t")

    def test_onerror_img_stripped(self):
        out = sanitise("<img src=x onerror=alert(1)>hello")
        self.assertEqual(out, "hello")
        self.assertNotIn("onerror", out)

    def test_attributes_stripped(self):
        out = sanitise('<b style="color:red" onclick="x()" class="y">t</b>')
        self.assertEqual(out, "<b>t</b>")

    def test_javascript_link_removed(self):
        out = sanitise('<a href="javascript:alert(1)">click</a>')
        self.assertEqual(out, "click")
        self.assertNotIn("javascript", out)

    def test_obfuscated_javascript_link(self):
        for bad in ("JaVa\tScRiPt:alert(1)", " javascript:alert(1)", "data:text/html,x",
                    "vbscript:x", "//evil.com", "/relative"):
            out = sanitise('<a href="%s">t</a>' % bad)
            self.assertEqual(out, "t", bad)

    def test_https_link_gets_rel_and_target(self):
        out = sanitise('<a href="https://example.com/a?b=1&c=2" onclick="x">go</a>')
        self.assertEqual(out, '<a href="https://example.com/a?b=1&amp;c=2" rel="noopener" target="_blank">go</a>')

    def test_link_quote_breakout(self):
        out = sanitise('<a href="https://x.com/&quot; onmouseover=&quot;alert(1)">t</a>')
        self.assertNotIn('" onmouseover', out)

    def test_nested_junk(self):
        out = sanitise("<div><span><b>x</b><script><b>y</b></script></span></div>")
        self.assertEqual(out, "<p><b>x</b></p>")

    def test_unbalanced_is_closed(self):
        self.assertEqual(sanitise("<b>one <i>two"), "<b>one <i>two</i></b>")

    def test_stray_close_ignored(self):
        self.assertEqual(sanitise("x</b></p>y"), "xy")

    def test_text_is_escaped(self):
        self.assertEqual(sanitise("1 < 2 & 3 > 2"), "1 &lt; 2 &amp; 3 &gt; 2")
        self.assertEqual(sanitise("&lt;script&gt;alert(1)&lt;/script&gt;"), "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_arabic_kept(self):
        self.assertEqual(sanitise("<p>مرحبا <b>بكم</b></p>"), "<p>مرحبا <b>بكم</b></p>")

    def test_empty_editor_is_empty(self):
        self.assertEqual(sanitise("<p><br></p>"), "")
        self.assertEqual(sanitise(None), "")
        self.assertEqual(sanitise("   "), "")

    def test_idempotent(self):
        s = '<p>a <a href="https://x.com">l</a> &amp; <b>b</b></p>'
        self.assertEqual(sanitise(sanitise(s)), sanitise(s))

    def test_list_inside_paragraph_is_split(self):
        self.assertEqual(sanitise("<p>a<ul><li>b</li></ul></p>"), "<p>a</p><ul><li>b</li></ul>")
        self.assertEqual(sanitise("<p>a<p>b"), "<p>a</p><p>b</p>")

    def test_empty_paragraphs_dropped(self):
        self.assertEqual(sanitise("<p></p><p>a</p><p> </p>"), "<p>a</p>")

    def test_svg_math_and_comments(self):
        out = sanitise("<svg onload=alert(1)><circle/></svg><!-- c --><math><mi>x</mi></math>t")
        self.assertNotIn("<", out.replace("&lt;", ""))
        self.assertTrue(out.endswith("t"))


if __name__ == "__main__":
    unittest.main()
