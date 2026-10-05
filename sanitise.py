"""
Cleans the styled text (form description) an admin types, so it is safe to
show on a public page. Standard library only.

Allow-list: <b> <strong> <i> <em> <u> <a href="http(s)://..."> <ul> <ol>
<li> <br> <p>. Every other tag is removed (its text stays, except <script>
and <style>, which are dropped with their contents). Every attribute is
removed except href on <a>, and only http:// or https:// links survive.
Links get rel="noopener" and target="_blank". Output is always balanced.
"""
import html
import re
from html.parser import HTMLParser

ALLOWED = {"b", "strong", "i", "em", "u", "a", "ul", "ol", "li", "br", "p"}
VOID = {"br"}
RENAME = {"div": "p"}                    # contenteditable makes <div> lines
DROP_WITH_CONTENT = {"script", "style", "template", "iframe", "object", "noscript", "textarea", "title"}
SAFE_HREF = re.compile(r"^https?://[^\s<>\"'`\x00-\x1f\x7f]+$", re.I)


class _Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []          # open allowed tags
        self.skip = 0            # depth inside a dropped-with-content tag
        self.skip_tag = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip:
            if tag == self.skip_tag:
                self.skip += 1
            return
        if tag in DROP_WITH_CONTENT:
            self.skip, self.skip_tag = 1, tag
            return
        tag = RENAME.get(tag, tag)
        if tag not in ALLOWED:
            return
        if tag in ("p", "ul", "ol") and "p" in self.stack:
            # A paragraph can't hold a list or another paragraph: close it first.
            while self.stack:
                top = self.stack.pop()
                self.out.append("</%s>" % top)
                if top == "p":
                    break
        if tag in VOID:
            self.out.append("<br>")
            return
        if tag == "a":
            href = ""
            for k, v in attrs:
                if k.lower() == "href" and v:
                    href = v.strip()
            if not SAFE_HREF.match(href):
                return                      # a link without a safe address: keep the text only
            self.out.append('<a href="%s" rel="noopener" target="_blank">' % html.escape(href, quote=True))
        else:
            self.out.append("<%s>" % tag)
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag in VOID:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip:
            if tag == self.skip_tag:
                self.skip -= 1
            return
        tag = RENAME.get(tag, tag)
        if tag not in ALLOWED or tag in VOID or tag not in self.stack:
            return
        while self.stack:                     # close anything left open inside it
            top = self.stack.pop()
            self.out.append("</%s>" % top)
            if top == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def close_all(self):
        while self.stack:
            self.out.append("</%s>" % self.stack.pop())


def sanitise(value):
    """Returns safe HTML for any input (None becomes an empty string)."""
    if not value:
        return ""
    value = str(value).replace("\x00", "")
    p = _Cleaner()
    p.feed(value)
    p.close()
    p.close_all()
    out = "".join(p.out).strip()
    out = re.sub(r"<p>\s*</p>", "", out)          # empty paragraphs left by the editor
    # An editor that was emptied leaves things like <p><br></p>: treat as empty.
    if not re.sub(r"<[^>]*>|&nbsp;|\s", "", out):
        return ""
    return out
