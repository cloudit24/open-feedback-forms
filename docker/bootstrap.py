#!/usr/bin/env python3
"""
Pre-fills config.json's database section from environment variables, run
once by install.sh before the app's first `docker compose up` — so a fresh
install already has its database connection ready instead of making the
admin re-enter it through the web UI's Database tab.

The admin account itself is deliberately NOT created here: config_store's
existing "adminConfigured" gate already sends a first-run visitor to a
"create your admin account" screen, and that account's password should be
typed once, straight into that form, never passed around as an env var or
CLI argument.

Usage (inside the app container, image already built):
    OFF_DB_HOST=... OFF_DB_PORT=... OFF_DB_USER=... OFF_DB_PASSWORD=... OFF_DB_NAME=... \
        python docker/bootstrap.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_store  # noqa: E402


def main():
    host = os.environ.get("OFF_DB_HOST", "").strip()
    if not host:
        print("OFF_DB_HOST not set — nothing to bootstrap, leaving config.json as-is")
        return
    cfg = config_store.load()
    cfg["db"] = {
        "host": host,
        "port": int(os.environ.get("OFF_DB_PORT", "3306")),
        "user": os.environ.get("OFF_DB_USER", ""),
        "password": os.environ.get("OFF_DB_PASSWORD", ""),
        "database": os.environ.get("OFF_DB_NAME", ""),
    }
    config_store.save(cfg)
    print("database connection saved to config.json (host=%s, database=%s)" %
          (host, cfg["db"]["database"]))


if __name__ == "__main__":
    main()
