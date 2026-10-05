import hashlib
import hmac
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webhooks  # noqa: E402


class UrlTests(unittest.TestCase):
    def ok(self, url):
        return webhooks.check_url(url)[0] is None

    def test_https_only_except_localhost(self):
        self.assertTrue(self.ok("https://example.com/hook"))
        self.assertFalse(self.ok("http://example.com/hook"))
        self.assertTrue(self.ok("http://localhost:9000/x"))
        self.assertTrue(self.ok("http://127.0.0.1:9000/x"))
        self.assertTrue(self.ok("http://[::1]:9000/x"))

    def test_rejects_junk(self):
        for u in ("", "ftp://example.com", "javascript:alert(1)", "https://", "https://u:p@example.com/",
                  "https://" + "a" * 600 + ".com", "file:///etc/passwd"):
            self.assertFalse(self.ok(u), u)


class SsrfTests(unittest.TestCase):
    def blocked(self, host):
        try:
            webhooks.resolve(host, 443, False)
            return False
        except webhooks.WebhookError:
            return True

    def test_internal_addresses_blocked_by_default(self):
        for h in ("127.0.0.1", "10.1.2.3", "192.168.0.5", "172.16.0.1", "169.254.169.254",
                  "0.0.0.0", "::1", "fd00::1", "fe80::1", "::ffff:127.0.0.1", "localhost"):
            self.assertTrue(self.blocked(h), h)

    def test_allowed_when_ticked(self):
        self.assertEqual(webhooks.resolve("127.0.0.1", 80, True), "127.0.0.1")

    def test_public_address_passes(self):
        self.assertEqual(webhooks.resolve("93.184.216.34", 443, False), "93.184.216.34")


class Receiver(BaseHTTPRequestHandler):
    seen = []
    code = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Receiver.seen.append((self.headers, body, self.path))
        self.send_response(Receiver.code)
        self.end_headers()

    def log_message(self, *a):
        pass


class SendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Receiver)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Receiver.seen = []
        Receiver.code = 200

    def test_signature_verifies_and_body_matches(self):
        form = {"id": 3, "name": "F", "slug": "f"}
        body = webhooks.build_body(form, 7, "2026-01-01T00:00:00Z", {"q": "a"}, {"branch": "dubai"})
        code, err = webhooks.send("http://127.0.0.1:%d/hook?x=1" % self.port, "s3cret", body, "submission", True)
        self.assertEqual((code, err), (200, None))
        headers, got, path = Receiver.seen[0]
        self.assertEqual(path, "/hook?x=1")
        self.assertEqual(got, body)
        self.assertEqual(headers["X-OFF-Signature"], hmac.new(b"s3cret", got, hashlib.sha256).hexdigest())
        doc = json.loads(got)
        self.assertEqual(doc["form"], form)
        self.assertEqual(doc["submission"]["answers"], {"q": "a"})
        self.assertEqual(doc["submission"]["hidden"], {"branch": "dubai"})

    def test_private_address_blocked_unless_allowed(self):
        code, err = webhooks.send("http://127.0.0.1:%d/" % self.port, "x", b"{}", "submission", False)
        self.assertIsNone(code)
        self.assertIn("blocked", err)
        self.assertEqual(Receiver.seen, [])

    def test_error_status_is_reported(self):
        Receiver.code = 500
        code, err = webhooks.send("http://127.0.0.1:%d/" % self.port, "x", b"{}", "submission", True)
        self.assertEqual(code, 500)
        self.assertIn("500", err)

    def test_nothing_listening(self):
        code, err = webhooks.send("http://127.0.0.1:1/", "x", b"{}", "submission", True)
        self.assertIsNone(code)
        self.assertTrue(err)

    def test_secret_never_in_error_text(self):
        code, err = webhooks.send("http://127.0.0.1:1/", "TOPSECRET", b"{}", "submission", True)
        self.assertNotIn("TOPSECRET", err or "")


if __name__ == "__main__":
    unittest.main()
