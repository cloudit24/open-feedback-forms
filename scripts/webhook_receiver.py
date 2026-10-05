"""A tiny test receiver for webhooks. NOT part of the app.

    python scripts/webhook_receiver.py 19000 my-secret [fail]

Prints each POST it gets and says whether the X-OFF-Signature matches the
secret you gave it. Add "fail" as the last word to answer 500 every time (to
see the retries in the admin log)."""
import hashlib
import hmac
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 19000
SECRET = sys.argv[2] if len(sys.argv) > 2 else ""
FAIL = "fail" in sys.argv[3:]


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        want = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        got = self.headers.get("X-OFF-Signature", "")
        ok = hmac.compare_digest(want, got)
        print("POST %s event=%s signature=%s" % (self.path, self.headers.get("X-OFF-Event"), "OK" if ok else "BAD"), flush=True)
        print(body.decode("utf-8", "replace"), flush=True)
        self.send_response(500 if FAIL else 200)
        self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
