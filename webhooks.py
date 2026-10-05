"""Webhooks: send each new response to another system.

Standard library only. Three jobs live here:
  - checking a webhook address (https only; http only for localhost),
  - refusing private / internal network addresses unless the admin allowed
    them (so a webhook can't be used to poke around inside the network),
  - sending one signed POST with a 5 second limit.

The secret is only ever used to sign; it is never logged or returned."""

import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import threading
import urllib.parse

TIMEOUT = 5
# Wait before retry 1, 2 and 3 (seconds): 1 min, 5 min, 30 min.
RETRY_DELAYS = (60, 300, 1800)
LOCAL_NAMES = ("localhost", "127.0.0.1", "::1")
MAX_URL = 500
MAX_PER_FORM = 5

WAKE = threading.Event()        # set when something new is waiting to be sent


class WebhookError(Exception):
    pass


def new_secret():
    return "whsec_" + secrets.token_hex(24)


def sign(secret, body):
    """Hex HMAC-SHA256 of the exact bytes that are sent."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def check_url(url):
    """Returns (error_text, None) or (None, (scheme, host, port, path))."""
    url = (url or "").strip()
    if not url or len(url) > MAX_URL:
        return "enter a web address (at most %d characters)" % MAX_URL, None
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname
        port = p.port
    except ValueError:
        return "that web address is not valid", None
    if p.scheme not in ("https", "http") or not host:
        return "the address must start with https://", None
    if p.username or p.password:
        return "the address must not contain a user name or password", None
    if p.scheme == "http" and host.lower() not in LOCAL_NAMES:
        return "only https:// is allowed (http:// works for localhost only)", None
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    return None, (p.scheme, host, port or (443 if p.scheme == "https" else 80), path)


def _ip_is_internal(ip):
    ip = ipaddress.ip_address(ip)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified)


def resolve(host, port, allow_local):
    """The IP address to connect to. Every address the name resolves to is
    checked; one internal address is enough to refuse (unless allowed)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise WebhookError("could not find that host")
    ips = []
    for info in infos:
        ip = info[4][0].split("%")[0]
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise WebhookError("could not find that host")
    if not allow_local:
        for ip in ips:
            if _ip_is_internal(ip):
                raise WebhookError("blocked: that address is on a private or internal network "
                                   "(tick \"Allow local network\" to send there)")
    return ips[0]


class _Http(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _Https(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def send(url, secret, body, event, allow_local):
    """One POST. Returns (status_code or None, error text or None); ok means
    a 2xx answer. Redirects are never followed. The connection goes to the
    address that was checked, not to a second DNS lookup."""
    err, parts = check_url(url)
    if err:
        return None, err
    scheme, host, port, path = parts
    try:
        ip = resolve(host, port, allow_local)
        conn = (_Https if scheme == "https" else _Http)(host, port, ip, TIMEOUT)
        try:
            conn.request("POST", path, body=body, headers={
                "Content-Type": "application/json",
                "User-Agent": "OpenFeedbackForms-Webhook",
                "X-OFF-Event": event,
                "X-OFF-Signature": sign(secret, body)})
            resp = conn.getresponse()
            resp.read(1024)
            code = resp.status
        finally:
            conn.close()
    except WebhookError as e:
        return None, str(e)
    except ssl.SSLError:
        return None, "secure connection failed (certificate problem)"
    except socket.timeout:
        return None, "timed out after %d seconds" % TIMEOUT
    except (OSError, http.client.HTTPException) as e:
        return None, ("connection failed: %s" % (e.__class__.__name__))
    if 200 <= code < 300:
        return code, None
    return code, "the receiver answered %d" % code


def build_body(form, submission_id, created_at, answers, hidden, event="submission"):
    """The JSON that is sent. `answers` and `hidden` are exactly what was
    saved (questions hidden by a rule were already dropped)."""
    doc = {"form": {"id": form["id"], "name": form["name"], "slug": form["slug"]},
           "submission": {"id": submission_id, "created_at": created_at,
                          "answers": answers or {}, "hidden": hidden or {}}}
    if event == "test":
        doc["test"] = True
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
