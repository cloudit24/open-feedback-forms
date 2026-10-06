#!/usr/bin/env python3
"""
Open Feedback Forms — a self-hosted feedback receiver, with
an admin panel.

Serves any number of public feedback forms — each on its own port, each
with its own logo/colors/name — plus one admin panel for configuring the
database connection, managing forms and their questions, and browsing
and exporting submissions.

    python server.py

Environment variables (all optional):
    ADMIN_PORT         port the admin panel listens on   (default 8800)
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
import shutil
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
import zoneinfo
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db
import config_store
import translate_client
import logic
import sanitise
import templates
import ui_strings
import notifier
import formstate
import webhooks

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

# Update check: the published VERSION and CHANGELOG on the project's main
# branch. Only ever read, only for the owner account, and switchable off
# (Configuration → Time → "Check for updates").
UPDATE_VERSION_URL = "https://raw.githubusercontent.com/cloudit24/open-feedback-forms/main/VERSION"
UPDATE_CHANGELOG_URL = "https://raw.githubusercontent.com/cloudit24/open-feedback-forms/main/CHANGELOG.md"
UPDATE_CACHE_TTL = 3600          # a new release shows within the hour
UPDATE_FRESH_MIN = 60            # "Check now" asks GitHub at most once a minute
_update_cache = {"at": 0, "latest": "", "notes": []}
_update_lock = threading.Lock()

HOST = os.environ.get("HOST", "127.0.0.1")
# Addresses that mean "every interface". They're fine to bind to but useless
# in a link, so a form's address is worked out from the request instead.
WILDCARD_HOSTS = ("0.0.0.0", "::", "[::]", "*", "")
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", os.environ.get("PORT", "8800")))
DEFAULT_FORM_PORT = int(os.environ.get("FORM_PORT", "8081"))
FORM_PORT_RANGE = (8090, 8189)          # auto-assign scans this range
# Which ports the outside world can actually reach. In Docker only published
# ports work, however happily the listener starts inside the container — so
# docker-compose.yml passes its published list here and the admin panel can
# say when a hand-picked port won't be reachable. Empty = no restriction
# (running outside Docker, where any free port works).
PUBLISHED_PORTS_RAW = os.environ.get("PUBLISHED_PORTS", "").strip()
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
# A theme (the app's, under Configuration -> Themes, or an account's own):
# colors plus a plain, gradient or wallpaper background.
THEME_STYLES = ("plain", "gradient", "wallpaper")
WALLPAPER_EXT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
WALLPAPER_FILE_RE = re.compile(r"^wallpaper_[0-9a-f]{16}\.(png|jpg|jpeg|webp)$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^\+[1-9][0-9]{6,17}$")
FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
CORE_KEYS = {"firstName", "lastName", "email", "consent", "language", "website", "turnstile"}

# The permission "checklist" a custom role is built from — see
# docs/ROLES_AND_PERMISSIONS.md. GLOBAL_PERMISSIONS only do anything for a
# role with applies_to_all_forms=True; FORM_PERMISSIONS apply to whichever
# forms a user is assigned (or every form, again under applies_to_all_forms).
GLOBAL_PERMISSIONS = {
    "manage_users", "manage_forms", "manage_field_keys",
    "manage_app_settings", "manage_global_alerts",
}
FORM_PERMISSIONS = {
    "view_dashboard", "view_submissions", "export_submissions",
    "edit_fields", "edit_branding", "manage_form_settings", "manage_form_alerts",
}
ALL_PERMISSIONS = GLOBAL_PERMISSIONS | FORM_PERMISSIONS


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

# token -> {"identity": {...}, "expires": epoch}. An identity is either
#   {"kind": "bootstrap", "username": ...}                          — the
#     config.json account: always full access, exactly as before roles
#     existed.
#   {"kind": "db", "user_id", "username", "permissions": set(...),
#    "form_ids": set(...) | "all"}                                  — an
#     admin_users row, resolved once at login (see db.get_admin_user_by_username)
#     so every later request is an in-memory check, never a DB round-trip.
_sessions = {}
_sessions_lock = threading.Lock()


def create_session(identity):
    token = secrets.token_hex(32)
    with _sessions_lock:
        _sessions[token] = {"identity": identity, "expires": time.time() + SESSION_TTL}
    return token


def session_identity(token):
    if not token:
        return None
    with _sessions_lock:
        s = _sessions.get(token)
        if not s or s["expires"] < time.time():
            _sessions.pop(token, None)
            return None
        return s["identity"]


def drop_session(token):
    with _sessions_lock:
        _sessions.pop(token, None)


def version_tuple(text):
    """"1.12.3" -> (1, 12, 3). Anything unparseable sorts lowest, so a
    mangled download can never look like a newer release."""
    parts = []
    for piece in (text or "").strip().split(".")[:3]:
        digits = "".join(c for c in piece if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def changelog_notes(markdown, current):
    """The bullet lines of every release newer than the one running."""
    notes, keep = [], False
    for line in (markdown or "").splitlines():
        if line.startswith("## "):
            keep = version_tuple(line[3:]) > version_tuple(current)
            continue
        if keep:
            stripped = line.strip()
            if stripped.startswith("- "):
                notes.append(stripped[2:].strip())
            elif notes and stripped and not stripped.startswith("#"):
                notes[-1] += " " + stripped          # a bullet wrapped onto the next line
    return notes[:12]


def fetch_update_info(fresh=False):
    """Latest published version + what it changes. Cached, fail-soft: any
    network trouble just means "no update known", never an error page.
    fresh = the owner clicked "Check now" (still no more than once a minute)."""
    with _update_lock:
        ttl = UPDATE_FRESH_MIN if fresh else UPDATE_CACHE_TTL
        if time.time() - _update_cache["at"] < ttl and _update_cache["latest"]:
            return _update_cache["latest"], _update_cache["notes"]
    latest, notes = "", []
    try:
        req = urllib.request.Request(UPDATE_VERSION_URL, headers={"User-Agent": "OpenFeedbackForms/%s" % VERSION})
        with urllib.request.urlopen(req, timeout=5) as resp:
            latest = resp.read(64).decode("utf-8", "replace").strip()
    except Exception as e:
        log("update check failed: %s" % e)
        return "", []
    if version_tuple(latest) > version_tuple(VERSION):
        # A missing or unreadable changelog still leaves a usable "there's a
        # new version" message — only the list of changes goes missing.
        try:
            req = urllib.request.Request(UPDATE_CHANGELOG_URL, headers={"User-Agent": "OpenFeedbackForms/%s" % VERSION})
            with urllib.request.urlopen(req, timeout=5) as resp:
                notes = changelog_notes(resp.read(20000).decode("utf-8", "replace"), VERSION)
        except Exception as e:
            log("update changelog fetch failed: %s" % e)
    with _update_lock:
        _update_cache.update(at=time.time(), latest=latest, notes=notes)
    return latest, notes


LIBRARY_TOPUP_MARKER = "library_keys_1_5_0"

DEFAULT_ADMIN_THEME = {"primary": "#0B5273", "text": "#1F2B33"}

# How a public form may look: light only, dark only, or both (follows the
# visitor's device, with a switch button).
THEME_MODES = ("both", "light", "dark")
# Form text styling (1.8.0). The font names are the keys the pages use; the
# .woff2 files live in public/fonts/ (self-hosted, no outside requests).
FORM_FONTS = ("system", "inter", "cairo", "tajawal", "plex", "kufi")
TEXT_SIZES = ("small", "normal", "large")
TITLE_ALIGNS = ("start", "center")
LAYOUT_MODES = ("single_page", "steps", "one_at_a_time")
TERMS_MODES = ("agree", "show", "off")
# Welcome / thank-you screen columns on `forms`: short text, styled text, link.
SCREEN_TEXT_COLS = ("welcome_title_en", "welcome_title_ar", "thanks_title_en", "thanks_title_ar",
                    "thanks_button_en", "thanks_button_ar",
                    "terms_title_en", "terms_title_ar", "consent_label_en", "consent_label_ar")
# Fixed wording of the public form that a form may replace with its own
# (see public/index.html, T.en / T.ar). Plain text only; shown with textContent.
UI_TEXT_KEYS = ("submit", "sending", "start", "next", "back", "again", "refLabel", "progress",
                "firstName", "lastName", "email", "optional", "choose", "scaleLow", "scaleHigh",
                "errRequired", "errFirst", "errLast", "errEmail", "errEmailFormat", "errPhoneFormat",
                "errShort", "errSelect", "errRating", "errConsent", "errFix", "errNet", "errRate", "errBot")


def parse_ui_text(raw):
    """{"en": {key: text}, "ar": {...}} with only known keys and short plain
    text; anything else is dropped. Accepts a dict or its JSON string."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    out = {}
    if not isinstance(raw, dict):
        return out
    for lng in ("en", "ar"):
        texts = raw.get(lng)
        if not isinstance(texts, dict):
            continue
        keep = {}
        for k in UI_TEXT_KEYS:
            v = texts.get(k)
            if isinstance(v, str):
                v = clean(v, 300)
                if v:
                    keep[k] = v
        if keep:
            out[lng] = keep
    return out
SCREEN_RICH_COLS = ("welcome_text_en", "welcome_text_ar", "thanks_text_en", "thanks_text_ar",
                    "terms_text_en", "terms_text_ar")
URL_RE = re.compile(r"^https?://[^\s<>\"']{1,490}$", re.I)
FONT_FILE_RE = re.compile(r"^[a-z0-9-]+\.woff2$")


def identity_theme_key(identity):
    """A stable name for "this account's own colors" in the config store. The
    primary admin lives in config.json rather than the users table, so it gets
    its own name instead of a user id."""
    if not identity:
        return None
    if identity["kind"] == "bootstrap":
        return "bootstrap"
    return "user:%s" % identity.get("user_id", identity["username"])


def wallpaper_base(owner):
    """File name (no extension) of a wallpaper: derived from its owner ("app"
    or an account), so a request can never name some other file."""
    import hashlib
    return "wallpaper_" + hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]


def clean_theme(body, old):
    """(theme, error). Keeps the old wallpaper so switching style and back
    needs no new upload. Older themes (just primary + text) stay valid."""
    if not isinstance(body, dict):
        return None, "invalid theme"
    primary = clean(body.get("primary"), 7)
    text = clean(body.get("text"), 7) or "#1F2B33"
    if not HEX_COLOR_RE.match(primary or "") or not HEX_COLOR_RE.match(text):
        return None, "colors must be a 6-digit hex code, like #0B5273"
    theme = {"primary": primary, "text": text}
    style = body.get("style") or "plain"
    if style not in THEME_STYLES:
        return None, "unknown background style"
    theme["style"] = style
    g1, g2 = clean(body.get("from"), 7), clean(body.get("to"), 7)
    if HEX_COLOR_RE.match(g1 or "") and HEX_COLOR_RE.match(g2 or ""):
        theme["from"], theme["to"] = g1, g2
    elif style == "gradient":
        return None, "gradient colors must be 6-digit hex codes"
    try:
        theme["angle"] = int(body.get("angle", 135)) % 360
    except (TypeError, ValueError):
        theme["angle"] = 135
    try:
        theme["dim"] = max(0, min(80, int(body.get("dim", 25))))
    except (TypeError, ValueError):
        theme["dim"] = 25
    old = old or {}
    if old.get("wallpaper"):
        theme["wallpaper"] = old["wallpaper"]
    if style == "wallpaper" and not theme.get("wallpaper"):
        return None, "upload a picture first"
    return theme, None


def theme_for_identity(cfg, identity):
    """The colors this account sees: its own if it picked some, otherwise the
    app-wide ones set under Configuration -> Appearance."""
    app_theme = cfg["settings"].get("admin_theme") or dict(DEFAULT_ADMIN_THEME)
    key = identity_theme_key(identity)
    own = (cfg["settings"].get("user_themes") or {}).get(key) if key else None
    return dict(own) if own else dict(app_theme)


def topup_library_once():
    """Questions added by a release reach an existing install's Field keys
    catalog the first time it runs that release, and never again — so a key
    an admin deleted stays deleted."""
    cfg = config_store.load()
    if cfg["settings"].get(LIBRARY_TOPUP_MARKER):
        return
    try:
        added, skipped = db.add_missing_library_keys()
        owned_added, owned_relabelled, owned_removed = db.replace_owned_library_keys()
    except Exception as e:
        log("could not top up the field key library: %s" % e)
        return
    cfg = config_store.load()
    cfg["settings"][LIBRARY_TOPUP_MARKER] = True
    config_store.save(cfg)
    if added:
        log("added new field keys to the library: %s" % ", ".join(added))
    if skipped:
        log("kept your own field keys instead of adding: %s" % "; ".join(skipped))
    if owned_added:
        log("added to the library: %s" % ", ".join(owned_added))
    if owned_relabelled:
        log("refreshed the English/Arabic labels of: %s" % ", ".join(owned_relabelled))
    if owned_removed:
        log("removed your duplicate catalog entries (forms and submissions untouched): %s"
            % ", ".join(owned_removed))


def drop_sessions_for(kind, key, keep_token=None):
    """Sign one account out everywhere, e.g. after its password changes.
    `key` is the username for the bootstrap account, user_id for a DB user."""
    field = "username" if kind == "bootstrap" else "user_id"
    with _sessions_lock:
        for token in [t for t, s in _sessions.items()
                      if t != keep_token and s["identity"]["kind"] == kind
                      and s["identity"].get(field) == key]:
            del _sessions[token]


# --------------------------------------------------------------- validation

def clean(value, limit):
    if not isinstance(value, str):
        return ""
    value = "".join(c for c in value if c >= " " or c in "\n\t")
    return value.strip()[:limit]


def form_look(form):
    """The title, description and text-styling settings of a form, as the
    pages need them. Descriptions are cleaned again on the way out, so even
    an old plain-text subtitle can never inject markup."""
    return {
        "formName": form.get("name") or "",
        "titleEn": (form.get("title_en") or "").strip(),
        "titleAr": (form.get("title_ar") or "").strip(),
        "descriptionEn": sanitise.sanitise(form.get("description_en")),
        "descriptionAr": sanitise.sanitise(form.get("description_ar")),
        "font": form.get("font") if form.get("font") in FORM_FONTS else "system",
        "textSize": form.get("text_size") if form.get("text_size") in TEXT_SIZES else "normal",
        "titleAlign": form.get("title_align") if form.get("title_align") in TITLE_ALIGNS else "start",
        "layoutMode": form.get("layout_mode") if form.get("layout_mode") in LAYOUT_MODES else "single_page",
        "hiddenFields": logic.parse_hidden_names(form.get("hidden_fields") or "")[0],
        "welcomeEnabled": bool(form.get("welcome_enabled")),
        "welcomeTitleEn": (form.get("welcome_title_en") or "").strip(),
        "welcomeTitleAr": (form.get("welcome_title_ar") or "").strip(),
        "welcomeTextEn": sanitise.sanitise(form.get("welcome_text_en")),
        "welcomeTextAr": sanitise.sanitise(form.get("welcome_text_ar")),
        "thanksTitleEn": (form.get("thanks_title_en") or "").strip(),
        "thanksTitleAr": (form.get("thanks_title_ar") or "").strip(),
        "thanksTextEn": sanitise.sanitise(form.get("thanks_text_en")),
        "thanksTextAr": sanitise.sanitise(form.get("thanks_text_ar")),
        "thanksButtonEn": (form.get("thanks_button_en") or "").strip(),
        "thanksButtonAr": (form.get("thanks_button_ar") or "").strip(),
        # only ever a plain http(s) address; anything else is dropped here too
        "thanksUrl": (form.get("thanks_url") or "").strip() if URL_RE.match((form.get("thanks_url") or "").strip()) else "",
        "termsTitleEn": (form.get("terms_title_en") or "").strip(),
        "termsTitleAr": (form.get("terms_title_ar") or "").strip(),
        "termsTextEn": sanitise.sanitise(form.get("terms_text_en")),
        "termsTextAr": sanitise.sanitise(form.get("terms_text_ar")),
        "consentLabelEn": (form.get("consent_label_en") or "").strip(),
        "consentLabelAr": (form.get("consent_label_ar") or "").strip(),
        "termsMode": form.get("terms_mode") if form.get("terms_mode") in TERMS_MODES else "agree",
        "uiText": parse_ui_text(form.get("ui_text_json") or ""),
    }


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:64] or "form"


def unique_slug(base, taken):
    """Slugs are unique per install, so a copy needs its own — "customer-feedback",
    then "customer-feedback-2", "customer-feedback-3"..."""
    slug = base or "form"
    if slug not in taken:
        return slug
    n = 2
    while True:
        candidate = "%s-%d" % (slug[:60], n)
        if candidate not in taken:
            return candidate
        n += 1


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


def validate_core(raw, require_core=True, require_consent=True):
    """The built-in block: first name, last name, email, consent. A form can
    turn the three name/email boxes off (ask_core_fields), in which case they
    arrive empty and are stored empty. Consent is required unless the form
    has no "I agree" box (terms_mode show/off); then it is stored as 0."""
    rec = {
        "firstName": clean(raw.get("firstName"), 60),
        "lastName":  clean(raw.get("lastName"), 60),
        "email":     clean(raw.get("email"), 120),
        "language":  "ar" if raw.get("language") == "ar" else "en",
    }
    if require_core:
        if len(rec["firstName"]) < 2:
            return None, "first name too short"
        if len(rec["lastName"]) < 2:
            return None, "last name too short"
        if not EMAIL_RE.match(rec["email"]):
            return None, "invalid email"
    else:
        # Nothing was asked, so nothing is kept — a stray value in the request
        # can't sneak into the record.
        rec["firstName"] = rec["lastName"] = rec["email"] = ""
    if require_consent and raw.get("consent") is not True:
        return None, "consent not given"
    rec["consent"] = bool(require_consent)
    return rec, None


# How a rating or dropdown question can be shown (see public/index.html).
# NULL/empty = the original look (stars / a dropdown).
DISPLAY_STYLES = {
    "rating": ("stars", "faces", "numbers"),
    "select": ("buttons", "scale", "grid"),
}


def clean_display_style(field_type, value):
    """The style to store for this question type, or None (the original look)."""
    value = (value or "").strip() if isinstance(value, str) else ""
    return value if value in DISPLAY_STYLES.get(field_type, ()) else None


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
    elif ftype == "date":
        # The browser's date box sends YYYY-MM-DD; anything else is someone
        # posting by hand. strptime also rejects impossible dates like 02-31.
        try:
            parsed = datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            return None, "invalid date for %s" % field["field_key"]
        if not (datetime(1900, 1, 1) <= parsed <= datetime(2100, 12, 31)):
            return None, "date out of range for %s" % field["field_key"]
    elif ftype == "select":
        valid = {o["value"] for o in (field["options"] or [])}
        if v not in valid:
            return None, "invalid option for %s" % field["field_key"]
    elif ftype == "textarea":
        if len(v) < 10:
            return None, "%s is too short" % field["field_key"]
    # 'text' — length cap already applied by clean(), nothing further to check

    return v, None


def form_is_expired(form):
    """True once today has reached the form's expiry date — same-day
    still counts as expired, not "expires later today"."""
    exp = form and form.get("expiry_date")
    if not exp:
        return False
    if isinstance(exp, str):
        exp = datetime.strptime(exp, "%Y-%m-%d").date()
    return exp <= datetime.now().date()


def current_form_state(form):
    """open / scheduled / closed_date / closed_limit for one form. Only forms
    with a response limit pay for a count."""
    n = 0
    if form and form.get("max_responses") is not None:
        try:
            n = db.count_responses(form["id"])
        except Exception as e:
            log("response count failed: %s" % e)
    return formstate.form_state(form, n)


def close_info(form, st):
    """What the public page needs to show a closed / not-yet-open notice."""
    reason = {formstate.SCHEDULED: "scheduled", formstate.CLOSED_DATE: "date",
              formstate.CLOSED_LIMIT: "limit"}[st["state"]]
    closed = reason != "scheduled"
    return {"reason": reason, "opensOn": st["opens_on"],
            "messageEn": sanitise.sanitise(form.get("closed_message_en" if closed else "opens_message_en")),
            "messageAr": sanitise.sanitise(form.get("closed_message_ar" if closed else "opens_message_ar"))}


def validate_submission(raw, active_fields, require_core=True, hidden_names=(), require_consent=True):
    if raw.get("website"):
        return None, "honeypot"

    core, err = validate_core(raw, require_core=require_core, require_consent=require_consent)
    if err:
        return None, err

    incoming = raw.get("fields")
    if not isinstance(incoming, dict):
        incoming = {}

    # Conditional questions: the rules are checked here again, never trusted
    # from the browser. A hidden question is not required and whatever was
    # posted for it is dropped, even when it looks valid.
    shown = logic.visible_keys(active_fields, incoming)

    extra = {}
    for field in active_fields:
        key = field["field_key"]
        if key not in shown:
            continue
        val, ferr = validate_dynamic_value(field, incoming.get(key))
        if ferr:
            return None, ferr
        if val not in (None, ""):
            extra[key] = val

    core["extra"] = extra
    core["hidden"] = logic.clean_hidden_values(raw.get("hidden"), hidden_names)
    return core, None


# ------------------------------------------------------------------- ports

def published_ports():
    """The set of ports reachable from outside, parsed from PUBLISHED_PORTS
    ("8800,8081,8090-8189"). Empty set means "no restriction known"."""
    out = set()
    for part in PUBLISHED_PORTS_RAW.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                out.update(range(int(lo), int(hi) + 1))
            except ValueError:
                continue
        else:
            try:
                out.add(int(part))
            except ValueError:
                continue
    return out


def port_reachable_warning(port):
    """A plain-English warning when a port the admin picked by hand starts
    fine but nothing outside the container can reach it."""
    allowed = published_ports()
    if not allowed or port in allowed:
        return None
    return ("Port %d is not published by Docker, so the form is running but "
            "can't be reached from outside. Use a port in %s, or publish %d "
            "in docker-compose.yml and run the installer again."
            % (port, PUBLISHED_PORTS_RAW, port))


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

    def admin_identity(self):
        c = self.cookies()
        if SESSION_COOKIE not in c:
            return None
        return session_identity(c[SESSION_COOKIE].value)

    def current_token(self):
        c = self.cookies()
        return c[SESSION_COOKIE].value if SESSION_COOKIE in c else None

    def admin_user(self):
        identity = self.admin_identity()
        return identity["username"] if identity else None

    def require_admin(self):
        """Any valid session, bootstrap or a custom-role user — no specific
        permission required. Use require_permission() instead wherever an
        action should actually be restricted by role."""
        identity = self.admin_identity()
        if not identity:
            self.send_json(401, {"ok": False, "error": "not authenticated"})
            return None
        return identity["username"]

    def has_permission(self, identity, key, form_id=None):
        """Non-response-writing check, so callers can combine several
        acceptable permissions (see require_any_permission)."""
        if identity["kind"] == "bootstrap":
            return True
        if key not in identity["permissions"]:
            return False
        if form_id is not None and identity["form_ids"] != "all" and int(form_id) not in identity["form_ids"]:
            return False
        return True

    def require_permission(self, key, form_id=None):
        """Returns the caller's identity dict on success. Sends 401/403 and
        returns None on failure — callers should `return` immediately on
        None, same as require_admin(). The bootstrap account always passes;
        a custom-role user needs `key` in their role's permissions and,
        when form_id is given, access to that specific form (either
        applies_to_all_forms, or that id in their assigned set)."""
        identity = self.admin_identity()
        if not identity:
            self.send_json(401, {"ok": False, "error": "not authenticated"})
            return None
        if not self.has_permission(identity, key, form_id):
            self.send_json(403, {"ok": False, "error": "you don't have permission to do that"})
            return None
        return identity

    def require_any_permission(self, options):
        """options: [(key, form_id), ...] — passes if any one matches.
        For actions two different permissions can each justify, e.g.
        editing a form's settings via either the global manage_forms or
        a per-form manage_form_settings grant."""
        identity = self.admin_identity()
        if not identity:
            self.send_json(401, {"ok": False, "error": "not authenticated"})
            return None
        if any(self.has_permission(identity, key, form_id) for key, form_id in options):
            return identity
        self.send_json(403, {"ok": False, "error": "you don't have permission to do that"})
        return None

    def require_form_access(self, form_id):
        """Any per-form permission at all on this form — for read views
        several different roles might legitimately need (e.g. the fields
        list, which a viewer, an editor, and an alert manager all see)."""
        return self.require_any_permission([(k, form_id) for k in FORM_PERMISSIONS])

    def require_bootstrap(self):
        """The config.json account specifically — for the Database tab,
        which has to stay reachable before any DB-backed role can exist."""
        identity = self.admin_identity()
        if not identity:
            self.send_json(401, {"ok": False, "error": "not authenticated"})
            return None
        if identity["kind"] != "bootstrap":
            self.send_json(403, {"ok": False, "error": "only the primary admin account can do that"})
            return None
        return identity

    def send_json(self, code, payload, cookie_header=None):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
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

    def is_https(self):
        """True when the visitor reached us over HTTPS. Behind a Cloudflare
        Tunnel or any reverse proxy the connection to this app is plain HTTP,
        so the proxy's header is what tells us."""
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return proto == "https" or (self.headers.get("X-Forwarded-Ssl") or "").lower() == "on"

    def session_cookie(self, token, max_age):
        # Secure only when the visit came over HTTPS — setting it always would
        # break a plain-HTTP install on a LAN, where the browser would drop it.
        flags = "Path=/; HttpOnly; SameSite=Strict"
        if self.is_https():
            flags += "; Secure"
        return "%s=%s; %s; Max-Age=%d" % (SESSION_COOKIE, token, flags, max_age)

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        # SAMEORIGIN: the admin panel shows a form's own page in its live
        # preview; no other site may frame either.
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; "
                         "script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; "
                         "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                         "font-src 'self' https://fonts.gstatic.com; "
                         "frame-src 'self' https://challenges.cloudflare.com; "
                         "frame-ancestors 'self'; "
                         "connect-src 'self' https://challenges.cloudflare.com; "
                         "img-src 'self' data:; base-uri 'none'; form-action 'self'")

    def fail(self, code, text="Not found"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
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
        self.send_header("X-Content-Type-Options", "nosniff")
        # An uploaded file is never trusted content. An SVG can carry script,
        # which would otherwise run on this site's own origin when the image
        # URL is opened directly: this CSP allows nothing at all, and sandbox
        # strips scripting even in browsers that ignore the rest.
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def serve_font(self, name):
        # Only plain *.woff2 names from public/fonts/ (no folders, no "..").
        if not FONT_FILE_RE.match(name):
            return self.fail(404)
        return self.static_from(os.path.join(PUBLIC, "fonts"), name)

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

class PublicRoutes:
    """One form's public pages and endpoints. Mixed into both listeners: the
    form's own port (PublicHandler) and the admin panel's port under
    /f/<slug>/, so a form can be published either way."""

    def public_get(self, form_id, path):
        if path == "/health":
            return self.send_json(200, {"ok": True, "formId": form_id})
        if path == "/api/form-fields":
            return self.get_form_fields(form_id)
        if path == "/api/ui-strings":
            return self.get_ui_strings()
        if path == "/logo":
            return self.serve_logo(form_id)
        if path in ("/", "/index.html"):
            return self.static_from(PUBLIC, "index.html")
        if path.startswith("/fonts/"):
            return self.serve_font(path[len("/fonts/"):])

        name = path.lstrip("/")
        if "/" in name or "\\" in name or ".." in name or not name:
            return self.fail(404)
        return self.static_from(PUBLIC, name)

    def public_post(self, form_id, path):
        if path == "/api/feedback":
            return self.post_feedback(form_id)
        return self.fail(404)

    def get_form_fields(self, form_id):
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "not configured"})
        try:
            fields = db.list_fields(form_id, active_only=True)
            form = db.get_form(form_id)
        except Exception as e:
            log("form-fields query failed: %s" % e)
            return self.send_json(500, {"ok": False})
        st = current_form_state(form) if form else None
        if st and st["state"] != formstate.OPEN:
            return self.send_json(200, {"ok": True, "expired": True, "closed": close_info(form, st), "fields": [],
                                        "branding": dict(form_look(form),
                                                         orgName=form.get("org_name") or form.get("name") or ""),
                                        "languages": {"en": True, "ar": True}, "extraLanguages": [],
                                        "languageLabels": config_store.load()["settings"].get("language_labels", {})})
        out = [{"id": f["id"], "field_key": f["field_key"], "label_en": f["label_en"],
                "label_ar": f["label_ar"], "labels_extra": f.get("labels_extra") or {},
                "field_type": f["field_type"],
                "display_style": clean_display_style(f["field_type"], f.get("display_style")),
                "options": f["options"], "required": f["required"],
                "show_if": logic.clean_show_if(f.get("show_if"))[0],
                "category": (f.get("category") or "").strip(),
                "page_break": bool(f.get("page_break_before"))} for f in fields]
        branding = {
            "orgName": (form and (form.get("org_name") or form.get("name"))) or "",
            "primaryColor": (form and form.get("primary_color")) or "#FFEC01",
            "inkColor": (form and form.get("ink_color")) or "#0B0B0B",
            "logoUrl": "logo" if (form and form.get("logo_filename")) else None,
            "themeMode": (form.get("theme_mode") or "both") if form else "both",
        }
        if form:
            branding.update(form_look(form))
        ask_core = bool(form.get("ask_core_fields", True)) if form else True
        languages = {
            "en": bool(form["lang_en"]) if form else True,
            "ar": bool(form["lang_ar"]) if form else True,
        }
        extraLangs = (form and form.get("enabled_extra_langs")) or []
        cfg = config_store.load()
        languageLabels = cfg["settings"].get("language_labels", {})
        return self.send_json(200, {"ok": True, "fields": out, "branding": branding,
                                    "languages": languages, "extraLanguages": extraLangs,
                                    "languageLabels": languageLabels, "askCoreFields": ask_core})

    def get_ui_strings(self):
        lang = self.query_one("lang", "")
        cfg = config_store.load()
        strings = cfg["settings"].get("ui_translations", {}).get(lang)
        if not strings:
            return self.send_json(404, {"ok": False, "error": "no cached translation for this language"})
        # Translations cached by older releases used a different name for the
        # organisation placeholder; hand them out under the current one.
        legacy = "{" + "cl" + "ub}"
        strings = {("org" if k == "cl" + "ub" else k): (v.replace(legacy, "{org}") if isinstance(v, str) else v)
                   for k, v in strings.items()}
        return self.send_json(200, {"ok": True, "strings": strings})

    def post_feedback(self, form_id):
        ip = self.client_ip()
        raw = self.read_json_body()
        if raw is None:
            return self.send_json(400, {"ok": False})

        if not rate_ok(ip, "attempts", ATTEMPT_LIMIT):
            log("flood guard tripped by %s" % ip)
            return self.send_json(429, {"ok": False})

        if not db.is_connected():
            return self.send_json(503, {"ok": False})

        form = db.get_form(form_id)
        st = current_form_state(form) if form else None
        if st and st["state"] != formstate.OPEN:
            return self.send_json(410, {"ok": False, "error": "this form is not accepting responses",
                                        "reason": st["state"]})

        try:
            active_fields = db.list_fields(form_id, active_only=True)
        except Exception as e:
            log("could not load fields: %s" % e)
            return self.send_json(500, {"ok": False})

        rec, err = validate_submission(raw, active_fields,
                                       require_core=bool(form and form.get("ask_core_fields", True)),
                                       hidden_names=logic.parse_hidden_names((form or {}).get("hidden_fields") or "")[0],
                                       require_consent=(form or {}).get("terms_mode", "agree") not in ("show", "off"))
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
            saved = db.save_feedback_full(form_id, rec)
        except db.FormClosedError:
            log("response limit reached on form %s; submission from %s refused" % (form_id, ip))
            return self.send_json(410, {"ok": False, "error": "this form is not accepting responses",
                                        "reason": formstate.CLOSED_LIMIT})
        except Exception as e:
            log("SAVE FAILED: %s" % e)
            return self.send_json(500, {"ok": False})
        ref = saved["reference"]

        log("saved %s from %s (form %s)" % (ref, ip, form_id))
        _fire_new_submission_alert(form_id, rec, ref)
        _queue_webhooks(form, saved, rec)
        return self.send_json(200, {"ok": True, "reference": ref})


class PublicHandler(PublicRoutes, BaseHandler):
    """Serves one form on its own port. FORM_ID is bound by
    make_public_handler()."""
    FORM_ID = None

    def do_GET(self):
        return self.public_get(self.FORM_ID, urllib.parse.urlparse(self.path).path)

    def do_POST(self):
        return self.public_post(self.FORM_ID, urllib.parse.urlparse(self.path).path)


def _fire_new_submission_alert(form_id, rec, ref):
    """Spawned as a daemon thread so a slow SMTP/Telegram call never delays
    the visitor's own response."""
    def run():
        try:
            form = db.get_form(form_id)
            cfg = config_store.load()
            subject = "New submission: %s" % (form["name"] if form else ("form #%d" % form_id))
            body = "%s %s <%s>\nReference: %s" % (rec.get("firstName", ""), rec.get("lastName", ""),
                                                    rec.get("email", ""), ref)
            notifier.fire("new_submission", cfg, log=log, form_id=form_id, subject=subject, body=body)
        except Exception as e:
            log("new-submission alert failed: %s" % e)
    threading.Thread(target=run, daemon=True).start()


def _queue_webhooks(form, saved, rec):
    """Put the new response in the webhook queue. Never raises: a problem
    here must not touch the visitor's own submission."""
    try:
        body = webhooks.build_body(form, saved["id"], saved["created_at"], rec.get("extra"), rec.get("hidden"))
        if db.enqueue_webhooks(form["id"], body.decode("utf-8")):
            webhooks.WAKE.set()
    except Exception as e:
        log("could not queue webhooks for form %s: %s" % (form.get("id"), e.__class__.__name__))


def _deliver_one(d):
    """Send one queued delivery, write the log line, then drop it or
    schedule the next try (1 min, 5 min, 30 min)."""
    attempt = d["attempt"] + 1
    try:
        if not d["active"]:
            db.finish_delivery(d["id"], True)
            return
        code, err = webhooks.send(d["url"], d["secret"], d["body"].encode("utf-8"), d["event"],
                                  bool(d["allow_local"]))
        ok = err is None
        db.log_webhook_attempt(d["webhook_id"], d["event"], attempt, code, ok, err)
        if ok or attempt > len(webhooks.RETRY_DELAYS):
            db.finish_delivery(d["id"], True)
        else:
            db.finish_delivery(d["id"], False,
                               next_try=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=webhooks.RETRY_DELAYS[attempt - 1]),
                               attempt=attempt)
    except Exception as e:
        log("webhook delivery %s failed: %s %s" % (d.get("id"), e.__class__.__name__, getattr(e, "errno", "")))


def _webhook_worker_loop():
    """Background sender. Wakes when a response arrives, and every few
    seconds to pick up retries that have come due. Survives a restart,
    because the queue lives in the database."""
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=4)
    while True:
        webhooks.WAKE.wait(5)
        webhooks.WAKE.clear()
        if not db.is_connected():
            continue
        try:
            due = db.claim_due_deliveries()
            if due:
                list(pool.map(_deliver_one, due))
        except Exception as e:
            log("webhook worker: %s %s" % (e.__class__.__name__, getattr(e, "errno", "")))


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

class AdminHandler(PublicRoutes, BaseHandler):
    # /f/<slug>/... serves a form on this same port and hostname, so one
    # tunnel or proxy hostname publishes every form. A form still has its own
    # port as well; this is the route for when publishing a port per form
    # isn't practical.
    FORM_PATH_RE = re.compile(r"^/f/([a-z0-9][a-z0-9-]{0,63})(/.*)?$")

    def public_form_target(self, path):
        """(form_id, sub_path) for a /f/<slug> request, or None. Sends the
        404/redirect itself when there's nothing to serve."""
        m = self.FORM_PATH_RE.match(path)
        if not m:
            return None
        slug, sub = m.group(1), m.group(2)
        if not db.is_connected():
            self.fail(503)
            return "handled"
        if sub is None:
            # Without the trailing slash the page's relative links would
            # resolve one level too high, so send the browser to the slash.
            self.send_response(301)
            query = urllib.parse.urlparse(self.path).query
            self.send_header("Location", "/f/%s/%s" % (slug, ("?" + query) if query else ""))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return "handled"
        form = db.get_form_by_slug(slug)
        if not form:
            self.fail(404)
            return "handled"
        return form["id"], sub

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        target = self.public_form_target(path)
        if target == "handled":
            return
        if target:
            return self.public_get(target[0], target[1])

        if path == "/health":
            return self.send_json(200, {"ok": True})
        if path in ("/", "/admin", "/admin/"):
            return self.static_from(ADMIN_DIR, "index.html")
        if path.startswith("/fonts/"):
            return self.serve_font(path[len("/fonts/"):])
        if path == "/admin/status":
            return self.get_admin_status()
        if path == "/admin/db-settings":
            return self.get_db_settings()
        if path == "/admin/backup":
            return self.get_backup()
        m = re.match(r"^/admin/wallpaper/([A-Za-z0-9_.]+)$", path)
        if m:
            return self.get_wallpaper(m.group(1))
        if path == "/admin/update-check":
            return self.get_update_check()
        if path == "/admin/app-settings":
            return self.get_app_settings()
        if path == "/admin/forms":
            return self.get_admin_forms()
        if path == "/admin/templates":
            return self.send_json(200, {"ok": True, "templates": [
                {k: t[k] for k in ("key", "name_en", "name_ar", "title_en", "title_ar",
                                    "description_en", "description_ar")} | {"questions": len(t["fields"])}
                for t in templates.TEMPLATES]})
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
        if path == "/admin/roles":
            return self.get_roles()
        if path == "/admin/users":
            return self.get_admin_users()
        if path == "/admin/alert-settings":
            return self.get_alert_settings()
        if path == "/admin/alert-rules":
            return self.get_alert_rules()

        m = re.match(r"^/admin/forms/(\d+)/summary$", path)
        if m:
            return self.get_form_summary(int(m.group(1)))
        if path == "/admin/submissions/stats":
            return self.get_admin_submission_stats()
        m = re.match(r"^/admin/submissions/(\d+)$", path)
        if m:
            return self.get_admin_submission(int(m.group(1)))
        m = re.match(r"^/admin/forms/(\d+)/webhooks$", path)
        if m:
            return self.get_form_webhooks(int(m.group(1)))
        m = re.match(r"^/admin/webhooks/(\d+)/log$", path)
        if m:
            return self.get_webhook_log(int(m.group(1)))
        m = re.match(r"^/admin/forms/(\d+)/branding$", path)
        if m:
            return self.get_admin_branding(int(m.group(1)))
        m = re.match(r"^/admin/forms/(\d+)/logo$", path)
        if m:
            if not self.require_form_access(int(m.group(1))):
                return
            return self.serve_logo(int(m.group(1)))

        return self.fail(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        target = self.public_form_target(path)
        if target == "handled":
            return
        if target:
            return self.public_post(target[0], target[1])

        routes = {
            "/admin/setup": self.post_admin_setup,
            "/admin/login": self.post_admin_login,
            "/admin/logout": self.post_admin_logout,
            "/admin/account/password": self.post_account_password,
            "/admin/account/theme": self.post_account_theme,
            "/admin/wallpaper": self.post_wallpaper,
            "/admin/port-test": self.post_port_test,
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
            "/admin/roles": self.post_role_create,
            "/admin/users": self.post_admin_user_create,
            "/admin/alert-settings": self.post_alert_settings,
            "/admin/alert-settings/test-smtp": self.post_smtp_test,
            "/admin/alert-rules": self.post_alert_rule_create,
        }
        if path in routes:
            return routes[path]()

        m = re.match(r"^/admin/roles/(\d+)/(update|delete)$", path)
        if m:
            role_id, action = int(m.group(1)), m.group(2)
            if action == "update":
                return self.post_role_update(role_id)
            return self.post_role_delete(role_id)

        m = re.match(r"^/admin/alert-rules/(\d+)/(update|delete)$", path)
        if m:
            rule_id, action = int(m.group(1)), m.group(2)
            if action == "update":
                return self.post_alert_rule_update(rule_id)
            return self.post_alert_rule_delete(rule_id)

        m = re.match(r"^/admin/users/(\d+)/(update|delete)$", path)
        if m:
            user_id, action = int(m.group(1)), m.group(2)
            if action == "update":
                return self.post_admin_user_update(user_id)
            return self.post_admin_user_delete(user_id)

        m = re.match(r"^/admin/forms/(\d+)/clone$", path)
        if m:
            return self.post_form_clone(int(m.group(1)))

        m = re.match(r"^/admin/forms/(\d+)/webhooks$", path)
        if m:
            return self.post_webhook_create(int(m.group(1)))
        m = re.match(r"^/admin/webhooks/(\d+)/(update|delete|regenerate|test)$", path)
        if m:
            return self.post_webhook_action(int(m.group(1)), m.group(2))

        m = re.match(r"^/admin/forms/(\d+)/(update|delete|restore|destroy)$", path)
        if m:
            form_id, action = int(m.group(1)), m.group(2)
            if action == "update":
                return self.post_form_update(form_id)
            if action == "restore":
                return self.post_form_restore(form_id)
            if action == "destroy":
                return self.post_form_destroy(form_id)
            return self.post_form_delete(form_id)

        m = re.match(r"^/admin/forms/(\d+)/branding$", path)
        if m:
            return self.post_form_branding(int(m.group(1)))

        m = re.match(r"^/admin/fields/(\d+)/page-break$", path)
        if m:
            return self.post_field_page_break(int(m.group(1)))

        m = re.match(r"^/admin/fields/(\d+)/label$", path)
        if m:
            return self.post_field_label(int(m.group(1)))

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
        identity = self.admin_identity()
        if identity and identity["kind"] == "bootstrap":
            permissions, allowed_forms = "all", "all"
        elif identity:
            permissions, allowed_forms = sorted(identity["permissions"]), (
                "all" if identity["form_ids"] == "all" else sorted(identity["form_ids"]))
        else:
            permissions, allowed_forms = [], []
        return self.send_json(200, {
            "ok": True,
            "adminConfigured": config_store.admin_configured(cfg),
            "dbConfigured": config_store.db_configured(cfg),
            "dbConnected": db.is_connected(),
            "dbError": db.last_error(),
            "authenticated": bool(identity),
            "username": identity["username"] if identity else None,
            "permissions": permissions,
            "allowedForms": allowed_forms,
            # Safe to expose pre-login (just colors) — the login/setup
            # screens need it too, not only the dashboard behind auth.
            "adminTheme": theme_for_identity(cfg, identity),
            # Whether those colors are this account's own pick or the app's.
            "adminThemeIsOwn": bool((cfg["settings"].get("user_themes") or {}).get(
                identity_theme_key(identity))) if identity else False,
            "version": VERSION,
        })

    def get_db_settings(self):
        if not self.require_bootstrap():
            return
        cfg = config_store.load()
        d = dict(cfg["db"])
        d["password"] = ""                              # never echo it back
        d["hasPassword"] = bool(cfg["db"]["password"])
        return self.send_json(200, {"ok": True, "db": d, "connected": db.is_connected(),
                                    "error": db.last_error()})

    def get_update_check(self):
        # Owner account only: it's the one that would run the update, and it
        # keeps this outbound request off everyone else's screen.
        if not self.require_bootstrap():
            return
        cfg = config_store.load()
        if not cfg["settings"].get("update_check", True):
            return self.send_json(200, {"ok": True, "enabled": False, "current": VERSION})
        latest, notes = fetch_update_info(fresh=self.query_one("fresh") == "1")
        return self.send_json(200, {
            "ok": True, "enabled": True, "current": VERSION, "latest": latest,
            "updateAvailable": bool(latest) and version_tuple(latest) > version_tuple(VERSION),
            "notes": notes,
            "changelogUrl": "https://github.com/cloudit24/open-feedback-forms/blob/main/CHANGELOG.md",
        })

    def get_backup(self):
        # Full-admin only: config.json carries the DB password in plain
        # text, and the zip is everything needed to stand the app back up.
        if not self.require_bootstrap():
            return
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            if os.path.isfile(config_store.CONFIG_PATH):
                zf.write(config_store.CONFIG_PATH, arcname="config.json")
            uploads = config_store.uploads_dir()
            if os.path.isdir(uploads):
                for fname in os.listdir(uploads):
                    fpath = os.path.join(uploads, fname)
                    if os.path.isfile(fpath):
                        zf.write(fpath, arcname="uploads/logos/" + fname)
            if db.is_connected():
                try:
                    dump = db.export_all_tables()
                    zf.writestr("database.json", json.dumps(dump, indent=2, default=str, ensure_ascii=False))
                except Exception as e:
                    zf.writestr("database-export-failed.txt", "Could not export the database: %s" % e)
            else:
                zf.writestr("database-export-failed.txt",
                             "Database was not connected at backup time — nothing was exported.")
        body = buf.getvalue()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="off-backup-%s.zip"' % stamp)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        log("backup downloaded by %s" % self.client_ip())

    def get_app_settings(self):
        if not self.require_permission("manage_app_settings"):
            return
        cfg = config_store.load()
        s = cfg.get("settings", {"timezone": "UTC", "ntp_server": "pool.ntp.org"})
        return self.send_json(200, {"ok": True, "settings": s,
                                    "timezones": sorted(zoneinfo.available_timezones())})

    def post_app_settings(self):
        if not self.require_permission("manage_app_settings"):
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
            clean_t, err = clean_theme(theme, cfg["settings"].get("admin_theme"))
            if err:
                return self.send_json(400, {"ok": False, "error": err})
            cfg["settings"]["admin_theme"] = clean_t
        if "translate_api_key" in body:
            cfg["settings"]["translate_api_key"] = clean(body.get("translate_api_key"), 200)
        if "update_check" in body:
            cfg["settings"]["update_check"] = bool(body.get("update_check"))
        cfg["settings"]["timezone"] = tz
        cfg["settings"]["ntp_server"] = ntp
        config_store.save(cfg)
        return self.send_json(200, {"ok": True})

    def post_language_labels(self):
        if not self.require_permission("manage_app_settings"):
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
        if not self.require_permission("manage_app_settings"):
            return
        cfg = config_store.load()
        endpoint = cfg["settings"].get("translate_endpoint")
        langs = translate_client.list_languages(endpoint)
        registered = set(cfg["settings"].get("language_labels", {}))
        # Already-added languages (including en/ar) don't need offering again.
        langs = [l for l in langs if l["code"] not in registered]
        return self.send_json(200, {"ok": True, "languages": langs})

    def post_language_labels_add(self):
        if not self.require_permission("manage_app_settings"):
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
        if not self.require_permission("manage_app_settings"):
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

    # ---- global alert settings (SMTP/Telegram infra + db_disconnected) ----

    def get_alert_settings(self):
        if not self.require_permission("manage_global_alerts"):
            return
        cfg = config_store.load()
        smtp = dict(cfg["settings"].get("smtp", {}))
        smtp["password"] = ""                              # never echo it back
        smtp["hasPassword"] = bool(cfg["settings"].get("smtp", {}).get("password"))
        telegram = dict(cfg["settings"].get("telegram", {}))
        telegram["hasBotToken"] = bool(telegram.get("bot_token"))
        telegram["bot_token"] = ""
        return self.send_json(200, {"ok": True, "smtp": smtp, "telegram": telegram,
                                    "dbDisconnectedAlerts": cfg["settings"].get("db_disconnected_alerts", [])})

    def post_smtp_test(self):
        # Tests the values as typed in the form, before saving; a blank
        # password means the saved one, same as Save.
        if not self.require_permission("manage_global_alerts"):
            return
        if not rate_ok(self.client_ip(), "smtp_test", 10):
            return self.send_json(429, {"ok": False, "error": "too many test emails, try again later"})
        body = self.read_json_body() or {}
        s = body.get("smtp") if isinstance(body.get("smtp"), dict) else {}
        to_addr = clean(body.get("to"), 200)
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to_addr):
            return self.send_json(400, {"ok": False, "error": "enter a valid email address to send the test to"})
        host = clean(s.get("host"), 200)
        if not host:
            return self.send_json(400, {"ok": False, "error": "enter the SMTP host first"})
        saved = config_store.load()["settings"].get("smtp", {})
        try:
            port = int(s.get("port") or 587)
        except (TypeError, ValueError):
            return self.send_json(400, {"ok": False, "error": "port must be a number"})
        smtp_cfg = {
            "host": host, "port": port, "user": clean(s.get("user"), 200),
            "password": s.get("password") or saved.get("password", ""),
            "from": clean(s.get("from"), 200), "from_name": clean(s.get("from_name"), 120),
            "use_tls": bool(s.get("use_tls", True)),
        }
        try:
            notifier.deliver_email(smtp_cfg, to_addr, "Open Feedback Forms — SMTP test",
                                   "This is a test email from Open Feedback Forms.\n\n"
                                   "If you can read this, the SMTP settings work and email alerts will be delivered.")
        except Exception as e:
            log("smtp test to %s failed: %s" % (to_addr, e))
            return self.send_json(200, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
        log("smtp test email sent to %s by %s" % (to_addr, self.admin_user()))
        return self.send_json(200, {"ok": True})

    def post_alert_settings(self):
        if not self.require_permission("manage_global_alerts"):
            return
        body = self.read_json_body() or {}
        cfg = config_store.load()
        if "smtp" in body:
            s = body["smtp"] or {}
            if not isinstance(s, dict):
                return self.send_json(400, {"ok": False, "error": "invalid smtp settings"})
            existing = cfg["settings"].get("smtp", {})
            cfg["settings"]["smtp"] = {
                "host": clean(s.get("host"), 200), "port": int(s.get("port") or 587),
                "user": clean(s.get("user"), 200),
                "password": s.get("password") or existing.get("password", ""),   # blank = keep saved
                "from": clean(s.get("from"), 200), "from_name": clean(s.get("from_name"), 120),
                "use_tls": bool(s.get("use_tls", True)),
            }
        if "telegram" in body:
            t = body["telegram"] or {}
            if not isinstance(t, dict):
                return self.send_json(400, {"ok": False, "error": "invalid telegram settings"})
            existing = cfg["settings"].get("telegram", {})
            cfg["settings"]["telegram"] = {
                "bot_token": clean(t.get("bot_token"), 200) or existing.get("bot_token", ""),
            }
        if "dbDisconnectedAlerts" in body:
            alerts = body["dbDisconnectedAlerts"]
            if not isinstance(alerts, list):
                return self.send_json(400, {"ok": False, "error": "dbDisconnectedAlerts must be a list"})
            cleaned = []
            for a in alerts:
                if not isinstance(a, dict) or a.get("channel") not in ("email", "telegram"):
                    continue
                dest = clean(a.get("destination"), 200)
                if not dest:
                    continue
                cleaned.append({"channel": a["channel"], "destination": dest,
                                "enabled": bool(a.get("enabled", True))})
            cfg["settings"]["db_disconnected_alerts"] = cleaned
        config_store.save(cfg)
        return self.send_json(200, {"ok": True})

    # ---- per-form alert rules ---------------------------------------------

    def get_alert_rules(self):
        try:
            form_id = int(self.query_one("form_id") or 0)
        except ValueError:
            form_id = 0
        if not form_id:
            return self.send_json(400, {"ok": False, "error": "form_id is required"})
        if not self.require_permission("manage_form_alerts", form_id):
            return
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        return self.send_json(200, {"ok": True, "rules": db.list_alert_rules(form_id=form_id)})

    def _validate_alert_rule_payload(self, body):
        trigger = body.get("trigger_type")
        if trigger not in ("new_submission", "daily_digest", "form_expiry"):
            return "invalid trigger type", None
        channel = body.get("channel")
        if channel not in ("email", "telegram"):
            return "invalid channel", None
        # Deliberately not required here — the admin UI creates a blank
        # rule row and lets the admin fill in the destination inline
        # afterward (see openFormDetail/add-form-alert-btn). An empty
        # destination just means notifier.send() no-ops on that rule.
        destination = clean(body.get("destination"), 200)
        enabled = bool(body.get("enabled", True))
        return None, (trigger, channel, destination, enabled)

    def post_alert_rule_create(self):
        body = self.read_json_body() or {}
        try:
            form_id = int(body.get("form_id") or 0)
        except (TypeError, ValueError):
            form_id = 0
        if not form_id:
            return self.send_json(400, {"ok": False, "error": "form_id is required"})
        if not self.require_permission("manage_form_alerts", form_id):
            return
        err, parsed = self._validate_alert_rule_payload(body)
        if err:
            return self.send_json(400, {"ok": False, "error": err})
        trigger, channel, destination, enabled = parsed
        new_id = db.create_alert_rule(form_id, trigger, channel, destination, enabled)
        return self.send_json(200, {"ok": True, "id": new_id})

    def post_alert_rule_update(self, rule_id):
        existing = db.get_alert_rule(rule_id)
        if not existing:
            return self.send_json(404, {"ok": False, "error": "alert rule not found"})
        if not self.require_permission("manage_form_alerts", existing["form_id"]):
            return
        body = self.read_json_body() or {}
        trigger_type = body.get("trigger_type") if body.get("trigger_type") in \
            ("new_submission", "daily_digest", "form_expiry") else None
        channel = body.get("channel") if body.get("channel") in ("email", "telegram") else None
        # Empty destination is allowed (see _validate_alert_rule_payload) —
        # the rule just does nothing until filled in.
        destination = clean(body.get("destination"), 200) if "destination" in body else None
        enabled = bool(body.get("enabled")) if "enabled" in body else None
        db.update_alert_rule(rule_id, trigger_type=trigger_type, channel=channel,
                             destination=destination, enabled=enabled)
        return self.send_json(200, {"ok": True})

    def post_alert_rule_delete(self, rule_id):
        existing = db.get_alert_rule(rule_id)
        if not existing:
            return self.send_json(404, {"ok": False, "error": "alert rule not found"})
        if not self.require_permission("manage_form_alerts", existing["form_id"]):
            return
        db.delete_alert_rule(rule_id)
        return self.send_json(200, {"ok": True})

    def post_db_test(self):
        if not self.require_bootstrap():
            return
        body = self.read_json_body() or {}
        db_cfg = self._db_cfg_from_body(body)
        try:
            db.test_connection(db_cfg)
        except Exception as e:
            return self.send_json(200, {"ok": False, "error": str(e)})
        return self.send_json(200, {"ok": True})

    def post_db_settings(self):
        if not self.require_bootstrap():
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
        topup_library_once()
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

    def link_host(self):
        """A host a browser can actually open. HOST is what we bind to, which
        in Docker is 0.0.0.0 — no use in a link — so fall back to the host
        this admin page was reached on (the tunnel or proxy hostname)."""
        if HOST not in WILDCARD_HOSTS:
            return HOST
        raw = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "")
        raw = raw.split(",")[0].strip()
        if raw.startswith("["):                       # [::1]:8080
            host = raw.split("]")[0] + "]"
        else:
            host = raw.split(":")[0]
        return host or "localhost"

    def form_url(self, form):
        """Where the public reaches this form. A Cloudflare Tunnel or reverse
        proxy publishes it on its own hostname, which only the admin knows —
        so if one has been set on the form, that's the link."""
        public = (form.get("public_url") or "").strip()
        if public:
            return public if public.endswith("/") else public + "/"
        return "http://%s:%d/" % (self.link_host(), form["port"])

    def get_admin_forms(self):
        if not self.require_admin():
            return
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        forms = self._forms_visible_to(self.admin_identity())
        running = FORMS.status()
        try:
            counts = db.response_counts([f["id"] for f in forms if f.get("max_responses") is not None])
        except Exception:
            counts = {}
        out = [{
            "id": f["id"], "name": f["name"], "slug": f["slug"], "port": f["port"],
            "active": f["active"], "listening": f["id"] in running,
            "url": self.form_url(f),
            "public_url": f.get("public_url") or "",
            "ask_core_fields": bool(f.get("ask_core_fields", True)),
            "lang_en": f["lang_en"], "lang_ar": f["lang_ar"],
            "enabled_extra_langs": f.get("enabled_extra_langs") or [],
            "expiry_date": str(f["expiry_date"]) if f.get("expiry_date") else None,
            "opens_on": str(f["opens_on"]) if f.get("opens_on") else None,
            "max_responses": f.get("max_responses"),
            "response_count": counts.get(f["id"], 0) if f.get("max_responses") is not None else None,
            "status": formstate.form_state(f, counts.get(f["id"], 0))["state"],
            "closed_message_en": sanitise.sanitise(f.get("closed_message_en")),
            "closed_message_ar": sanitise.sanitise(f.get("closed_message_ar")),
            "opens_message_en": sanitise.sanitise(f.get("opens_message_en")),
            "opens_message_ar": sanitise.sanitise(f.get("opens_message_ar")),
            "created_at": str(f["created_at"]) if f.get("created_at") else None,
        } for f in forms]
        return self.send_json(200, {"ok": True, "forms": out, "adminPort": ADMIN_PORT,
                                    "publishedPorts": PUBLISHED_PORTS_RAW,
                                    "autoPortRange": "%d-%d" % FORM_PORT_RANGE})

    def post_form_create(self):
        if not self.require_permission("manage_forms"):
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

        err, expiry_date = self._parse_expiry_date(body.get("expiry_date"))
        if err:
            return self.send_json(400, {"ok": False, "error": err})

        err, public_url = self._parse_public_url(body.get("public_url"))
        if err:
            return self.send_json(400, {"ok": False, "error": err})

        template_key = clean(body.get("template"), 40) or None
        if template_key and not templates.get(template_key):
            return self.send_json(400, {"ok": False, "error": "unknown template"})

        try:
            new_id = db.create_form(name, slug, port, lang_en=lang_en, lang_ar=lang_ar,
                                    enabled_extra_langs=extra_langs, expiry_date=expiry_date,
                                    public_url=public_url, template_key=template_key,
                                    # A new form starts with nothing but what
                                    # the admin picks, the built-in name/email
                                    # block included. Forms that already exist
                                    # keep theirs switched on.
                                    ask_core_fields=bool(body.get("ask_core_fields", False)))
        except Exception as e:
            return self.send_json(400, {"ok": False, "error": str(e)})
        FORMS.sync()
        log("form created: %s (port %d)" % (name, port))
        return self.send_json(200, {"ok": True, "id": new_id, "port": port,
                                    "warning": port_reachable_warning(port)})

    def post_form_clone(self, form_id):
        """A copy of this form on its own port: same languages, branding and
        questions, no submissions. Its name gets "(copy)" and the admin can
        rename it straight away."""
        if not self.require_permission("manage_forms"):
            return
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        source = db.get_form(form_id)
        if not source:
            return self.send_json(404, {"ok": False, "error": "form not found"})

        body = self.read_json_body() or {}
        name = clean(body.get("name"), 120) or ("%s (copy)" % source["name"])[:120]
        port_err, port = self._resolve_new_port(body.get("port"))
        if port_err:
            return self.send_json(400, {"ok": False, "error": port_err})

        taken = {f["slug"] for f in db.list_forms()}
        slug = unique_slug(slugify(name), taken)
        try:
            result = db.clone_form(form_id, name, slug, port)
        except Exception as e:
            return self.send_json(400, {"ok": False, "error": str(e)})

        # The logo is a file on disk, not a database row — copy it too, under
        # the new form's own name, so deleting either form leaves the other's
        # logo alone.
        logo = result.get("logo_filename")
        if logo:
            try:
                updir = config_store.uploads_dir()
                ext = logo.rsplit(".", 1)[-1]
                new_logo = "form_%d.%s" % (result["id"], ext)
                shutil.copyfile(os.path.join(updir, logo), os.path.join(updir, new_logo))
                db.update_branding(result["id"], logo_filename=new_logo)
            except OSError as e:
                log("could not copy the logo for the copy of '%s': %s" % (source["name"], e))

        FORMS.sync()
        log("form cloned: %s -> %s (port %d)" % (source["name"], name, port))
        return self.send_json(200, {
            "ok": True, "id": result["id"], "port": port, "name": name,
            "fields": result["fields"],
            "warning": port_reachable_warning(port),
            "note": "Questions and branding were copied. Submissions and alert rules were not.",
        })

    def _parse_public_url(self, value):
        """"" / None clears it. Anything else must be a plain http(s) address
        with no path of its own. Returns (error, value_or_None)."""
        if value in (None, ""):
            return None, None
        text = clean(value, 300).rstrip("/")
        if not re.match(r"^https?://[A-Za-z0-9._~:\[\]-]+$", text):
            return "public URL must look like https://feedback.example.com", None
        return None, text

    def _parse_expiry_date(self, value):
        """value: "" / None clears it, "YYYY-MM-DD" sets it. Returns
        (error, parsed_value_or_None)."""
        if value in (None, ""):
            return None, None
        text = clean(value, 10)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            return "expiry date must be YYYY-MM-DD", None
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return "invalid expiry date", None
        return None, text

    def _forms_visible_to(self, identity, forms=None):
        """The bootstrap account and any applies_to_all_forms role see
        every form; anyone else only sees the ones they're assigned."""
        forms = db.list_forms(active_only=False) if forms is None else forms
        if identity["kind"] == "bootstrap" or identity["form_ids"] == "all":
            return forms
        return [f for f in forms if f["id"] in identity["form_ids"]]

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
        if not self.require_any_permission([("manage_forms", None), ("manage_form_settings", form_id)]):
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

        update_kwargs = {}
        if "ask_core_fields" in body:
            update_kwargs["ask_core_fields"] = bool(body.get("ask_core_fields"))
        if "public_url" in body:
            err, public_url = self._parse_public_url(body.get("public_url"))
            if err:
                return self.send_json(400, {"ok": False, "error": err})
            update_kwargs["public_url"] = public_url
        if "expiry_date" in body:
            err, expiry_date = self._parse_expiry_date(body.get("expiry_date"))
            if err:
                return self.send_json(400, {"ok": False, "error": err})
            update_kwargs["expiry_date"] = expiry_date
        if "opens_on" in body:
            err, opens_on = self._parse_expiry_date(body.get("opens_on"))
            if err:
                return self.send_json(400, {"ok": False, "error": err.replace("expiry", "opening")})
            update_kwargs["opens_on"] = opens_on
        if "max_responses" in body:
            raw_max = body.get("max_responses")
            if raw_max in (None, ""):
                update_kwargs["max_responses"] = None
            else:
                try:
                    n = int(str(raw_max).strip())
                except ValueError:
                    n = 0
                if n < 1 or n > 10000000:
                    return self.send_json(400, {"ok": False, "error": "the response limit must be a whole number from 1 up (or empty for no limit)"})
                update_kwargs["max_responses"] = n
        # the opening date has to come before the expiry date
        o = update_kwargs["opens_on"] if "opens_on" in update_kwargs else form.get("opens_on")
        x = update_kwargs["expiry_date"] if "expiry_date" in update_kwargs else form.get("expiry_date")
        if o and x and str(o) >= str(x):
            return self.send_json(400, {"ok": False, "error": "the opening date must be before the expiry date"})
        close_texts = {}
        for col in db.CLOSE_TEXT_COLUMNS:
            if col in body:
                raw = body.get(col)
                if not isinstance(raw, str) or len(raw) > 6000:
                    return self.send_json(400, {"ok": False, "error": "that message is too long"})
                safe = sanitise.sanitise(raw)
                if len(safe) > 4000:
                    return self.send_json(400, {"ok": False, "error": "that message is too long"})
                close_texts[col] = safe
        if close_texts:
            update_kwargs["close_texts"] = close_texts

        db.update_form(form_id, name=name, port=port, lang_en=lang_en, lang_ar=lang_ar,
                        enabled_extra_langs=extra_langs, **update_kwargs)
        FORMS.sync()
        return self.send_json(200, {"ok": True, "warning": port_reachable_warning(port)})

    def post_form_delete(self, form_id):
        if not self.require_permission("manage_forms"):
            return
        db.set_form_active(form_id, False)
        FORMS.sync()
        return self.send_json(200, {"ok": True})

    def post_form_restore(self, form_id):
        if not self.require_permission("manage_forms"):
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

    def post_form_destroy(self, form_id):
        """Permanent delete — only for a form that's already archived
        (inactive). The admin UI requires an explicit confirmation and
        offers a CSV export first; this endpoint itself just enforces the
        one hard rule (must already be archived) and does the irreversible
        part."""
        if not self.require_permission("manage_forms"):
            return
        form = db.get_form(form_id)
        if not form:
            return self.send_json(404, {"ok": False, "error": "form not found"})
        if form["active"]:
            return self.send_json(400, {"ok": False,
                "error": "archive this form first (Disable) before permanently deleting it"})
        logo_filename = db.delete_form_permanently(form_id)
        if logo_filename:
            try:
                os.remove(os.path.join(config_store.uploads_dir(), logo_filename))
            except OSError:
                pass
        FORMS.sync()
        log("form permanently deleted: %s (#%d)" % (form["name"], form_id))
        return self.send_json(200, {"ok": True})

    # ---- branding ---------------------------------------------------------

    # ---- webhooks (same permission as editing the form) ------------------

    def _webhook_permission(self, form_id):
        return self.require_any_permission([("manage_forms", None), ("manage_form_settings", form_id)])

    def _webhook_public(self, w):
        """A webhook as the admin sees it: never the secret."""
        return {"id": w["id"], "url": w["url"], "allow_local": w["allow_local"], "active": w["active"],
                "last": w.get("last"), "waiting": w.get("waiting", 0)}

    def get_form_webhooks(self, form_id):
        if not self._webhook_permission(form_id):
            return
        if not db.get_form(form_id):
            return self.send_json(404, {"ok": False, "error": "form not found"})
        return self.send_json(200, {"ok": True, "webhooks": [self._webhook_public(w) for w in db.list_webhooks(form_id)]})

    def get_webhook_log(self, webhook_id):
        w = db.get_webhook(webhook_id)
        if not w:
            return self.send_json(404, {"ok": False, "error": "webhook not found"})
        if not self._webhook_permission(w["form_id"]):
            return
        return self.send_json(200, {"ok": True, "log": db.webhook_log(webhook_id)})

    def _check_webhook_target(self, url, allow_local):
        """(error, cleaned url). Also refuses an address that already points
        inside the network; a name that can't be found yet is let through
        (the sender checks again every time)."""
        err, parts = webhooks.check_url(url)
        if err:
            return err, None
        if not allow_local:
            try:
                webhooks.resolve(parts[1], parts[2], False)
            except webhooks.WebhookError as e:
                if str(e).startswith("blocked"):
                    return str(e), None
        return None, url.strip()

    def post_webhook_create(self, form_id):
        if not self._webhook_permission(form_id):
            return
        if not db.get_form(form_id):
            return self.send_json(404, {"ok": False, "error": "form not found"})
        body = self.read_json_body() or {}
        allow_local = bool(body.get("allow_local"))
        err, url = self._check_webhook_target(body.get("url"), allow_local)
        if err:
            return self.send_json(400, {"ok": False, "error": err})
        if db.count_webhooks(form_id) >= webhooks.MAX_PER_FORM:
            return self.send_json(400, {"ok": False, "error": "a form can have at most %d webhooks" % webhooks.MAX_PER_FORM})
        secret = webhooks.new_secret()
        wid = db.create_webhook(form_id, url, secret, allow_local)
        log("webhook %s added to form %s" % (wid, form_id))
        # The secret is shown this one time only.
        return self.send_json(200, {"ok": True, "id": wid, "secret": secret})

    def post_webhook_action(self, webhook_id, action):
        w = db.get_webhook(webhook_id)
        if not w:
            return self.send_json(404, {"ok": False, "error": "webhook not found"})
        if not self._webhook_permission(w["form_id"]):
            return
        if action == "delete":
            db.delete_webhook(webhook_id)
            log("webhook %s deleted" % webhook_id)
            return self.send_json(200, {"ok": True})
        if action == "regenerate":
            secret = webhooks.new_secret()
            db.update_webhook(webhook_id, secret=secret)
            log("webhook %s secret regenerated" % webhook_id)
            return self.send_json(200, {"ok": True, "secret": secret})
        if action == "update":
            body = self.read_json_body() or {}
            allow_local = bool(body["allow_local"]) if "allow_local" in body else w["allow_local"]
            url = None
            if "url" in body or "allow_local" in body:
                err, url = self._check_webhook_target(body.get("url", w["url"]), allow_local)
                if err:
                    return self.send_json(400, {"ok": False, "error": err})
            db.update_webhook(webhook_id, url=url if "url" in body else None,
                              allow_local=allow_local if "allow_local" in body else None,
                              active=bool(body["active"]) if "active" in body else None)
            return self.send_json(200, {"ok": True})
        # action == "test": sent right now, so the admin sees the answer
        full = db.get_webhook(webhook_id, with_secret=True)
        form = db.get_form(w["form_id"])
        sample = webhooks.build_body(form, 0, datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                     {"example_question": "Example answer"}, {}, event="test")
        code, err = webhooks.send(full["url"], full["secret"], sample, "test", full["allow_local"])
        db.log_webhook_attempt(webhook_id, "test", 1, code, err is None, err)
        return self.send_json(200, {"ok": True, "sent": err is None, "status_code": code, "error": err})

    def get_admin_branding(self, form_id):
        if not self.require_permission("edit_branding", form_id):
            return
        form = db.get_form(form_id)
        if not form:
            return self.send_json(404, {"ok": False, "error": "form not found"})
        return self.send_json(200, {"ok": True, "branding": {
            "org_name": form.get("org_name") or "",
            "primary_color": form.get("primary_color") or "#FFEC01",
            "ink_color": form.get("ink_color") or "#0B0B0B",
            "title_en": form.get("title_en") or "",
            "title_ar": form.get("title_ar") or "",
            "description_en": sanitise.sanitise(form.get("description_en")),
            "description_ar": sanitise.sanitise(form.get("description_ar")),
            "font": form_look(form)["font"],
            "text_size": form_look(form)["textSize"],
            "title_align": form_look(form)["titleAlign"],
            "layout_mode": form_look(form)["layoutMode"],
            "welcome_enabled": bool(form.get("welcome_enabled")),
            "welcome_title_en": form.get("welcome_title_en") or "",
            "welcome_title_ar": form.get("welcome_title_ar") or "",
            "welcome_text_en": sanitise.sanitise(form.get("welcome_text_en")),
            "welcome_text_ar": sanitise.sanitise(form.get("welcome_text_ar")),
            "thanks_title_en": form.get("thanks_title_en") or "",
            "thanks_title_ar": form.get("thanks_title_ar") or "",
            "thanks_text_en": sanitise.sanitise(form.get("thanks_text_en")),
            "thanks_text_ar": sanitise.sanitise(form.get("thanks_text_ar")),
            "thanks_button_en": form.get("thanks_button_en") or "",
            "thanks_button_ar": form.get("thanks_button_ar") or "",
            "thanks_url": form_look(form)["thanksUrl"],
            "terms_title_en": form.get("terms_title_en") or "",
            "terms_title_ar": form.get("terms_title_ar") or "",
            "terms_text_en": sanitise.sanitise(form.get("terms_text_en")),
            "terms_text_ar": sanitise.sanitise(form.get("terms_text_ar")),
            "consent_label_en": form.get("consent_label_en") or "",
            "consent_label_ar": form.get("consent_label_ar") or "",
            "terms_mode": form_look(form)["termsMode"],
            "ui_text": parse_ui_text(form.get("ui_text_json") or ""),
            "hidden_fields": ", ".join(form_look(form)["hiddenFields"]),
            "form_name": form.get("name") or "",
            "theme_mode": form.get("theme_mode") or "both",
            "hasLogo": bool(form.get("logo_filename")),
        }})

    def post_form_branding(self, form_id):
        if not self.require_permission("edit_branding", form_id):
            return
        form = db.get_form(form_id)
        if not form:
            return self.send_json(404, {"ok": False, "error": "form not found"})
        body = self.read_json_body(max_body=MAX_LOGO_BODY) or {}

        org_name = body.get("org_name")
        if org_name is not None:
            org_name = clean(org_name, 120)

        title_en = body.get("title_en")
        if title_en is not None:
            title_en = clean(title_en, 200)
        title_ar = body.get("title_ar")
        if title_ar is not None:
            title_ar = clean(title_ar, 200)
        # Styled text: cleaned with an allow-list before it is ever stored.
        # An emptied description is saved as "" (not NULL) on purpose.
        descriptions = {}
        for key in ("description_en", "description_ar"):
            raw = body.get(key)
            if raw is None:
                descriptions[key] = None
                continue
            if not isinstance(raw, str) or len(raw) > 6000:
                return self.send_json(400, {"ok": False, "error": "the description is too long"})
            safe = sanitise.sanitise(raw)
            if len(safe) > 4000:
                return self.send_json(400, {"ok": False, "error": "the description is too long"})
            descriptions[key] = safe
        font = text_size = title_align = None
        if "font" in body:
            font = body.get("font")
            if font not in FORM_FONTS:
                return self.send_json(400, {"ok": False, "error": "unknown font"})
        if "text_size" in body:
            text_size = body.get("text_size")
            if text_size not in TEXT_SIZES:
                return self.send_json(400, {"ok": False, "error": "text size must be small, normal or large"})
        if "title_align" in body:
            title_align = body.get("title_align")
            if title_align not in TITLE_ALIGNS:
                return self.send_json(400, {"ok": False, "error": "title alignment must be start or center"})

        # Layout, welcome screen and thank-you screen. Only what was sent is
        # changed; styled text goes through the same cleaner as the description.
        extra = {}
        if "layout_mode" in body:
            if body.get("layout_mode") not in LAYOUT_MODES:
                return self.send_json(400, {"ok": False, "error": "layout must be one page, steps or one question at a time"})
            extra["layout_mode"] = body["layout_mode"]
        if "terms_mode" in body:
            if body.get("terms_mode") not in TERMS_MODES:
                return self.send_json(400, {"ok": False, "error": "unknown Terms and Conditions setting"})
            extra["terms_mode"] = body["terms_mode"]
        if "welcome_enabled" in body:
            extra["welcome_enabled"] = 1 if body.get("welcome_enabled") else 0
        for col in SCREEN_TEXT_COLS:
            if col in body:
                extra[col] = clean(body.get(col), 80 if col.startswith("thanks_button") else 200) or None
        if "ui_text" in body:
            ui = parse_ui_text(body.get("ui_text"))
            extra["ui_text_json"] = json.dumps(ui, ensure_ascii=False) if ui else None
        for col in SCREEN_RICH_COLS:
            if col in body:
                raw = body.get(col)
                if not isinstance(raw, str) or len(raw) > 6000:
                    return self.send_json(400, {"ok": False, "error": "that text is too long"})
                safe = sanitise.sanitise(raw)
                if len(safe) > 4000:
                    return self.send_json(400, {"ok": False, "error": "that text is too long"})
                extra[col] = safe
        if "hidden_fields" in body:
            names, herr = logic.parse_hidden_names(clean(body.get("hidden_fields"), 500))
            if herr:
                return self.send_json(400, {"ok": False, "error": herr})
            extra["hidden_fields"] = ", ".join(names) or None
        if "thanks_url" in body:
            url = clean(body.get("thanks_url"), 500)
            if url and not URL_RE.match(url):
                return self.send_json(400, {"ok": False, "error": "the button link must start with http:// or https://"})
            extra["thanks_url"] = url or None

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

        theme_mode = None
        if "theme_mode" in body:
            theme_mode = body.get("theme_mode")
            if theme_mode not in THEME_MODES:
                return self.send_json(400, {"ok": False, "error": "theme must be light, dark or both"})

        db.update_branding(form_id, org_name=org_name, primary_color=primary_color,
                            ink_color=ink_color, title_en=title_en, title_ar=title_ar,
                            description_en=descriptions["description_en"],
                            description_ar=descriptions["description_ar"],
                            font=font, text_size=text_size, title_align=title_align, extra=extra,
                            logo_filename=logo_filename, clear_logo=clear_logo,
                            theme_mode=theme_mode)
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
        if not self.require_form_access(form_id):
            return
        fields = db.list_fields(form_id, active_only=False)
        return self.send_json(200, {"ok": True, "fields": fields})

    def get_field_library(self):
        if not self.require_any_permission([("manage_field_keys", None), ("edit_fields", None)]):
            return
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        return self.send_json(200, {"ok": True, "fields": db.list_field_keys()})

    def post_field_key_create(self):
        if not self.require_permission("manage_field_keys"):
            return
        body = self.read_json_body() or {}
        err = self._validate_field_key_payload(body, is_new=True)
        if err:
            return self.send_json(400, {"ok": False, "error": err})
        body["labels_extra"] = clean_labels_extra(body.get("labels_extra"))
        # Every question lands in a group: nothing chosen means "Other".
        body["category"] = clean(body.get("category"), 60) or "Other"
        try:
            new_id = db.create_field_key(body)
        except Exception as e:
            return self.send_json(400, {"ok": False, "error": str(e)})
        return self.send_json(200, {"ok": True, "id": new_id})

    def post_field_key_update(self, key_id):
        if not self.require_permission("manage_field_keys"):
            return
        body = self.read_json_body() or {}
        err = self._validate_field_key_payload(body, is_new=False)
        if err:
            return self.send_json(400, {"ok": False, "error": err})
        body["labels_extra"] = clean_labels_extra(body.get("labels_extra"))
        # Every question lands in a group: nothing chosen means "Other".
        body["category"] = clean(body.get("category"), 60) or "Other"
        try:
            db.update_field_key(key_id, body)
        except Exception as e:
            return self.send_json(400, {"ok": False, "error": str(e)})
        return self.send_json(200, {"ok": True})

    def post_field_key_delete(self, key_id):
        if not self.require_permission("manage_field_keys"):
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
        if body.get("field_type") not in ("text", "email", "tel", "textarea", "select", "rating", "checkbox", "date"):
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
        if not self.require_permission("edit_fields", form_id):
            return
        # A new form field must reference an existing field-key definition —
        # the key, its labels, type and options are owned by the Field keys
        # tab, not typed in here, so pull the canonical copy server-side and
        # ignore anything else the client sent for those.
        key = clean(body.get("field_key"), 64)
        lib_entry = next((k for k in db.list_field_keys() if k["field_key"] == key), None)
        if not lib_entry:
            return self.send_json(400, {"ok": False, "error": "unknown field key — define it in the Field keys tab first"})
        _rule, rerr = logic.clean_show_if(body.get("show_if"))
        if rerr:
            return self.send_json(400, {"ok": False, "error": "show-if rule: " + rerr})
        payload = {
            "field_key": lib_entry["field_key"], "label_en": lib_entry["label_en"],
            "label_ar": lib_entry["label_ar"], "labels_extra": lib_entry.get("labels_extra") or {},
            "field_type": lib_entry["field_type"],
            "options": lib_entry["options"], "required": bool(body.get("required")),
            "display_style": clean_display_style(lib_entry["field_type"], body.get("display_style")),
            "show_if": logic.clean_show_if(body.get("show_if"))[0],
        }
        try:
            new_id = db.create_field(form_id, payload)
        except Exception as e:
            return self.send_json(400, {"ok": False, "error": str(e)})
        return self.send_json(200, {"ok": True, "id": new_id})

    def post_field_label(self, field_id):
        existing = db.get_field(field_id)
        if not existing:
            return self.send_json(404, {"ok": False, "error": "field not found"})
        if not self.require_permission("edit_fields", existing["form_id"]):
            return
        body = self.read_json_body() or {}
        lng = body.get("lang")
        label = clean(body.get("label"), 200)
        if lng not in ("en", "ar"):
            return self.send_json(400, {"ok": False, "error": "language must be en or ar"})
        if not label:
            return self.send_json(400, {"ok": False, "error": "a question needs some text"})
        db.set_field_label(field_id, lng, label)
        return self.send_json(200, {"ok": True})

    def post_field_update(self, field_id):
        existing = db.get_field(field_id)
        if not existing:
            return self.send_json(404, {"ok": False, "error": "field not found"})
        if not self.require_permission("edit_fields", existing["form_id"]):
            return
        body = self.read_json_body() or {}
        err = self._validate_field_payload(body, is_new=False)
        if err:
            return self.send_json(400, {"ok": False, "error": err})
        body["labels_extra"] = clean_labels_extra(body.get("labels_extra"))
        if "display_style" in body:
            body["display_style"] = clean_display_style(body.get("field_type"), body.get("display_style"))
        if "show_if" in body:
            body["show_if"] = logic.clean_show_if(body.get("show_if"))[0]
        try:
            db.update_field(field_id, body)
        except Exception as e:
            return self.send_json(400, {"ok": False, "error": str(e)})
        return self.send_json(200, {"ok": True})

    def post_field_delete(self, field_id):
        existing = db.get_field(field_id)
        if not existing:
            return self.send_json(404, {"ok": False, "error": "field not found"})
        if not self.require_permission("edit_fields", existing["form_id"]):
            return
        db.set_field_active(field_id, False)
        return self.send_json(200, {"ok": True})

    def post_field_page_break(self, field_id):
        existing = db.get_field(field_id)
        if not existing:
            return self.send_json(404, {"ok": False, "error": "field not found"})
        if not self.require_permission("edit_fields", existing["form_id"]):
            return
        body = self.read_json_body() or {}
        db.set_field_page_break(field_id, bool(body.get("on")))
        return self.send_json(200, {"ok": True})

    def post_field_restore(self, field_id):
        existing = db.get_field(field_id)
        if not existing:
            return self.send_json(404, {"ok": False, "error": "field not found"})
        if not self.require_permission("edit_fields", existing["form_id"]):
            return
        db.set_field_active(field_id, True)
        return self.send_json(200, {"ok": True})

    def post_field_reorder(self):
        body = self.read_json_body() or {}
        order = body.get("order")
        if not isinstance(order, list) or not all(isinstance(i, int) for i in order):
            return self.send_json(400, {"ok": False, "error": "order must be a list of ids"})
        # Every id in one reorder call belongs to the same form (the admin
        # UI only ever reorders within a single form's Questions list) —
        # checking the first one covers the whole batch.
        first = db.get_field(order[0]) if order else None
        if not first:
            return self.send_json(400, {"ok": False, "error": "no fields to reorder"})
        if not self.require_permission("edit_fields", first["form_id"]):
            return
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
        if body.get("field_type") not in ("text", "email", "tel", "textarea", "select", "rating", "checkbox", "date"):
            return "invalid field type"
        if body.get("field_type") == "select":
            opts = body.get("options")
            if not isinstance(opts, list) or not opts:
                return "a select field needs at least one option"
            for o in opts:
                if not isinstance(o, dict) or not o.get("value") or not (o.get("label_en") or o.get("label_ar")):
                    return "every option needs a value and an English or Arabic label"
        style = body.get("display_style")
        if style and clean_display_style(body.get("field_type"), style) is None:
            return "that display style does not fit this question type"
        if "show_if" in body:
            _rule, rerr = logic.clean_show_if(body.get("show_if"))
            if rerr:
                return "show-if rule: " + rerr
        return None

    # ---- roles & users ----------------------------------------------------
    #
    # Custom roles built from the permission checklist at the top of this
    # file — see docs/ROLES_AND_PERMISSIONS.md. The bootstrap account
    # (config.json) is untouched by any of this and isn't listed here.

    def get_roles(self):
        if not self.require_permission("manage_users"):
            return
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        return self.send_json(200, {"ok": True, "roles": db.list_roles(),
                                    "permissionKeys": sorted(ALL_PERMISSIONS)})

    def _validate_role_payload(self, body):
        name = clean(body.get("name"), 60)
        if not name:
            return "role name is required", None
        perms = body.get("permissions")
        if not isinstance(perms, list) or not all(isinstance(p, str) for p in perms):
            return "permissions must be a list", None
        perms = sorted(set(p for p in perms if p in ALL_PERMISSIONS))
        applies_to_all_forms = bool(body.get("applies_to_all_forms"))
        return None, (name, perms, applies_to_all_forms)

    def post_role_create(self):
        if not self.require_permission("manage_users"):
            return
        body = self.read_json_body() or {}
        err, parsed = self._validate_role_payload(body)
        if err:
            return self.send_json(400, {"ok": False, "error": err})
        try:
            new_id = db.create_role(*parsed)
        except Exception as e:
            return self.send_json(400, {"ok": False, "error": str(e)})
        return self.send_json(200, {"ok": True, "id": new_id})

    def post_role_update(self, role_id):
        if not self.require_permission("manage_users"):
            return
        body = self.read_json_body() or {}
        err, parsed = self._validate_role_payload(body)
        if err:
            return self.send_json(400, {"ok": False, "error": err})
        try:
            db.update_role(role_id, *parsed)
        except Exception as e:
            return self.send_json(400, {"ok": False, "error": str(e)})
        return self.send_json(200, {"ok": True})

    def post_role_delete(self, role_id):
        if not self.require_permission("manage_users"):
            return
        if db.role_in_use(role_id):
            return self.send_json(400, {"ok": False,
                "error": "this role is still assigned to a user — reassign or remove them first"})
        db.delete_role(role_id)
        return self.send_json(200, {"ok": True})

    def get_admin_users(self):
        if not self.require_permission("manage_users"):
            return
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        # The primary (config.json) admin isn't a row in admin_users, but it
        # is an account people sign in with, so the list shows it too.
        primary = config_store.load()["admin"].get("username") or None
        return self.send_json(200, {"ok": True, "users": db.list_admin_users(),
                                    "primary": {"username": primary} if primary else None})

    def post_admin_user_create(self):
        if not self.require_permission("manage_users"):
            return
        body = self.read_json_body() or {}
        username = clean(body.get("username"), 60)
        password = body.get("password") or ""
        role_id = body.get("role_id")
        form_ids = body.get("form_ids")
        if len(username) < 3:
            return self.send_json(400, {"ok": False, "error": "username too short"})
        if len(password) < 8:
            return self.send_json(400, {"ok": False, "error": "password must be at least 8 characters"})
        role = db.get_role(role_id) if role_id else None
        if not role:
            return self.send_json(400, {"ok": False, "error": "choose a role"})
        if not isinstance(form_ids, list) or not all(isinstance(i, int) for i in form_ids):
            form_ids = []
        pw_hash, salt = config_store.hash_password(password)
        try:
            new_id = db.create_admin_user(username, pw_hash, salt, role["id"], form_ids)
        except Exception as e:
            return self.send_json(400, {"ok": False, "error": str(e)})
        log("admin user created: %s (role: %s)" % (username, role["name"]))
        return self.send_json(200, {"ok": True, "id": new_id})

    def post_admin_user_update(self, user_id):
        if not self.require_permission("manage_users"):
            return
        existing = db.get_admin_user(user_id)
        if not existing:
            return self.send_json(404, {"ok": False, "error": "user not found"})
        body = self.read_json_body() or {}
        role_id, active, form_ids = None, None, None
        if "role_id" in body:
            role = db.get_role(body.get("role_id"))
            if not role:
                return self.send_json(400, {"ok": False, "error": "unknown role"})
            role_id = role["id"]
        if "active" in body:
            active = bool(body.get("active"))
        if "form_ids" in body:
            form_ids = body.get("form_ids")
            if not isinstance(form_ids, list) or not all(isinstance(i, int) for i in form_ids):
                return self.send_json(400, {"ok": False, "error": "form_ids must be a list of ids"})
        password_hash = salt = None
        if body.get("password"):
            if len(body["password"]) < 8:
                return self.send_json(400, {"ok": False, "error": "password must be at least 8 characters"})
            password_hash, salt = config_store.hash_password(body["password"])
        db.update_admin_user(user_id, role_id=role_id, active=active, form_ids=form_ids,
                             password_hash=password_hash, salt=salt)
        if password_hash:
            drop_sessions_for("db", user_id, keep_token=self.current_token())
            log("password reset for admin user %s by %s" % (existing["username"], self.admin_user()))
        return self.send_json(200, {"ok": True})

    def post_admin_user_delete(self, user_id):
        if not self.require_permission("manage_users"):
            return
        db.delete_admin_user(user_id)
        return self.send_json(200, {"ok": True})

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
        if self.query_one("language"):
            filters["language"] = clean(self.query_one("language"), 10)
        if self.query_one("q"):
            filters["q"] = clean(self.query_one("q"), 120)
        return filters

    def _restrict_filters_to_identity(self, identity, filters, permission_key):
        """Enforces form access on top of whatever the query string already
        asked for. Returns (filters, error) — error is a ready-to-send
        (code, message) tuple on a 403, otherwise None. A request naming
        one form_id must have permission_key on that form; a request for
        "all forms" instead gets narrowed to the caller's whole allowed set
        (never silently shown everyone's data)."""
        if identity["kind"] == "bootstrap" or identity["form_ids"] == "all":
            return filters, None
        if not self.has_permission(identity, permission_key):
            return filters, (403, "you don't have permission to do that")
        if filters.get("form_id") is not None:
            if filters["form_id"] not in identity["form_ids"]:
                return filters, (403, "you don't have access to that form")
            return filters, None
        filters = dict(filters)
        filters["form_ids"] = sorted(identity["form_ids"])
        return filters, None

    def get_admin_submissions(self):
        if not self.require_admin():
            return
        identity = self.admin_identity()
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        filters = self._filters_from_query()
        filters, err = self._restrict_filters_to_identity(identity, filters, "view_submissions")
        if err:
            return self.send_json(err[0], {"ok": False, "error": err[1]})
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

    def get_admin_submission_stats(self):
        """Today / last 7 days / a 30-day trend for the Submissions page, in
        the app's own timezone, for whatever the page is filtered to."""
        if not self.require_admin():
            return
        identity = self.admin_identity()
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        filters = self._filters_from_query()
        filters, err = self._restrict_filters_to_identity(identity, filters, "view_submissions")
        if err:
            return self.send_json(err[0], {"ok": False, "error": err[1]})
        try:
            tz = zoneinfo.ZoneInfo(config_store.load()["settings"].get("timezone") or "UTC")
        except Exception:
            tz = timezone.utc
        today = datetime.now(tz).date()
        first_day = today - timedelta(days=29)
        since = datetime(first_day.year, first_day.month, first_day.day, tzinfo=tz).astimezone(timezone.utc)
        try:
            total, languages, hourly = db.submission_stats(filters, since.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as e:
            log("submission stats failed: %s" % e)
            return self.send_json(500, {"ok": False})
        days = {}
        for hour, n in hourly:
            local = datetime.strptime(hour, "%Y-%m-%d %H").replace(tzinfo=timezone.utc).astimezone(tz).date()
            days[local] = days.get(local, 0) + n
        trend = [{"day": str(first_day + timedelta(days=i)), "n": days.get(first_day + timedelta(days=i), 0)}
                 for i in range(30)]
        busiest = max(trend, key=lambda d: d["n"])
        return self.send_json(200, {
            "ok": True, "total": total, "languages": languages, "trend": trend,
            "today": trend[-1]["n"], "last7": sum(d["n"] for d in trend[-7:]),
            "last30": sum(d["n"] for d in trend),
            "busiest": busiest if busiest["n"] else None,
        })

    def get_admin_submission(self, sub_id):
        """One response, every answer - for the Submissions row click."""
        if not self.require_admin():
            return
        identity = self.admin_identity()
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        try:
            row = db.get_submission(sub_id)
        except Exception as e:
            log("submission %s query failed: %s" % (sub_id, e))
            return self.send_json(500, {"ok": False})
        if not row:
            return self.send_json(404, {"ok": False, "error": "not found"})
        if not self.has_permission(identity, "view_submissions", row["form_id"]):
            return self.send_json(403, {"ok": False, "error": "you don't have access to that form"})
        return self.send_json(200, {"ok": True, "submission": row})

    def get_admin_dashboard(self):
        if not self.require_admin():
            return
        identity = self.admin_identity()
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        filters = self._filters_from_query()
        filters, err = self._restrict_filters_to_identity(identity, filters, "view_dashboard")
        if err:
            return self.send_json(err[0], {"ok": False, "error": err[1]})
        granularity = "day"
        if (self.query_one("granularity") == "hour" and filters.get("date_from") and filters.get("date_to")):
            try:
                span = (datetime.strptime(filters["date_to"][:19], "%Y-%m-%d %H:%M:%S") -
                        datetime.strptime(filters["date_from"][:19], "%Y-%m-%d %H:%M:%S"))
                if timedelta(0) <= span <= timedelta(days=10):
                    granularity = "hour"
            except ValueError:
                pass
        tz_offset_minutes = 0
        try:
            tz_name = config_store.load()["settings"].get("timezone") or "UTC"
            now = datetime.now(zoneinfo.ZoneInfo(tz_name))
            tz_offset_minutes = int(now.utcoffset().total_seconds() // 60)
        except Exception:
            pass   # unknown/misconfigured timezone — fall back to UTC bucketing (offset 0)
        try:
            summary = db.dashboard_summary(filters, granularity=granularity, tz_offset_minutes=tz_offset_minutes)
        except Exception as e:
            log("dashboard query failed: %s" % e)
            return self.send_json(500, {"ok": False})
        return self.send_json(200, {"ok": True, "summary": summary})

    def get_form_summary(self, form_id):
        """GET /admin/forms/<id>/summary?from=&to=&hidden_name=&hidden_value=
        The Summary tab's numbers for one form. Needs "view dashboard" on that
        form; the written answers (and "show all") also need "view
        submissions", because they are people's own words."""
        identity = self.require_permission("view_dashboard", form_id)
        if not identity:
            return
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        form = db.get_form(form_id)
        if not form:
            return self.send_json(404, {"ok": False, "error": "form not found"})
        filters = {}
        if self.query_one("from"):
            filters["date_from"] = normalize_filter_dt(self.query_one("from"), end_of_range=False)
        if self.query_one("to"):
            filters["date_to"] = normalize_filter_dt(self.query_one("to"), end_of_range=True)
        hidden = {}
        hname, hvalue = self.query_one("hidden_name"), self.query_one("hidden_value")
        if hname and hvalue is not None and hvalue != "":
            if hname not in logic.parse_hidden_names(form.get("hidden_fields") or "")[0]:
                return self.send_json(400, {"ok": False, "error": "unknown link field"})
            hidden[hname] = clean(hvalue, 100)
        can_text = self.has_permission(identity, "view_submissions", form_id)
        text_key = self.query_one("text_key")
        try:
            if text_key:
                if not can_text:
                    return self.send_json(403, {"ok": False, "error": "you don't have permission to do that"})
                try:
                    offset = max(0, int(self.query_one("offset") or "0"))
                except ValueError:
                    offset = 0
                return self.send_json(200, dict(ok=True, **db.summary_text_answers(
                    form_id, text_key, filters, hidden, offset=offset)))
            tz_offset_minutes = 0
            try:
                tz_name = config_store.load()["settings"].get("timezone") or "UTC"
                tz_offset_minutes = int(datetime.now(zoneinfo.ZoneInfo(tz_name)).utcoffset().total_seconds() // 60)
            except Exception:
                pass
            data = db.form_summary(form_id, filters, hidden, tz_offset_minutes, include_text=can_text)
        except Exception as e:
            log("summary query failed: %s" % e)
            return self.send_json(500, {"ok": False})
        data["canSeeText"] = can_text
        data["formName"] = form["name"]
        return self.send_json(200, {"ok": True, "summary": data})

    def export_csv(self):
        if not self.admin_user():
            return self.fail(403, "Forbidden")
        identity = self.admin_identity()
        if not db.is_connected():
            return self.fail(503, "Database not connected")
        filters = self._filters_from_query()
        filters, err = self._restrict_filters_to_identity(identity, filters, "export_submissions")
        if err:
            return self.fail(err[0], err[1])
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
        token = create_session({"kind": "bootstrap", "username": username})
        cookie = self.session_cookie(token, SESSION_TTL)
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

        if (admin["username"] and username == admin["username"] and
                config_store.verify_password(password, admin["password_hash"], admin["salt"])):
            token = create_session({"kind": "bootstrap", "username": username})
            cookie = self.session_cookie(token, SESSION_TTL)
            log("admin login: %s" % username)
            return self.send_json(200, {"ok": True}, cookie_header=cookie)

        # Not the bootstrap account — try a custom-role user (only possible
        # once the database is up, since that's where these live).
        db_user = None
        if db.is_connected():
            try:
                db_user = db.get_admin_user_by_username(username)
            except Exception as e:
                log("login: could not check admin_users: %s" % e)
        if db_user and config_store.verify_password(password, db_user["password_hash"], db_user["salt"]):
            identity = {
                "kind": "db", "user_id": db_user["id"], "username": db_user["username"],
                "permissions": set(db_user["permissions"]),
                "form_ids": "all" if db_user["role_applies_to_all_forms"] else set(db_user["form_ids"]),
            }
            token = create_session(identity)
            cookie = self.session_cookie(token, SESSION_TTL)
            log("admin login: %s" % username)
            return self.send_json(200, {"ok": True}, cookie_header=cookie)

        log("failed admin login for %r from %s" % (username, ip))
        return self.send_json(401, {"ok": False, "error": "invalid username or password"})

    def post_admin_logout(self):
        c = self.cookies()
        if SESSION_COOKIE in c:
            drop_session(c[SESSION_COOKIE].value)
        cookie = self.session_cookie("", 0)
        return self.send_json(200, {"ok": True}, cookie_header=cookie)

    def post_account_theme(self):
        """Any signed-in account setting the colors it sees. {"reset": true}
        goes back to the app-wide colors; nobody else's view changes either
        way."""
        identity = self.admin_identity()
        if not identity:
            return self.send_json(401, {"ok": False, "error": "not signed in"})
        body = self.read_json_body() or {}
        cfg = config_store.load()
        themes = cfg["settings"].setdefault("user_themes", {})
        key = identity_theme_key(identity)
        if body.get("reset"):
            themes.pop(key, None)
            config_store.save(cfg)
            return self.send_json(200, {"ok": True, "theme": theme_for_identity(cfg, identity),
                                        "isOwn": False})
        theme, err = clean_theme(body, themes.get(key))
        if err:
            return self.send_json(400, {"ok": False, "error": err})
        themes[key] = theme
        config_store.save(cfg)
        return self.send_json(200, {"ok": True, "theme": themes[key], "isOwn": True})

    def post_wallpaper(self):
        """Upload the picture behind the panel: {"target": "mine"} for the
        signed-in account, {"target": "app"} for everyone's default (needs the
        app-settings permission). PNG, JPG or WEBP up to 2 MB."""
        identity = self.admin_identity()
        if not identity:
            return self.send_json(401, {"ok": False, "error": "not signed in"})
        body = self.read_json_body(max_body=MAX_LOGO_BODY) or {}
        target = body.get("target")
        if target == "app":
            if not self.require_permission("manage_app_settings"):
                return
            owner = "app"
        elif target == "mine":
            owner = identity_theme_key(identity)
        else:
            return self.send_json(400, {"ok": False, "error": "unknown target"})
        ext = (body.get("ext") or "").lower().lstrip(".")
        if ext not in WALLPAPER_EXT:
            return self.send_json(400, {"ok": False, "error": "the picture must be PNG, JPG or WEBP"})
        try:
            raw = base64.b64decode(body.get("base64") or "", validate=True)
        except Exception:
            return self.send_json(400, {"ok": False, "error": "could not read that picture"})
        if not raw or len(raw) > MAX_LOGO_BYTES:
            return self.send_json(400, {"ok": False, "error": "the picture must be under 2 MB"})
        base = wallpaper_base(owner)
        updir = config_store.uploads_dir()
        for e in WALLPAPER_EXT:
            try:
                os.remove(os.path.join(updir, base + "." + e))
            except OSError:
                pass
        with open(os.path.join(updir, base + "." + ext), "wb") as f:
            f.write(raw)
        cfg = config_store.load()
        if owner == "app":
            theme = dict(cfg["settings"].get("admin_theme") or DEFAULT_ADMIN_THEME)
        else:
            themes = cfg["settings"].setdefault("user_themes", {})
            theme = dict(themes.get(owner) or theme_for_identity(cfg, identity))
        theme["wallpaper"] = "%s.%s?v=%d" % (base, ext, int(time.time()))
        theme["style"] = "wallpaper"
        if owner == "app":
            cfg["settings"]["admin_theme"] = theme
        else:
            themes[owner] = theme
        config_store.save(cfg)
        return self.send_json(200, {"ok": True, "theme": theme})

    def get_wallpaper(self, name):
        # Signed-in accounts only; the name can only ever be a wallpaper file.
        if not self.admin_identity():
            return self.fail(401, "Not signed in")
        if not WALLPAPER_FILE_RE.match(name):
            return self.fail(404)
        path = os.path.join(config_store.uploads_dir(), name)
        if not os.path.isfile(path):
            return self.fail(404)
        with open(path, "rb") as f:
            return self.send_file_bytes(f.read(), WALLPAPER_EXT[name.rsplit(".", 1)[1]], cache=True)

    def post_port_test(self):
        """Checks a port before a form is saved — the one the admin typed, or
        the one auto-assign would pick when the box is left blank."""
        if not self.require_any_permission([("manage_forms", None), ("manage_form_settings", None)]):
            return
        if not db.is_connected():
            return self.send_json(503, {"ok": False, "error": "database not connected"})
        body = self.read_json_body() or {}
        form_id = body.get("form_id")
        try:
            form_id = int(form_id) if form_id else None
        except (TypeError, ValueError):
            form_id = None
        requested = body.get("port")
        err, port = self._resolve_new_port(requested, exclude_form_id=form_id)
        if err:
            return self.send_json(200, {"ok": False, "port": None,
                                        "assigned": "manual" if requested else "automatic",
                                        "error": err})

        own_port = None
        if form_id:
            existing = db.get_form(form_id)
            own_port = existing["port"] if existing else None

        notes = []
        if form_id and port == own_port:
            notes.append("Port %d is this form's current port." % port)
        elif requested:
            notes.append("Port %d is free and not used by another form." % port)
        else:
            notes.append("Port %d is the next free port in the auto-assign range %d–%d."
                         % (port, FORM_PORT_RANGE[0], FORM_PORT_RANGE[1]))
        if port == own_port:
            notes.append("This form already runs on it, and answered just now."
                         if self._port_answers(port) else
                         "This form is set to it, but nothing answered on it just now.")
        allowed = published_ports()
        if not allowed:
            notes.append("Nothing is restricting which ports can be reached on this server.")
        elif port in allowed:
            notes.append("Docker publishes it, so visitors can reach it.")
        else:
            notes.append("Docker does not publish it — the form would start but stay "
                         "unreachable from outside. Published: %s." % PUBLISHED_PORTS_RAW)
        reachable = (not allowed) or (port in allowed)
        return self.send_json(200, {"ok": reachable, "port": port,
                                    "assigned": "manual" if requested else "automatic",
                                    "notes": notes})

    def _port_answers(self, port, timeout=1.0):
        """Does something actually answer on this port right now?"""
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout):
                return True
        except OSError:
            return False

    def post_account_password(self):
        # Any signed-in account changing its own password. For the primary
        # (config.json) admin this is the only in-app way — nobody else can
        # reset that account; reset_password.py covers a lockout.
        identity = self.admin_identity()
        if not identity:
            return self.send_json(401, {"ok": False, "error": "not authenticated"})
        if not rate_ok(self.client_ip(), "password", 20):
            return self.send_json(429, {"ok": False, "error": "too many attempts, try later"})
        body = self.read_json_body() or {}
        current = body.get("current_password") or ""
        new = body.get("new_password") or ""
        if len(new) < 8:
            return self.send_json(400, {"ok": False, "error": "new password must be at least 8 characters"})

        if identity["kind"] == "bootstrap":
            cfg = config_store.load()
            admin = cfg["admin"]
            if not config_store.verify_password(current, admin["password_hash"], admin["salt"]):
                return self.send_json(400, {"ok": False, "error": "current password is incorrect"})
            admin["password_hash"], admin["salt"] = config_store.hash_password(new)
            config_store.save(cfg)
            drop_sessions_for("bootstrap", identity["username"], keep_token=self.current_token())
        else:
            if not db.is_connected():
                return self.send_json(503, {"ok": False, "error": "database not connected"})
            user = db.get_admin_user_by_username(identity["username"])
            if not user or not config_store.verify_password(current, user["password_hash"], user["salt"]):
                return self.send_json(400, {"ok": False, "error": "current password is incorrect"})
            pw_hash, salt = config_store.hash_password(new)
            db.update_admin_user(user["id"], password_hash=pw_hash, salt=salt)
            drop_sessions_for("db", user["id"], keep_token=self.current_token())

        log("password changed by %s" % identity["username"])
        return self.send_json(200, {"ok": True})


# --------------------------------------------------------------------- main

def _db_health_check_loop():
    """Runs for the lifetime of the process, independent of any one
    request. Fires db_disconnected once per outage (not once per failed
    check) by tracking whether we already alerted on the current outage,
    and clears that flag as soon as the database is reachable again."""
    already_alerted = False
    while True:
        time.sleep(60)
        if not db.is_connected():
            continue
        if db.ping():
            already_alerted = False
            continue
        if already_alerted:
            continue
        already_alerted = True
        log("database health check failed — connection appears to be down")
        try:
            cfg = config_store.load()
            notifier.fire("db_disconnected", cfg, log=log,
                          subject="Database disconnected",
                          body="Open Feedback Forms lost its database connection. "
                               "Forms will stop accepting submissions until it's restored.")
        except Exception as e:
            log("db_disconnected alert failed: %s" % e)


def _daily_digest_loop():
    """Sleeps to the next local midnight, then (a) sends each digest-enabled
    form's submission count since the last run and (b) fires form_expiry
    for any form whose expiry date has just been reached. Repeats. A day
    this thread is asleep for (process restarted, machine was off) is
    simply skipped — not worth the complexity of catching up on a missed
    digest, though an expiry alert that was missed still fires the next
    time this loop runs, since form_is_expired() stays true past the day."""
    last_run = datetime.now()
    already_expiry_alerted = set()
    while True:
        now = datetime.now()
        tomorrow_midnight = datetime(now.year, now.month, now.day) + timedelta(days=1)
        time.sleep(max(60, (tomorrow_midnight - now).total_seconds()))
        if not db.is_connected():
            last_run = datetime.now()
            continue
        cfg = config_store.load()
        try:
            for form in db.list_forms(active_only=True):
                rules = db.list_alert_rules(form_id=form["id"], trigger_type="daily_digest", enabled_only=True)
                if rules:
                    filters = {"form_id": form["id"], "date_from": last_run.strftime("%Y-%m-%d %H:%M:%S")}
                    _, total = db.list_submissions(filters, page=1, page_size=1)
                    if total:
                        subject = "Daily digest: %s" % form["name"]
                        body = "%d new submission%s since the last digest." % (total, "" if total == 1 else "s")
                        for rule in rules:
                            notifier.send(rule, subject, body, cfg, log)

                if form_is_expired(form) and form["id"] not in already_expiry_alerted:
                    already_expiry_alerted.add(form["id"])
                    notifier.fire("form_expiry", cfg, log=log, form_id=form["id"],
                                  subject="Form expired: %s" % form["name"],
                                  body="This form reached its expiry date (%s) and stopped "
                                       "accepting responses." % form["expiry_date"])
        except Exception as e:
            log("daily digest / expiry check failed: %s" % e)
        last_run = datetime.now()


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
            topup_library_once()
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

    threading.Thread(target=_db_health_check_loop, daemon=True).start()
    threading.Thread(target=_daily_digest_loop, daemon=True).start()
    threading.Thread(target=_webhook_worker_loop, daemon=True).start()

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
