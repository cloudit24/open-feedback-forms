"""
SMTP + Telegram alerts — stdlib-only, same "one real dependency (the DB
driver), everything else is standard library" rule the rest of this
project follows.

Every public function here fails soft: a bad SMTP server, an invalid bot
token, a network hiccup — none of it should ever propagate up and break
the request that triggered the alert (a visitor submitting a form must
never see a 500 because someone's Telegram bot token expired). Failures
are logged via the caller-supplied `log` function and otherwise swallowed.
"""

import json
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

import db

_TIMEOUT = 10


def send_email(smtp_cfg, to_addr, subject, body, log=lambda msg: None):
    host = (smtp_cfg or {}).get("host")
    if not host or not to_addr:
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_cfg.get("from") or smtp_cfg.get("user") or "noreply@localhost"
    msg["To"] = to_addr
    msg.set_content(body)
    try:
        port = int(smtp_cfg.get("port") or 587)
        cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
        with cls(host, port, timeout=_TIMEOUT) as server:
            if smtp_cfg.get("use_tls") and port != 465:
                server.starttls()
            if smtp_cfg.get("user"):
                server.login(smtp_cfg["user"], smtp_cfg.get("password") or "")
            server.send_message(msg)
        return True
    except Exception as e:
        log("email alert failed (%s): %s" % (to_addr, e))
        return False


def send_telegram(bot_token, chat_id, text, log=lambda msg: None):
    if not bot_token or not chat_id:
        return False
    url = "https://api.telegram.org/bot%s/sendMessage" % bot_token
    data = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                log("telegram alert rejected (%s): %s" % (chat_id, body.get("description")))
                return False
            return True
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        log("telegram alert failed (%s): %s" % (chat_id, e))
        return False


def send(rule, subject, body, cfg, log=lambda msg: None):
    """One alert_rules row -> one delivery attempt on its channel."""
    if rule["channel"] == "email":
        return send_email(cfg["settings"].get("smtp", {}), rule["destination"], subject, body, log)
    if rule["channel"] == "telegram":
        bot_token = cfg["settings"].get("telegram", {}).get("bot_token", "")
        return send_telegram(bot_token, rule["destination"], subject + "\n\n" + body, log)
    return False


def fire(trigger_type, cfg, log=lambda msg: None, form_id=None, subject="", body=""):
    """Looks up every enabled rule for this trigger and sends on each.
    Never raises — a delivery failure is logged and moves on to the next
    rule rather than blocking the rest.

    db_disconnected is the one exception to "rules live in the database":
    by definition it can fire exactly when the database is unreachable, so
    its destinations live in config_store (config.json) instead of the
    alert_rules table, the same reason the bootstrap admin account does —
    it has to work when nothing else does."""
    if trigger_type == "db_disconnected":
        rules = cfg["settings"].get("db_disconnected_alerts", [])
        rules = [r for r in rules if r.get("enabled", True)]
    else:
        if not db.is_connected():
            return
        try:
            rules = db.list_alert_rules(form_id=form_id, trigger_type=trigger_type, enabled_only=True)
        except Exception as e:
            log("alert lookup failed: %s" % e)
            return
    for rule in rules:
        send(rule, subject, body, cfg, log)
