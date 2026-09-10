#!/usr/bin/env python3
"""
Open Feedback Forms — a self-hosted fan/customer feedback receiver, with
an admin panel.

Serves any number of public feedback forms — each on its own port, each
with its own logo/colors/name — plus one admin panel for configuring the
database connection, managing forms and their questions, and browsing
and exporting submissions.

    python server.py

Environment variables (all optional):
    ADMIN_PORT         port the admin panel listens on   (default 8080)
    HOST               address to bind everything to     (default 127.0.0.1)
    FORM_PORT          port given to the very first form  (default 8081)
    TURNSTILE_SECRET   Cloudflare Turnstile secret  (bot check off if unset)
    RATE_LIMIT         submissions per IP per hour  (default 5)

Bind to 127.0.0.1 and let cloudflared reach it there. That way no port is
ever open to the local network, and the only way in is the tunnel — give
cloudflared one hostname per form port you want to expose publicly.

MariaDB connection settings, the admin account, and everything else that
has to exist before the app can even reach a database live in config.json
next to this file (see config_store.py) — set them up from /admin on
first run. Once connected, the required tables are created automatically,
and the first form starts listening right away.
"""

import base64
import csv
import http.cookies
import io
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import zoneinfo
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db
import config_store
import translate_client
import ui_strings

# Bundled read-only assets: sys._MEIPASS when frozen by PyInstaller (onedir's
# _internal folder), the script's own folder otherwise. Never the same
# question as *where does config.json go* — that one has to be writable
# without admin rights, this one never needs to be (see config_store.py).
if getattr(sys, "frozen", False):
    BASE = sys._MEIPASS
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(BASE, "public")
ADMIN_DIR = os.path.join(BASE, "admin")


def _read_version():
    try:
        with open(os.path.join(BASE, "VERSION"), "r", encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


VERSION = _read_version()

HOST = os.environ.get("HOST", "127.0.0.1")
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", os.environ.get("PORT", "8080")))
DEFAULT_FORM_PORT = int(os.environ.get("FORM_PORT", "8081"))
FORM_PORT_RANGE = (8090, 8189)          # auto-assign scans this range
TURNSTILE_SECRET = os.environ.get("TURNSTILE_SECRET", "").strip()
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "5"))

MAX_BODY = 64 * 1024           # refuse anything bigger than 64 KB
MAX_LOGO_BODY = 3 * 1024 * 1024  # logos arrive base64-encoded, so allow more headroom
MAX_LOGO_BYTES = 2 * 1024 * 1024  # the decoded image itself
ALLOWED_LOGO_EXT = {"png", "jpg", "jpeg", "svg", "webp"}
LOGO_CONTENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                       ".svg": "image/svg+xml", ".webp": "image/webp"}
RATE_WINDOW = 3600            # one hour, in seconds
SESSION_TTL = 8 * 3600        # admin login lasts 8 hours
SESSION_COOKIE = "off_session"

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^\+[1-9][0-9]{6,17}$")
FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
CORE_KEYS = {"firstName", "lastName", "email", "consent", "language", "website", "turnstile"}


# --------------------------------------------------------------- rate limit

_hits = {}
_hits_lock = threading.Lock()
ATTEMPT_LIMIT = max(RATE_LIMIT * 6, 30)


def rate_ok(ip, bucket, limit):
    now = time.time()
    key = (ip, bucket)
    with _hits_lock:
        seen = [t for t in _hits.get(key, []) if now - t < RATE_WINDOW]
        if len(seen) >= limit:
            _hits[key] = seen
            return False
        seen.append(now)
        _hits[key] = seen
        if len(_hits) > 5000:
            for k in [k for k, v in _hits.items() if not v or now - max(v) > RATE_WINDOW]:
                _hits.pop(k, None)
        return True


# ---------------------------------------------------------------- turnstile

def turnstile_ok(token, ip):
    if not TURNSTILE_SECRET:
        return True
    if not token:
        return False
    body = urllib.parse.urlencode({
        "secret": TURNSTILE_SECRET, "response": token, "remoteip": ip}).encode()
    req = urllib.request.Request(
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return bool(json.loads(r.read().decode()).get("success"))
    except Exception as e:
        log("turnstile check failed: %s" % e)
        return False


# ------------------------------------------------------------- admin login

_sessions = {}                  # token -> {"username": ..., "expires": epoch}
_sessions_lock = threading.Lock()


def create_session(username):
    token = secrets.token_hex(32)
    with _sessions_lock:
        _sessions[token] = {"username": username, "expires": time.time() + SESSION_TTL}
    return token


def session_username(token):
    if not token:
        return None
    with _sessions_lock:
        s = _sessions.get(token)
        if not s or s["expires"] < time.time():
            _sessions.pop(token, None)
            return None
        return s["username"]


def drop_session(token):
    with _sessions_lock:
        _sessions.pop(token, None)


# --------------------------------------------------------------- validation

def clean(value, limit):
    if not isinstance(value, str):
        return ""
    value = "".join(c for c in value if c >= " " or c in "\n\t")
    return value.strip()[:limit]


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:64] or "form"


def clean_extra_langs(value):
    """Keeps only codes that are actually registered extra languages (en/ar
    are handled by their own dedicated lang_en/lang_ar flags, never here)."""
    if not isinstance(value, list):
        return []
    registered = set(config_store.load()["settings"].get("language_labels", {})) - {"en", "ar"}
    return [c for c in value if isinstance(c, str) and c in registered]


def clean_labels_extra(value):
    """Keeps only registered extra-language codes with a non-empty string
    label — same "unknown/blank entries are dropped, not errors" leniency
    as the rest of the label-editing flows."""
    if not isinstance(value, dict):
        return {}
    registered = set(config_store.load()["settings"].get("language_labels", {})) - {"en", "ar"}
    return {code: clean(text, 200) for code, text in value.items()
            if code in registered and clean(text, 200)}


def normalize_filter_dt(value, end_of_range):
    """Accepts either a plain date (YYYY-MM-DD, from an old bookmarked link
    or a manual query param) or a datetime-local value (YYYY-MM-DDTHH:MM,
    from the admin panel's date/time picker) and turns it into a MySQL
    DATETIME string. `end_of_range` pads a date-only value to the last
    second of that day instead of the first, so a "To" filter is inclusive."""
    value = (value or "").strip().replace("T", " ")
    if len(value) == 10:                              # date only
        return value + (" 23:59:59" if end_of_range else " 00:00:00")
    if len(value) == 16:                               # date + hour:minute
        return value + (":59" if end_of_range else ":00")
    return value


def validate_core(raw):
    """First name, last name, email, consent — the fields every submission has."""
    rec = {
        "firstName": clean(raw.get("firstName"), 60),
        "lastName":  clean(raw.get("lastName"), 60),
        "email":     clean(raw.get("email"), 120),
        "language":  "ar" if raw.get("language") == "ar" else "en",
    }
    if len(rec["firstName"]) < 2:
        return None, "first name too short"
    if len(rec["lastName"]) < 2:
        return None, "last name too short"
    if not EMAIL_RE.match(rec["email"]):
        return None, "invalid email"
    if raw.get("consent") is not True:
        return None, "consent not given"
    return rec, None


def validate_dynamic_value(field, value):
    """One admin-defined field. Returns (clean_value, error_or_None)."""
    ftype = field["field_type"]
    required = field["required"]

    if ftype == "checkbox":
        v = value is True
        if required and not v:
            return None, "%s is required" % field["field_key"]
        return v, None

    if ftype == "rating":
        try:
            n = int(value)
        except (TypeError, ValueError):
            n = None
        if n is None or not 1 <= n <= 5:
            if required or value not in (None, ""):
                return None, "invalid rating for %s" % field["field_key"]
            return None, None
        return n, None

    v = clean(value, 1000)

    if not v:
        if required:
            return None, "%s is required" % field["field_key"]
        return "", None

    if ftype == "email":
        if not EMAIL_RE.match(v):
            return None, "invalid email for %s" % field["field_key"]
    elif ftype == "tel":
        if not PHONE_RE.match(v):
            return None, "invalid phone for %s" % field["field_key"]
    elif ftype == "select":
        valid = {o["value"] for o in (field["options"] or [])}
        if v not in valid:
            return None, "invalid option for %s" % field["field_key"]
    elif ftype == "textarea":
        if len(v) < 10:
            return None, "%s is too short" % field["field_key"]
    # 'text' — length cap already applied by clean(), nothing further to check

    return v, None


def validate_submission(raw, active_fields):
    if raw.get("website"):
        return None, "honeypot"

    core, err = validate_core(raw)
    if err:
        return None, err

    incoming = raw.get("fields")
    if not isinstance(incoming, dict):
        incoming = {}

    extra = {}
    for field in active_fields:
        key = field["field_key"]
        val, ferr = validate_dynamic_value(field, incoming.get(key))
        if ferr:
            return None, ferr
        if val not in (None, ""):
            extra[key] = val

    core["extra"] = extra
    return core, None


# ------------------------------------------------------------------- ports

def port_available(port, host=HOST):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def find_available_port(used_ports, start, end):
    for p in range(start, end + 1):
        if p in used_ports:
            continue
        if port_available(p):
            return p
    return None


# ------------------------------------------------------------------ handler

def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


class BaseHandler(BaseHTTPRequestHandler):
    server_version = "OpenFeedbackForms"
    sys_version = ""

    def client_ip(self):
        for h in ("CF-Connecting-IP", "X-Forwarded-For"):
            v = self.headers.get(h)
            if v:
                return v.split(",")[0].strip()[:45]
        return self.client_address[0]

    def cookies(self):
        c = http.cookies.SimpleCookie()
        c.load(self.headers.get("Cookie", ""))
        return c

    def admin_user(self):
        c = self.cookies()
        if SESSION_COOKIE not in c:
            return None
        return session_username(c[SESSION_COOKIE].value)

    def require_admin(self):
        user = self.admin_user()
        if not user:
            self.send_json(401, {"ok": False, "error": "not authenticated"})
            return None
        return user

    def send_json(self, code, payload, cookie_header=None):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie_header:
            self.send_header("Set-Cookie", cookie_header)
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self, max_body=MAX_BODY):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > max_body:
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def query_one(self, name, default=""):
        v = self.query().get(name)
        return v[0] if v else default

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; "
                         "script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; "
                         "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                         "font-src 'self' https://fonts.gstatic.com; "
                         "frame-src https://challenges.cloudflare.com; "
                         "connect-src 'self' https://challenges.cloudflare.com; "
                         "img-src 'self' data:; base-uri 'none'; form-action 'self'")

    def fail(self, code, text="Not found"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass

    def static_from(self, root, name):
        full = os.path.join(root, name)
        if not os.path.isfile(full):
            return self.fail(404)
        types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
                 ".png": "image/png", ".jpg": "image/jpeg", ".ico": "image/x-icon",
                 ".webp": "image/webp", ".woff2": "font/woff2"}
        ext = os.path.splitext(name)[1].lower()
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", types.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache" if ext == ".html" else "max-age=3600")
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_file_bytes(self, body, content_type, cache=False):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=300" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def serve_logo(self, form_id):
        form = db.get_form(form_id) if db.is_connected() else None
        if not form or not form.get("logo_filename"):
            return self.fail(404)
        path = os.path.join(config_store.uploads_dir(), form["logo_filename"])
        if not os.path.isfile(path):
            return self.fail(404)
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            body = f.read()
        self.send_file_bytes(body, LOGO_CONTENT_TYPES.get(ext, "application/octet-stream"), cache=True)


# --------------------------------------------------------- public (1/form)

class PublicHandler(BaseHandler):
    """Serves one form. FORM_ID is bound per port by make_public_handler()."""
    FORM_ID = None

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/health":
            return self.send_json(200, {"ok": True, "formId": self.FORM_ID})
        if path == "/api/form-fields":
            return self.get_form_fields()
        if path == "/api/ui-strings":
            return self.get_ui_strings()
        if path == "/logo":
            return self.serve_logo(self.FORM_ID)
        if path in ("/", "/index.html"):
            return self.static_from(PUBLIC, "index.html")

        name = path.lstrip("/")
        if "/" in name or "\\" in name or ".." in name or not name:
            return self.fail(404)
        return self.static_from(PUBLIC, name)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/feedback":
            return self.post_feedback()
        return self.fail(404)

    def get_form_fields(self):
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "not configured"})
        try:
            fields = db.list_fields(self.FORM_ID, active_only=True)
            form = db.get_form(self.FORM_ID)
        except Exception as e:
            log("form-fields query failed: %s" % e)
            return self.send_json(500, {"ok": False})
        out = [{"id": f["id"], "field_key": f["field_key"], "label_en": f["label_en"],
                "label_ar": f["label_ar"], "labels_extra": f.get("labels_extra") or {},
                "field_type": f["field_type"],
                "options": f["options"], "required": f["required"]} for f in fields]
        branding = {
            "orgName": (form and (form.get("org_name") or form.get("name"))) or "",
            "primaryColor": (form and form.get("primary_color")) or "#FFEC01",
            "inkColor": (form and form.get("ink_color")) or "#0B0B0B",
            "logoUrl": "/logo" if (form and form.get("logo_filename")) else None,
            "subtitleEn": (form and form.get("subtitle_en")) or None,
            "subtitleAr": (form and form.get("subtitle_ar")) or None,
        }
        languages = {
            "en": bool(form["lang_en"]) if form else True,
            "ar": bool(form["lang_ar"]) if form else True,
        }
        extraLangs = (form and form.get("enabled_extra_langs")) or []
        cfg = config_store.load()
        languageLabels = cfg["settings"].get("language_labels", {})
        return self.send_json(200, {"ok": True, "fields": out, "branding": branding,
                                    "languages": languages, "extraLanguages": extraLangs,
                                    "languageLabels": languageLabels})

    def get_ui_strings(self):
        lang = self.query_one("lang", "")
        cfg = config_store.load()
        strings = cfg["settings"].get("ui_translations", {}).get(lang)
        if not strings:
            return self.send_json(404, {"ok": False, "error": "no cached translation for this language"})
        return self.send_json(200, {"ok": True, "strings": strings})

    def post_feedback(self):
        ip = self.client_ip()
        raw = self.read_json_body()
        if raw is None:
            return self.send_json(400, {"ok": False})

        if not rate_ok(ip, "attempts", ATTEMPT_LIMIT):
            log("flood guard tripped by %s" % ip)
            return self.send_json(429, {"ok": False})

        if not db.is_connected():
            return self.send_json(503, {"ok": False})

        try:
            active_fields = db.list_fields(self.FORM_ID, active_only=True)
        except Exception as e:
            log("could not load fields: %s" % e)
            return self.send_json(500, {"ok": False})

        rec, err = validate_submission(raw, active_fields)
        if err == "honeypot":
            log("honeypot caught a submission from %s" % ip)
            return self.send_json(200, {"ok": True, "reference": db.make_reference()})
        if err:
            log("rejected from %s: %s" % (ip, err))
            return self.send_json(400, {"ok": False})

        if not turnstile_ok(raw.get("turnstile", ""), ip):
            log("turnstile refused %s" % ip)
            return self.send_json(403, {"ok": False})

        if not rate_ok(ip, "saves", RATE_LIMIT):
            log("rate limit hit by %s" % ip)
            return self.send_json(429, {"ok": False})

        rec["ip"] = ip
        rec["userAgent"] = (self.headers.get("User-Agent") or "")[:250]

        try:
            ref = db.save_feedback(self.FORM_ID, rec)
        except Exception as e:
            log("SAVE FAILED: %s" % e)
            return self.send_json(500, {"ok": False})

        log("saved %s from %s (form %s)" % (ref, ip, self.FORM_ID))
        return self.send_json(200, {"ok": True, "reference": ref})


def make_public_handler(form_id):
    class _Bound(PublicHandler):
        FORM_ID = form_id
    return _Bound


# ------------------------------------------------------------- form manager

class FormManager:
    """Owns one ThreadingHTTPServer per active form, each on its own port,
    each running in its own daemon thread inside this one process."""

    def __init__(self):
        self._lock = threading.Lock()
        self._running = {}       # form_id -> {"httpd":, "port":, "name":}

    def start(self, form):
        try:
            handler_cls = make_public_handler(form["id"])
            httpd = ThreadingHTTPServer((HOST, form["port"]), handler_cls)
        except OSError as e:
            log("could not start form '%s' on port %d: %s" % (form["name"], form["port"], e))
            return str(e)
        httpd.daemon_threads = True
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        with self._lock:
            self._running[form["id"]] = {"httpd": httpd, "port": form["port"], "name": form["name"]}
        log("form '%s' listening on http://%s:%d" % (form["name"], HOST, form["port"]))
        return None

    def stop(self, form_id):
        with self._lock:
            entry = self._running.pop(form_id, None)
        if entry:
            entry["httpd"].shutdown()
            entry["httpd"].server_close()
            log("stopped listener on port %d" % entry["port"])

    def stop_all(self):
        with self._lock:
            ids = list(self._running.keys())
        for fid in ids:
            self.stop(fid)

    def status(self):
        with self._lock:
            return {fid: {"port": e["port"], "name": e["name"]} for fid, e in self._running.items()}

    def sync(self):
        """Reconcile running listeners against the forms table: start any
        active form that isn't listening yet (or whose port changed),
        stop any listener for a form that's gone inactive or been deleted."""
        if not db.is_connected():
            return
        try:
            forms = db.list_forms(active_only=True)
        except Exception as e:
            log("could not load forms: %s" % e)
            return
        want = {f["id"]: f for f in forms}

        with self._lock:
            running_snapshot = {fid: e["port"] for fid, e in self._running.items()}
        for fid in running_snapshot:
            if fid not in want:
                self.stop(fid)

        for fid, form in want.items():
            current_port = running_snapshot.get(fid)
            if current_port == form["port"]:
                continue
            if current_port is not None:
                self.stop(fid)
            self.start(form)


FORMS = FormManager()


# -------------------------------------------------------------- admin (1x)

class AdminHandler(BaseHandler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/health":
            return self.send_json(200, {"ok": True})
        if path in ("/", "/admin", "/admin/"):
            return self.static_from(ADMIN_DIR, "index.html")
        if path == "/admin/status":
            return self.get_admin_status()
        if path == "/admin/db-settings":
            return self.get_db_settings()
        if path == "/admin/app-settings":
            return self.get_app_settings()
        if path == "/admin/forms":
            return self.get_admin_forms()
        if path == "/admin/fields":
            return self.get_admin_fields()
        if path == "/admin/field-library":
            return self.get_field_library()
        if path == "/admin/submissions":
            return self.get_admin_submissions()
        if path == "/admin/export.csv":
            return self.export_csv()
        if path == "/admin/dashboard":
            return self.get_admin_dashboard()
        if path == "/admin/translate/languages":
            return self.get_translate_languages()

        m = re.match(r"^/admin/forms/(\d+)/branding$", path)
        if m:
            return self.get_admin_branding(int(m.group(1)))
        m = re.match(r"^/admin/forms/(\d+)/logo$", path)
        if m:
            if not self.require_admin():
                return
            return self.serve_logo(int(m.group(1)))

        return self.fail(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        routes = {
            "/admin/setup": self.post_admin_setup,
            "/admin/login": self.post_admin_login,
            "/admin/logout": self.post_admin_logout,
            "/admin/db-settings/test": self.post_db_test,
            "/admin/db-settings": self.post_db_settings,
            "/admin/app-settings": self.post_app_settings,
            "/admin/language-labels": self.post_language_labels,
            "/admin/language-labels/add": self.post_language_labels_add,
            "/admin/language-labels/remove": self.post_language_labels_remove,
            "/admin/forms": self.post_form_create,
            "/admin/fields": self.post_field_create,
            "/admin/fields/reorder": self.post_field_reorder,
            "/admin/field-keys": self.post_field_key_create,
        }
        if path in routes:
            return routes[path]()

        m = re.match(r"^/admin/forms/(\d+)/(update|delete|restore)$", path)
        if m:
            form_id, action = int(m.group(1)), m.group(2)
            if action == "update":
                return self.post_form_update(form_id)
            if action == "restore":
                return self.post_form_restore(form_id)
            return self.post_form_delete(form_id)

        m = re.match(r"^/admin/forms/(\d+)/branding$", path)
        if m:
            return self.post_form_branding(int(m.group(1)))

        m = re.match(r"^/admin/fields/(\d+)/(update|delete|restore)$", path)
        if m:
            field_id, action = int(m.group(1)), m.group(2)
            if action == "update":
                return self.post_field_update(field_id)
            if action == "restore":
                return self.post_field_restore(field_id)
            return self.post_field_delete(field_id)

        m = re.match(r"^/admin/field-keys/(\d+)/(update|delete)$", path)
        if m:
            key_id, action = int(m.group(1)), m.group(2)
            if action == "update":
                return self.post_field_key_update(key_id)
            return self.post_field_key_delete(key_id)

        return self.fail(404)

    # ---- status / db settings -----------------------------------------

    def get_admin_status(self):
        cfg = config_store.load()
        return self.send_json(200, {
            "ok": True,
            "adminConfigured": config_store.admin_configured(cfg),
            "dbConfigured": config_store.db_configured(cfg),
            "dbConnected": db.is_connected(),
            "dbError": db.last_error(),
            "authenticated": bool(self.admin_user()),
            # Safe to expose pre-login (just colors) — the login/setup
            # screens need it too, not only the dashboard behind auth.
            "adminTheme": cfg["settings"].get("admin_theme", {"primary": "#FFEC01", "text": "#0B0B0B"}),
            "version": VERSION,
        })

    def get_db_settings(self):
        if not self.require_admin():
            return
        cfg = config_store.load()
        d = dict(cfg["db"])
        d["password"] = ""                              # never echo it back
        d["hasPassword"] = bool(cfg["db"]["password"])
        return self.send_json(200, {"ok": True, "db": d, "connected": db.is_connected(),
                                    "error": db.last_error()})

    def get_app_settings(self):
        if not self.require_admin():
            return
        cfg = config_store.load()
        s = cfg.get("settings", {"timezone": "UTC", "ntp_server": "pool.ntp.org"})
        return self.send_json(200, {"ok": True, "settings": s,
                                    "timezones": sorted(zoneinfo.available_timezones())})

    def post_app_settings(self):
        if not self.require_admin():
            return
        body = self.read_json_body() or {}
        cfg = config_store.load()
        # timezone/ntp_server are omitted when this save came from the Appearance
        # sub-tab (theme colors only) — fall back to what's already saved instead
        # of the hardcoded defaults, so saving one setting doesn't reset the other.
        tz = clean(body.get("timezone"), 64) or cfg["settings"].get("timezone") or "UTC"
        if tz not in zoneinfo.available_timezones():
            return self.send_json(400, {"ok": False, "error": "unknown timezone"})
        ntp = clean(body.get("ntp_server"), 120) or cfg["settings"].get("ntp_server") or "pool.ntp.org"
        if not re.match(r"^[a-zA-Z0-9.-]+$", ntp):
            return self.send_json(400, {"ok": False, "error": "NTP server must be a plain hostname, like pool.ntp.org"})
        theme = body.get("admin_theme")
        if theme is not None:
            if not isinstance(theme, dict):
                return self.send_json(400, {"ok": False, "error": "invalid admin theme"})
            primary = clean(theme.get("primary"), 7) or "#FFEC01"
            text = clean(theme.get("text"), 7) or "#0B0B0B"
            if not re.match(r"^#[0-9a-fA-F]{6}$", primary) or not re.match(r"^#[0-9a-fA-F]{6}$", text):
                return self.send_json(400, {"ok": False, "error": "colors must be a 6-digit hex code, like #FFEC01"})
            cfg["settings"]["admin_theme"] = {"primary": primary, "text": text}
        if "translate_api_key" in body:
            cfg["settings"]["translate_api_key"] = clean(body.get("translate_api_key"), 200)
        cfg["settings"]["timezone"] = tz
        cfg["settings"]["ntp_server"] = ntp
        config_store.save(cfg)
        return self.send_json(200, {"ok": True})

    def post_language_labels(self):
        if not self.require_admin():
            return
        body = self.read_json_body() or {}
        labels = body.get("language_labels")
        cfg = config_store.load()
        registered = set(cfg["settings"].get("language_labels", {}))
        if not isinstance(labels, dict) or not labels or not set(labels.keys()) <= registered:
            return self.send_json(400, {"ok": False, "error": "unknown language key"})
        cleaned = {}
        for key, entry in labels.items():
            if not isinstance(entry, dict):
                return self.send_json(400, {"ok": False, "error": "invalid entry for " + key})
            name = clean(entry.get("name"), 40)
            if not name:
                return self.send_json(400, {"ok": False, "error": "a display name is required for " + key})
            direction = entry.get("dir")
            if direction not in ("ltr", "rtl"):
                return self.send_json(400, {"ok": False, "error": "direction must be ltr or rtl for " + key})
            cleaned[key] = {"name": name, "dir": direction}
        # Merge just the given keys — a rename never touches languages this
        # particular save didn't mention.
        cfg["settings"]["language_labels"].update(cleaned)
        config_store.save(cfg)
        return self.send_json(200, {"ok": True})

    def get_translate_languages(self):
        if not self.require_admin():
            return
        cfg = config_store.load()
        endpoint = cfg["settings"].get("translate_endpoint")
        langs = translate_client.list_languages(endpoint)
        registered = set(cfg["settings"].get("language_labels", {}))
        # Already-added languages (including en/ar) don't need offering again.
        langs = [l for l in langs if l["code"] not in registered]
        return self.send_json(200, {"ok": True, "languages": langs})

    def post_language_labels_add(self):
        if not self.require_admin():
            return
        body = self.read_json_body() or {}
        code = clean(body.get("code"), 10).lower()
        if not re.match(r"^[a-z]{2,3}(-[a-z]{2,4})?$", code):
            return self.send_json(400, {"ok": False, "error": "invalid language code"})
        cfg = config_store.load()
        if code in cfg["settings"].get("language_labels", {}):
            return self.send_json(400, {"ok": False, "error": "that language is already added"})
        name = clean(body.get("name"), 40) or code
        direction = body.get("dir") if body.get("dir") in ("ltr", "rtl") else \
            ("rtl" if code in translate_client.RTL_CODES else "ltr")

        endpoint = cfg["settings"].get("translate_endpoint")
        api_key = cfg["settings"].get("translate_api_key") or None
        translated = failed = 0

        def translate_map(source_map):
            """source_map: {id: english_text}. Returns {id: translated_text}
            for whatever came back non-None; missing keys mean 'not translated,
            leave for the admin to fill in by hand' — never a raised error."""
            nonlocal translated, failed
            ids = list(source_map.keys())
            texts = [source_map[i] for i in ids]
            results = translate_client.translate_batch(texts, code, endpoint=endpoint, api_key=api_key)
            out = {}
            for i, value in zip(ids, results):
                if value:
                    out[i] = value
                    translated += 1
                else:
                    failed += 1
            return out

        # 1) fixed UI chrome strings
        ui_source = dict(ui_strings.UI_STRINGS)
        ui_translated = translate_map(ui_source)
        ui_translated.update(ui_strings.UI_TEMPLATE_ONLY)   # copied verbatim, not sent through translate
        words_translated = translate_client.translate_batch(ui_strings.UI_WORDS, code, endpoint=endpoint, api_key=api_key)
        ui_translated["words"] = [w or "" for w in words_translated]
        cfg["settings"].setdefault("ui_translations", {})[code] = ui_translated

        # 2) every field-key label + its options
        if db.is_connected():
            try:
                for k in db.list_field_keys():
                    extra = dict(k.get("labels_extra") or {})
                    result = translate_map({"_": k["label_en"]})
                    if "_" in result:
                        extra[code] = result["_"]
                    opts = k.get("options")
                    if opts:
                        opt_map = {i: o["label_en"] for i, o in enumerate(opts) if o.get("label_en")}
                        opt_result = translate_map(opt_map)
                        for i, o in enumerate(opts):
                            if i in opt_result:
                                o.setdefault("label_extra", {})[code] = opt_result[i]
                    db.update_field_key(k["id"], {
                        "label_en": k["label_en"], "label_ar": k["label_ar"],
                        "labels_extra": extra, "field_type": k["field_type"], "options": opts})

                # 3) every per-form field override that has its own label
                for f in db.list_all_fields():
                    extra = dict(f.get("labels_extra") or {})
                    result = translate_map({"_": f["label_en"]})
                    if "_" in result:
                        extra[code] = result["_"]
                    opts = f.get("options")
                    if opts:
                        opt_map = {i: o["label_en"] for i, o in enumerate(opts) if o.get("label_en")}
                        opt_result = translate_map(opt_map)
                        for i, o in enumerate(opts):
                            if i in opt_result:
                                o.setdefault("label_extra", {})[code] = opt_result[i]
                    db.update_field(f["id"], {
                        "label_en": f["label_en"], "label_ar": f["label_ar"],
                        "labels_extra": extra, "field_type": f["field_type"],
                        "options": opts, "required": f["required"]})
            except Exception as e:
                log("language add: translating existing fields failed: %s" % e)

        cfg["settings"]["language_labels"][code] = {"name": name, "dir": direction}
        config_store.save(cfg)
        return self.send_json(200, {"ok": True, "code": code, "translated": translated, "failed": failed})

    def post_language_labels_remove(self):
        if not self.require_admin():
            return
        body = self.read_json_body() or {}
        code = clean(body.get("code"), 10).lower()
        cfg = config_store.load()
        if code in ("en", "ar"):
            return self.send_json(400, {"ok": False, "error": "the built-in en/ar pair can't be removed"})
        if code not in cfg["settings"].get("language_labels", {}):
            return self.send_json(404, {"ok": False, "error": "that language isn't registered"})
        del cfg["settings"]["language_labels"][code]
        cfg["settings"].get("ui_translations", {}).pop(code, None)
        config_store.save(cfg)
        # Best-effort: stop offering it on any form that had it enabled.
        # Labels left behind in labels_extra_json are harmless — unreferenced,
        # never shown once the language is gone from language_labels.
        if db.is_connected():
            try:
                for f in db.list_forms(active_only=False):
                    extras = f.get("enabled_extra_langs") or []
                    if code in extras:
                        db.update_form(f["id"], enabled_extra_langs=[c for c in extras if c != code])
            except Exception as e:
                log("language remove: form cleanup failed: %s" % e)
        return self.send_json(200, {"ok": True})

    def post_db_test(self):
        if not self.require_admin():
            return
        body = self.read_json_body() or {}
        db_cfg = self._db_cfg_from_body(body)
        try:
            db.test_connection(db_cfg)
        except Exception as e:
            return self.send_json(200, {"ok": False, "error": str(e)})
        return self.send_json(200, {"ok": True})

    def post_db_settings(self):
        if not self.require_admin():
            return
        body = self.read_json_body() or {}
        db_cfg = self._db_cfg_from_body(body)

        try:
            pool = db.connect_and_prepare(db_cfg, default_port=DEFAULT_FORM_PORT)
        except Exception as e:
            log("db connect failed: %s" % e)
            return self.send_json(200, {"ok": False, "error": str(e)})

        cfg = config_store.load()
        cfg["db"] = db_cfg
        config_store.save(cfg)
        db.set_pool(pool)
        log("database connected: %s@%s/%s" % (db_cfg["user"], db_cfg["host"], db_cfg["database"]))
        FORMS.sync()
        return self.send_json(200, {"ok": True})

    def _db_cfg_from_body(self, body):
        cfg = config_store.load()
        password = body.get("password")
        if not password:                                 # blank = keep the saved one
            password = cfg["db"].get("password", "")
        return {
            "host": clean(body.get("host"), 120),
            "port": int(body.get("port") or 3306),
            "user": clean(body.get("user"), 60),
            "password": password,
            "database": clean(body.get("database"), 64),
        }

    # ---- forms ----------------------------------------------------------

    def get_admin_forms(self):
        if not self.require_admin():
            return
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        forms = db.list_forms(active_only=False)
        running = FORMS.status()
        out = [{
            "id": f["id"], "name": f["name"], "slug": f["slug"], "port": f["port"],
            "active": f["active"], "listening": f["id"] in running,
            "url": "http://%s:%d/" % (HOST, f["port"]),
            "lang_en": f["lang_en"], "lang_ar": f["lang_ar"],
            "enabled_extra_langs": f.get("enabled_extra_langs") or [],
        } for f in forms]
        return self.send_json(200, {"ok": True, "forms": out, "adminPort": ADMIN_PORT})

    def post_form_create(self):
        if not self.require_admin():
            return
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        body = self.read_json_body() or {}
        name = clean(body.get("name"), 120)
        if len(name) < 2:
            return self.send_json(400, {"ok": False, "error": "form name is required"})
        slug_in = clean(body.get("slug"), 64).lower()
        slug = re.sub(r"[^a-z0-9-]", "-", slug_in).strip("-")[:64] if slug_in else slugify(name)
        if not slug:
            slug = slugify(name)

        port_err, port = self._resolve_new_port(body.get("port"))
        if port_err:
            return self.send_json(400, {"ok": False, "error": port_err})

        lang_en = bool(body.get("lang_en", True))
        lang_ar = bool(body.get("lang_ar", True))
        extra_langs = clean_extra_langs(body.get("enabled_extra_langs"))
        if not lang_en and not lang_ar and not extra_langs:
            return self.send_json(400, {"ok": False, "error": "at least one language must stay enabled"})

        try:
            new_id = db.create_form(name, slug, port, lang_en=lang_en, lang_ar=lang_ar,
                                    enabled_extra_langs=extra_langs)
        except Exception as e:
            return self.send_json(400, {"ok": False, "error": str(e)})
        FORMS.sync()
        log("form created: %s (port %d)" % (name, port))
        return self.send_json(200, {"ok": True, "id": new_id, "port": port})

    def _resolve_new_port(self, requested, exclude_form_id=None):
        used = db.used_ports(exclude_form_id=exclude_form_id)
        if requested:
            try:
                port = int(requested)
            except (TypeError, ValueError):
                return "port must be a number", None
            if not (1024 <= port <= 65535):
                return "port must be between 1024 and 65535", None
            if port == ADMIN_PORT or port in used:
                return "that port is already assigned to another form", None
            if not port_available(port):
                return "that port is already in use on this machine", None
            return None, port
        port = find_available_port(used | {ADMIN_PORT}, *FORM_PORT_RANGE)
        if not port:
            return "no available ports left in the auto-assign range (%d–%d)" % FORM_PORT_RANGE, None
        return None, port

    def post_form_update(self, form_id):
        if not self.require_admin():
            return
        form = db.get_form(form_id)
        if not form:
            return self.send_json(404, {"ok": False, "error": "form not found"})
        body = self.read_json_body() or {}
        name = clean(body.get("name"), 120) or form["name"]

        requested_port = body.get("port")
        if requested_port and int(requested_port) == form["port"]:
            requested_port = None                        # unchanged, skip re-validation
        if requested_port:
            port_err, port = self._resolve_new_port(requested_port, exclude_form_id=form_id)
            if port_err:
                return self.send_json(400, {"ok": False, "error": port_err})
        else:
            port = form["port"]

        lang_en = form["lang_en"] if "lang_en" not in body else bool(body.get("lang_en"))
        lang_ar = form["lang_ar"] if "lang_ar" not in body else bool(body.get("lang_ar"))
        extra_langs = (form.get("enabled_extra_langs") or []) if "enabled_extra_langs" not in body \
            else clean_extra_langs(body.get("enabled_extra_langs"))
        if not lang_en and not lang_ar and not extra_langs:
            return self.send_json(400, {"ok": False, "error": "at least one language must stay enabled"})

        db.update_form(form_id, name=name, port=port, lang_en=lang_en, lang_ar=lang_ar,
                        enabled_extra_langs=extra_langs)
        FORMS.sync()
        return self.send_json(200, {"ok": True})

    def post_form_delete(self, form_id):
        if not self.require_admin():
            return
        db.set_form_active(form_id, False)
        FORMS.sync()
        return self.send_json(200, {"ok": True})

    def post_form_restore(self, form_id):
        if not self.require_admin():
            return
        form = db.get_form(form_id)
        if not form:
            return self.send_json(404, {"ok": False, "error": "form not found"})
        if form["port"] == ADMIN_PORT or (form["port"] in db.used_ports(exclude_form_id=form_id)):
            return self.send_json(400, {"ok": False,
                "error": "port %d is now used elsewhere — edit this form's port first" % form["port"]})
        if not port_available(form["port"]):
            return self.send_json(400, {"ok": False,
                "error": "port %d is no longer free on this machine — edit this form's port first" % form["port"]})
        db.set_form_active(form_id, True)
        FORMS.sync()
        return self.send_json(200, {"ok": True})

    # ---- branding ---------------------------------------------------------

    def get_admin_branding(self, form_id):
        if not self.require_admin():
            return
        form = db.get_form(form_id)
        if not form:
            return self.send_json(404, {"ok": False, "error": "form not found"})
        return self.send_json(200, {"ok": True, "branding": {
            "org_name": form.get("org_name") or "",
            "primary_color": form.get("primary_color") or "#FFEC01",
            "ink_color": form.get("ink_color") or "#0B0B0B",
            "subtitle_en": form.get("subtitle_en") or "",
            "subtitle_ar": form.get("subtitle_ar") or "",
            "hasLogo": bool(form.get("logo_filename")),
        }})

    def post_form_branding(self, form_id):
        if not self.require_admin():
            return
        form = db.get_form(form_id)
        if not form:
            return self.send_json(404, {"ok": False, "error": "form not found"})
        body = self.read_json_body(max_body=MAX_LOGO_BODY) or {}

        org_name = body.get("org_name")
        if org_name is not None:
            org_name = clean(org_name, 120)

        subtitle_en = body.get("subtitle_en")
        if subtitle_en is not None:
            subtitle_en = clean(subtitle_en, 300)
        subtitle_ar = body.get("subtitle_ar")
        if subtitle_ar is not None:
            subtitle_ar = clean(subtitle_ar, 300)

        primary_color = body.get("primary_color") or None
        ink_color = body.get("ink_color") or None
        for c in (primary_color, ink_color):
            if c and not HEX_COLOR_RE.match(c):
                return self.send_json(400, {"ok": False, "error": "colors must be hex, like #FFEC01"})

        updir = config_store.uploads_dir()
        clear_logo = bool(body.get("remove_logo"))
        logo_filename = None
        if not clear_logo and body.get("logo_base64"):
            ext = (body.get("logo_ext") or "").lower().lstrip(".")
            if ext not in ALLOWED_LOGO_EXT:
                return self.send_json(400, {"ok": False, "error": "logo must be PNG, JPG, WEBP or SVG"})
            try:
                raw = base64.b64decode(body["logo_base64"], validate=True)
            except Exception:
                return self.send_json(400, {"ok": False, "error": "could not decode the uploaded image"})
            if len(raw) > MAX_LOGO_BYTES:
                return self.send_json(400, {"ok": False, "error": "logo must be under 2 MB"})
            self._remove_logo_files(updir, form_id)
            logo_filename = "form_%d.%s" % (form_id, ext)
            with open(os.path.join(updir, logo_filename), "wb") as f:
                f.write(raw)
        elif clear_logo:
            self._remove_logo_files(updir, form_id)

        db.update_branding(form_id, org_name=org_name, primary_color=primary_color,
                            ink_color=ink_color, subtitle_en=subtitle_en, subtitle_ar=subtitle_ar,
                            logo_filename=logo_filename, clear_logo=clear_logo)
        return self.send_json(200, {"ok": True})

    @staticmethod
    def _remove_logo_files(updir, form_id):
        prefix = "form_%d." % form_id
        for existing in os.listdir(updir):
            if existing.startswith(prefix):
                try:
                    os.remove(os.path.join(updir, existing))
                except OSError:
                    pass

    # ---- fields -----------------------------------------------------------

    def get_admin_fields(self):
        if not self.require_admin():
            return
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        try:
            form_id = int(self.query_one("form_id") or 0)
        except ValueError:
            form_id = 0
        if not form_id:
            return self.send_json(400, {"ok": False, "error": "form_id is required"})
        fields = db.list_fields(form_id, active_only=False)
        return self.send_json(200, {"ok": True, "fields": fields})

    def get_field_library(self):
        if not self.require_admin():
            return
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        return self.send_json(200, {"ok": True, "fields": db.list_field_keys()})

    def post_field_key_create(self):
        if not self.require_admin():
            return
        body = self.read_json_body() or {}
        err = self._validate_field_key_payload(body, is_new=True)
        if err:
            return self.send_json(400, {"ok": False, "error": err})
        body["labels_extra"] = clean_labels_extra(body.get("labels_extra"))
        try:
            new_id = db.create_field_key(body)
        except Exception as e:
            return self.send_json(400, {"ok": False, "error": str(e)})
        return self.send_json(200, {"ok": True, "id": new_id})

    def post_field_key_update(self, key_id):
        if not self.require_admin():
            return
        body = self.read_json_body() or {}
        err = self._validate_field_key_payload(body, is_new=False)
        if err:
            return self.send_json(400, {"ok": False, "error": err})
        body["labels_extra"] = clean_labels_extra(body.get("labels_extra"))
        try:
            db.update_field_key(key_id, body)
        except Exception as e:
            return self.send_json(400, {"ok": False, "error": str(e)})
        return self.send_json(200, {"ok": True})

    def post_field_key_delete(self, key_id):
        if not self.require_admin():
            return
        db.delete_field_key(key_id)
        return self.send_json(200, {"ok": True})

    def _validate_field_key_payload(self, body, is_new):
        if is_new:
            key = body.get("field_key", "")
            if not FIELD_KEY_RE.match(key or ""):
                return "field key must be lowercase letters, numbers and underscores, starting with a letter"
            if key in CORE_KEYS:
                return "that key is reserved"
            if any(k["field_key"] == key for k in db.list_field_keys()):
                return "that field key already exists"
        if not clean(body.get("label_en"), 200):
            return "English label is required"
        if not clean(body.get("label_ar"), 200):
            return "Arabic label is required"
        if body.get("field_type") not in ("text", "email", "tel", "textarea", "select", "rating", "checkbox"):
            return "invalid field type"
        if body.get("field_type") == "select":
            opts = body.get("options")
            if not isinstance(opts, list) or not opts:
                return "a select field needs at least one option"
            for o in opts:
                if not isinstance(o, dict) or not o.get("value") or not (o.get("label_en") or o.get("label_ar")):
                    return "every option needs a value and an English or Arabic label"
        return None

    def post_field_create(self):
        if not self.require_admin():
            return
        body = self.read_json_body() or {}
        try:
            form_id = int(body.get("form_id") or 0)
        except (TypeError, ValueError):
            form_id = 0
        if not form_id:
            return self.send_json(400, {"ok": False, "error": "form_id is required"})
        # A new form field must reference an existing field-key definition —
        # the key, its labels, type and options are owned by the Field keys
        # tab, not typed in here, so pull the canonical copy server-side and
        # ignore anything else the client sent for those.
        key = clean(body.get("field_key"), 64)
        lib_entry = next((k for k in db.list_field_keys() if k["field_key"] == key), None)
        if not lib_entry:
            return self.send_json(400, {"ok": False, "error": "unknown field key — define it in the Field keys tab first"})
        payload = {
            "field_key": lib_entry["field_key"], "label_en": lib_entry["label_en"],
            "label_ar": lib_entry["label_ar"], "labels_extra": lib_entry.get("labels_extra") or {},
            "field_type": lib_entry["field_type"],
            "options": lib_entry["options"], "required": bool(body.get("required")),
        }
        try:
            new_id = db.create_field(form_id, payload)
        except Exception as e:
            return self.send_json(400, {"ok": False, "error": str(e)})
        return self.send_json(200, {"ok": True, "id": new_id})

    def post_field_update(self, field_id):
        if not self.require_admin():
            return
        body = self.read_json_body() or {}
        err = self._validate_field_payload(body, is_new=False)
        if err:
            return self.send_json(400, {"ok": False, "error": err})
        body["labels_extra"] = clean_labels_extra(body.get("labels_extra"))
        try:
            db.update_field(field_id, body)
        except Exception as e:
            return self.send_json(400, {"ok": False, "error": str(e)})
        return self.send_json(200, {"ok": True})

    def post_field_delete(self, field_id):
        if not self.require_admin():
            return
        db.set_field_active(field_id, False)
        return self.send_json(200, {"ok": True})

    def post_field_restore(self, field_id):
        if not self.require_admin():
            return
        db.set_field_active(field_id, True)
        return self.send_json(200, {"ok": True})

    def post_field_reorder(self):
        if not self.require_admin():
            return
        body = self.read_json_body() or {}
        order = body.get("order")
        if not isinstance(order, list) or not all(isinstance(i, int) for i in order):
            return self.send_json(400, {"ok": False, "error": "order must be a list of ids"})
        db.reorder_fields(order)
        return self.send_json(200, {"ok": True})

    def _validate_field_payload(self, body, is_new):
        if is_new:
            key = body.get("field_key", "")
            if not FIELD_KEY_RE.match(key or ""):
                return "field key must be lowercase letters, numbers and underscores, starting with a letter"
            if key in CORE_KEYS:
                return "that key is reserved"
        if not clean(body.get("label_en"), 200):
            return "English label is required"
        if not clean(body.get("label_ar"), 200):
            return "Arabic label is required"
        if body.get("field_type") not in ("text", "email", "tel", "textarea", "select", "rating", "checkbox"):
            return "invalid field type"
        if body.get("field_type") == "select":
            opts = body.get("options")
            if not isinstance(opts, list) or not opts:
                return "a select field needs at least one option"
            for o in opts:
                if not isinstance(o, dict) or not o.get("value") or not (o.get("label_en") or o.get("label_ar")):
                    return "every option needs a value and an English or Arabic label"
        return None

    # ---- dashboard / export ---------------------------------------------

    def _filters_from_query(self):
        filters = {}
        if self.query_one("form_id"):
            try:
                filters["form_id"] = int(self.query_one("form_id"))
            except ValueError:
                pass
        if self.query_one("date_from"):
            filters["date_from"] = normalize_filter_dt(self.query_one("date_from"), end_of_range=False)
        if self.query_one("date_to"):
            filters["date_to"] = normalize_filter_dt(self.query_one("date_to"), end_of_range=True)
        if self.query_one("status"):
            filters["status"] = clean(self.query_one("status"), 20)
        if self.query_one("q"):
            filters["q"] = clean(self.query_one("q"), 120)
        return filters

    def get_admin_submissions(self):
        if not self.require_admin():
            return
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        filters = self._filters_from_query()
        try:
            page = max(1, int(self.query_one("page") or "1"))
        except ValueError:
            page = 1
        page_size = 25
        try:
            rows, total = db.list_submissions(filters, page=page, page_size=page_size)
        except Exception as e:
            log("submissions query failed: %s" % e)
            return self.send_json(500, {"ok": False})
        return self.send_json(200, {"ok": True, "rows": rows, "total": total,
                                    "page": page, "pageSize": page_size})

    def get_admin_dashboard(self):
        if not self.require_admin():
            return
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        filters = self._filters_from_query()
        try:
            summary = db.dashboard_summary(filters)
        except Exception as e:
            log("dashboard query failed: %s" % e)
            return self.send_json(500, {"ok": False})
        return self.send_json(200, {"ok": True, "summary": summary})

    def export_csv(self):
        if not self.admin_user():
            return self.fail(403, "Forbidden")
        if not db.is_connected():
            return self.fail(503, "Database not connected")
        filters = self._filters_from_query()
        try:
            rows = db.export_rows(filters)
        except Exception as e:
            log("export failed: %s" % e)
            return self.fail(500, "Export failed")

        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            w.writeheader()
            for r in rows:
                w.writerow(r)
        body = ("﻿" + buf.getvalue()).encode("utf-8")     # BOM: Excel opens Arabic text correctly
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="feedback-export-%s.csv"' % stamp)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        log("exported %d rows to %s" % (len(rows), self.client_ip()))

    # ---- auth -------------------------------------------------------------

    def post_admin_setup(self):
        cfg = config_store.load()
        if config_store.admin_configured(cfg):
            return self.send_json(403, {"ok": False, "error": "admin account already exists"})
        body = self.read_json_body() or {}
        username = clean(body.get("username"), 60)
        password = body.get("password") or ""
        if len(username) < 3:
            return self.send_json(400, {"ok": False, "error": "username too short"})
        if len(password) < 8:
            return self.send_json(400, {"ok": False, "error": "password must be at least 8 characters"})
        pw_hash, salt = config_store.hash_password(password)
        cfg["admin"] = {"username": username, "password_hash": pw_hash, "salt": salt}
        config_store.save(cfg)
        token = create_session(username)
        cookie = "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (SESSION_COOKIE, token, SESSION_TTL)
        log("admin account created: %s" % username)
        return self.send_json(200, {"ok": True}, cookie_header=cookie)

    def post_admin_login(self):
        cfg = config_store.load()
        body = self.read_json_body() or {}
        username = clean(body.get("username"), 60)
        password = body.get("password") or ""
        admin = cfg["admin"]
        ip = self.client_ip()
        if not rate_ok(ip, "login", 20):
            return self.send_json(429, {"ok": False, "error": "too many attempts, try later"})
        if (not admin["username"] or username != admin["username"] or
                not config_store.verify_password(password, admin["password_hash"], admin["salt"])):
            log("failed admin login for %r from %s" % (username, ip))
            return self.send_json(401, {"ok": False, "error": "invalid username or password"})
        token = create_session(username)
        cookie = "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (SESSION_COOKIE, token, SESSION_TTL)
        log("admin login: %s" % username)
        return self.send_json(200, {"ok": True}, cookie_header=cookie)

    def post_admin_logout(self):
        c = self.cookies()
        if SESSION_COOKIE in c:
            drop_session(c[SESSION_COOKIE].value)
        cookie = "%s=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" % SESSION_COOKIE
        return self.send_json(200, {"ok": True}, cookie_header=cookie)


# --------------------------------------------------------------------- main

def main():
    if not os.path.isdir(PUBLIC):
        sys.exit("public/ folder is missing next to server.py")
    if not os.path.isdir(ADMIN_DIR):
        sys.exit("admin/ folder is missing next to server.py")

    cfg = config_store.load()
    if config_store.db_configured(cfg):
        try:
            pool = db.connect_and_prepare(cfg["db"], default_port=DEFAULT_FORM_PORT)
            db.set_pool(pool)
        except Exception as e:
            db.clear_pool(str(e))
            log("could not connect to the configured database: %s" % e)
            log("fix this from /admin — forms won't accept submissions until it's connected")

    log("Open Feedback Forms v%s" % VERSION)
    log("  admin panel  http://%s:%d/admin" % (HOST, ADMIN_PORT))
    log("  database     %s" % ("connected" if db.is_connected() else "NOT CONFIGURED — set it up from /admin"))
    log("  turnstile    %s" % ("on" if TURNSTILE_SECRET else "OFF — set TURNSTILE_SECRET"))
    log("  rate limit   %d submissions per IP per hour" % RATE_LIMIT)

    if db.is_connected():
        FORMS.sync()

    srv = ThreadingHTTPServer((HOST, ADMIN_PORT), AdminHandler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
        FORMS.stop_all()
        srv.shutdown()


if __name__ == "__main__":
    main()
