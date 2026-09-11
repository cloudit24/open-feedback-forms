#!/usr/bin/env python3
"""
Reset any admin account's password from the command line — the way back in
when the primary admin is locked out, since nobody can reset that account
from the admin panel.

    python reset_password.py              list every account
    python reset_password.py <username>   set a new password (asked twice)

Docker:
    docker compose exec app python reset_password.py <username>

The password is only ever typed at the prompt, never passed as an argument,
so it can't end up in shell history.
"""

import getpass
import sys

import config_store
import db


def connect_db(cfg):
    if not config_store.db_configured(cfg):
        return False
    try:
        db.set_pool(db._make_pool(cfg["db"]))
        return True
    except Exception as e:
        print("(database unreachable — only the primary admin can be reset right now: %s)" % e)
        return False


def ask_new_password():
    while True:
        pw = getpass.getpass("New password (at least 8 characters): ")
        if len(pw) < 8:
            print("  too short, try again")
        elif getpass.getpass("Repeat it: ") != pw:
            print("  didn't match, try again")
        else:
            return pw


def main():
    cfg = config_store.load()
    primary = cfg["admin"]["username"]
    has_db = connect_db(cfg)

    if len(sys.argv) != 2:
        print("usage: python reset_password.py <username>\n\naccounts:")
        if primary:
            print("  %s  (primary admin)" % primary)
        if has_db:
            for u in db.list_admin_users():
                print("  %s%s" % (u["username"], "" if u["active"] else "  (inactive)"))
        return 1

    username = sys.argv[1]
    if primary and username == primary:     # same precedence as login
        pw = ask_new_password()
        cfg = config_store.load()
        cfg["admin"]["password_hash"], cfg["admin"]["salt"] = config_store.hash_password(pw)
        config_store.save(cfg)
    else:
        user = db.get_admin_user_by_username(username) if has_db else None
        if not user:
            print("No account named %r — run without a username to list them." % username)
            return 1
        pw = ask_new_password()
        pw_hash, salt = config_store.hash_password(pw)
        db.update_admin_user(user["id"], password_hash=pw_hash, salt=salt)

    print("Password updated for %s." % username)
    # Sessions live in the running app's memory, out of this process's reach.
    print("Anyone already signed in as %s stays signed in until the app restarts" % username)
    print("(docker compose restart app).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
