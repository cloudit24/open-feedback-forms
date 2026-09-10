"""
Local app configuration — the one thing that can't live in MariaDB, since
the app needs it *before* it can reach MariaDB.

Holds: the MariaDB connection settings, the admin account (username +
password hash), and a random secret used to sign admin session cookies.

Stored as plain JSON next to the app. Anyone with filesystem access to this
machine can already read the source and the SQLite file the old version
used, so this isn't a new trust boundary — but it does mean this file
should never be committed to source control or placed on a shared drive.
"""

import hashlib
import json
import os
import secrets
import sys
import threading


def _config_dir():
    # OFF_CONFIG_DIR lets the Docker image point config.json and uploads at
    # a mounted volume (see Dockerfile/docker-compose.yml) instead of the
    # container's throwaway filesystem — checked first so it also works if
    # someone sets it on a frozen build.
    override = os.environ.get("OFF_CONFIG_DIR")
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    # The installed app lives under the current user's LocalAppData (a
    # per-user install, deliberately -- see packaging/app.wxs), which is
    # always writable without admin rights. Config goes in the same
    # per-user area rather than next to the executable itself, so it
    # survives a reinstall/upgrade that replaces the program files.
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.path.dirname(sys.executable)
        d = os.path.join(base, "Open Feedback Forms")
        os.makedirs(d, exist_ok=True)
        return d
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(_config_dir(), "config.json")


def uploads_dir():
    """Where uploaded form logos live — same writable, per-user area as
    config.json, so it survives a reinstall/upgrade just the same."""
    d = os.path.join(_config_dir(), "uploads", "logos")
    os.makedirs(d, exist_ok=True)
    return d

_lock = threading.Lock()

_DEFAULT = {
    "db": {"host": "", "port": 3306, "user": "", "password": "", "database": ""},
    "admin": {"username": "", "password_hash": "", "salt": ""},
    "session_secret": "",
    "settings": {
        "timezone": "UTC", "ntp_server": "pool.ntp.org",
        "language_labels": {
            "en": {"name": "English", "dir": "ltr"},
            "ar": {"name": "Arabic", "dir": "rtl"},
        },
        "admin_theme": {"primary": "#FFEC01", "text": "#0B0B0B"},
        # Cache of the public page's fixed UI text (Submit, error messages...)
        # per language added beyond the built-in en/ar pair — see
        # translate_client.py / ui_strings.py. en/ar never appear here, they
        # stay hand-written in public/index.html.
        "ui_translations": {},
        # Public LibreTranslate instance used only when adding a language —
        # editable in case of a future self-hosted instance; None means
        # translate_client's own default.
        "translate_endpoint": "https://libretranslate.com",
        # libretranslate.com's /translate endpoint requires a free API key
        # (portal.libretranslate.com) even though /languages doesn't —
        # without one, "Add a language" still works, just with nothing
        # auto-translated.
        "translate_api_key": "",
        # Shared infra credentials for alert_rules (see db.py) — one SMTP
        # server and one Telegram bot for the whole app; each alert rule
        # only needs to say *where* to send, not *how*.
        "smtp": {"host": "", "port": 587, "user": "", "password": "", "from": "", "use_tls": True},
        "telegram": {"bot_token": ""},
        # Unlike form-scoped alert rules (in MariaDB — see db.py's
        # alert_rules table), this list has to live here: it's the alert
        # for when that same database becomes unreachable, so it can't
        # depend on the database being reachable to know who to notify.
        "db_disconnected_alerts": [],
    },
}


def _fresh():
    cfg = json.loads(json.dumps(_DEFAULT))       # deep copy
    cfg["session_secret"] = secrets.token_hex(32)
    return cfg


def load():
    with _lock:
        if not os.path.isfile(CONFIG_PATH):
            cfg = _fresh()
            _write(cfg)
            return cfg
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # keep older configs usable if a field gets added later
        changed = False
        for key, default in _DEFAULT.items():
            if key not in cfg:
                cfg[key] = json.loads(json.dumps(default))
                changed = True
        # "settings" existed before language_labels was added to it — backfill
        # just that sub-key instead of clobbering timezone/ntp_server already saved
        for key, default in _DEFAULT["settings"].items():
            if key not in cfg["settings"]:
                cfg["settings"][key] = json.loads(json.dumps(default))
                changed = True
        if not cfg.get("session_secret"):
            cfg["session_secret"] = secrets.token_hex(32)
            changed = True
        if changed:
            _write(cfg)
        return cfg


def _write(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)                 # atomic on Windows too


def save(cfg):
    with _lock:
        _write(cfg)


def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), 200_000)
    return digest.hex(), salt


def verify_password(password, password_hash, salt):
    if not password_hash or not salt:
        return False
    check, _ = hash_password(password, salt)
    return secrets.compare_digest(check, password_hash)


def admin_configured(cfg):
    return bool(cfg["admin"]["username"] and cfg["admin"]["password_hash"])


def db_configured(cfg):
    d = cfg["db"]
    return bool(d["host"] and d["user"] and d["database"])
