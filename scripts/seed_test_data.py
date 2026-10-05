"""Fills a TEST database with fake responses so the Summary tab has something
to show. Never run this against a live database.

    python scripts/seed_test_data.py --port 13306 --password test --db off \
        --form-id 1 --count 500 --seed 1 --expect expected.json

It reads the form's questions and invents answers that fit each kind
(rating 1-5, 0-10 scale, choice, tick box, written text), leaves some optional
questions blank, spreads the responses over the last 60 days, and saves a link
field (branch) with each one. --expect writes the counts it generated, worked
out here in Python, so they can be compared with what the Summary tab shows.
"""
import argparse
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mysql.connector  # noqa: E402

import summary  # noqa: E402

TEXTS = ["Great service, thank you", "Waiting time was too long", "The staff were very helpful",
         "Could be cleaner", "Loved it", "شكرا لكم على الخدمة الممتازة", "الانتظار كان طويلا",
         "Please open earlier in the morning", "Nothing to add", "Easy to use and quick"]
BRANCHES = ["dubai", "abu-dhabi", "sharjah"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13306)
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="test")
    ap.add_argument("--db", default="off")
    ap.add_argument("--form-id", type=int, default=1)
    ap.add_argument("--count", type=int, default=500)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--expect", default="")
    a = ap.parse_args()

    rnd = random.Random(a.seed)
    conn = mysql.connector.connect(host=a.host, port=a.port, user=a.user, password=a.password, database=a.db)
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM form_fields WHERE form_id=%s AND active=1 ORDER BY sort_order, id", (a.form_id,))
    fields = cur.fetchall()
    for f in fields:
        f["options"] = json.loads(f["options_json"]) if f["options_json"] else None
    cur.execute("SELECT hidden_fields FROM forms WHERE id=%s", (a.form_id,))
    if not (cur.fetchone() or {}).get("hidden_fields"):
        cur.execute("UPDATE forms SET hidden_fields='branch' WHERE id=%s", (a.form_id,))

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    expect = {f["field_key"]: {} for f in fields}
    by_branch = {}
    for i in range(a.count):
        extra = {}
        for f in fields:
            kind = summary.kind_of(f)
            if rnd.random() < (0.05 if f["required"] else 0.35):
                continue                                     # left blank
            if kind == "checkbox":
                v = rnd.random() < 0.6
            elif f["field_type"] == "rating":
                v = rnd.choices([1, 2, 3, 4, 5], [1, 1, 2, 4, 5])[0]
            elif kind == "nps":
                v = str(rnd.choices(range(11), [1, 1, 1, 1, 1, 2, 2, 3, 4, 5, 6])[0])
            elif f["field_type"] == "select" and f["options"]:
                v = rnd.choice(f["options"])["value"]
            elif f["field_type"] in ("text", "textarea"):
                v = rnd.choice(TEXTS)
            else:
                continue                                     # email / tel / date: skip
            extra[f["field_key"]] = v
            k = "true" if v is True else ("false" if v is False else str(v))
            expect[f["field_key"]][k] = expect[f["field_key"]].get(k, 0) + 1
        branch = rnd.choice(BRANCHES)
        by_branch[branch] = by_branch.get(branch, 0) + 1
        when = now - timedelta(days=rnd.random() * 60, minutes=rnd.random() * 600)
        cur.execute(
            "INSERT INTO feedback (form_id, reference, created_at, first_name, last_name, email, consent, language,"
            " extra_fields, ip, user_agent, hidden_json) VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,'127.0.0.1','seed',%s)",
            (a.form_id, "SD%d-%05d" % (a.seed, i), when, "Test", "Person%d" % i, "t%d@example.com" % i,
             rnd.choice(["en", "ar"]), json.dumps(extra, ensure_ascii=False),
             json.dumps({"branch": branch})))
    conn.commit()
    if a.expect:
        with open(a.expect, "w", encoding="utf-8") as fh:
            json.dump({"total": a.count, "questions": expect, "branches": by_branch}, fh, ensure_ascii=False, indent=1)
    print("added %d responses to form %d" % (a.count, a.form_id))


if __name__ == "__main__":
    main()
