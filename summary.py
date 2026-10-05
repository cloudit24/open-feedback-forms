"""Results summary (1.12.0): which kind of chart a question gets, and the
NPS / CSAT / average maths. Pure functions, no database and no web code, so
the server and the unit tests share one definition. db.form_summary() does
the counting in SQL and hands the counts to build_question() here.

How a question is recognised (answers are stored as: rating = number 1-5,
dropdown/buttons/scale/grid = the option's value text, tick box = true/false):
  nps     a dropdown with 11 answers whose values are 0..10 (any display
          style), or a dropdown whose key is "nps"
  csat    a rating (stars, faces or numbers), or a dropdown with 5 answers
          whose values are 1..5 - CSAT is the share of 4s and 5s
  scale   any other dropdown whose values are all whole numbers and that is
          shown as a scale (for example 1-10) - bars plus an average
  choice  every other dropdown / buttons / grid
  checkbox, text (text, long text, email, phone, date)
"""

TEXT_TYPES = ("text", "textarea", "email", "tel", "date")
GRID_MIN, GRID_MAX = 2, 7          # same limits as public/index.html


def _int_values(field):
    """The option values as whole numbers, or None if any is not a number."""
    out = []
    for o in field.get("options") or []:
        try:
            out.append(int(str(o.get("value")).strip()))
        except (TypeError, ValueError):
            return None
    return out


def kind_of(field):
    t = field.get("field_type")
    if t == "checkbox":
        return "checkbox"
    if t == "rating":
        return "csat"
    if t in TEXT_TYPES:
        return "text"
    if t == "select":
        nums = _int_values(field)
        if nums and sorted(nums) == list(range(11)):
            return "nps"
        if field.get("field_key") == "nps":
            return "nps"
        if nums and sorted(nums) == [1, 2, 3, 4, 5]:
            return "csat"
        if nums and field.get("display_style") == "scale":
            return "scale"
        return "choice"
    return "text"


def is_grid(field):
    n = len(field.get("options") or [])
    return (field.get("field_type") == "select" and field.get("display_style") == "grid"
            and GRID_MIN <= n <= GRID_MAX)


def grid_groups(fields):
    """Neighbouring grid questions with the same answers form one heatmap
    (the public form joins them the same way). Returns a list of lists of
    field keys, only the runs of 2 or more matter for the heatmap but a
    single grid question gets one too."""
    groups, cur, sig = [], [], None
    for f in fields:
        if is_grid(f):
            s = "|".join(str(o.get("value")) for o in f["options"])
            if cur and s == sig:
                cur.append(f["field_key"])
            else:
                if cur:
                    groups.append(cur)
                cur, sig = [f["field_key"]], s
        else:
            if cur:
                groups.append(cur)
            cur, sig = [], None
    if cur:
        groups.append(cur)
    return groups


def _pct(n, total):
    return round(100.0 * n / total, 1) if total else 0.0


def nps_from_counts(counts):
    """counts: {"0": n, ..., "10": n}. Returns None when nobody answered."""
    pro = sum(counts.get(str(i), 0) for i in (9, 10))
    pas = sum(counts.get(str(i), 0) for i in (7, 8))
    det = sum(counts.get(str(i), 0) for i in range(0, 7))
    total = pro + pas + det
    if not total:
        return None
    return {"score": round(100.0 * (pro - det) / total), "n": total,
            "promoters": pro, "passives": pas, "detractors": det,
            "promotersPct": _pct(pro, total), "passivesPct": _pct(pas, total),
            "detractorsPct": _pct(det, total)}


def csat_from_counts(counts):
    """counts: {"1": n, ..., "5": n}. CSAT = share of 4s and 5s."""
    total = sum(counts.get(str(i), 0) for i in range(1, 6))
    if not total:
        return None
    sat = counts.get("4", 0) + counts.get("5", 0)
    return {"pct": _pct(sat, total), "n": total, "satisfied": sat}


def average_from_counts(counts):
    total = weighted = 0
    for k, n in counts.items():
        try:
            v = int(k)
        except (TypeError, ValueError):
            continue
        total += n
        weighted += v * n
    return round(weighted / total, 2) if total else None


def _tone_for_nps(v):
    return "bad" if v <= 6 else ("mid" if v <= 8 else "good")


def build_question(field, counts, answered):
    """One question's result. counts = {answer text: how many}, answered =
    how many responses have an answer (so a question hidden by a rule or left
    blank shows 'n answered', not the form total)."""
    kind = kind_of(field)
    out = {"key": field["field_key"], "label_en": field.get("label_en"),
           "label_ar": field.get("label_ar"), "kind": kind, "answered": answered,
           "grid": is_grid(field), "active": bool(field.get("active", True))}
    if kind == "text":
        return out
    if kind == "checkbox":
        yes = counts.get("true", 0) + counts.get("1", 0)   # MariaDB reads JSON true as 1
        out["bars"] = [{"value": "true", "label_en": "Yes", "label_ar": "نعم", "n": yes, "pct": _pct(yes, answered)},
                       {"value": "false", "label_en": "No", "label_ar": "لا",
                        "n": answered - yes, "pct": _pct(answered - yes, answered)}]
        return out

    bars, seen = [], set()
    if kind == "nps":
        values = [str(i) for i in range(11)]
        labels = {v: (v, v) for v in values}
    elif field.get("field_type") == "rating":
        values = [str(i) for i in range(1, 6)]
        labels = {v: (v, v) for v in values}
    else:
        values, labels = [], {}
        for o in field.get("options") or []:
            v = str(o.get("value"))
            values.append(v)
            labels[v] = (o.get("label_en") or v, o.get("label_ar") or o.get("label_en") or v)
    for v in values:
        n = counts.get(v, 0)
        seen.add(v)
        b = {"value": v, "label_en": labels[v][0], "label_ar": labels[v][1], "n": n, "pct": _pct(n, answered)}
        if kind == "nps":
            b["tone"] = _tone_for_nps(int(v))
        bars.append(b)
    for v, n in sorted(counts.items()):          # answers no longer in the option list
        if v not in seen and v != "":
            bars.append({"value": v, "label_en": v, "label_ar": v, "n": n, "pct": _pct(n, answered)})
    out["bars"] = bars
    if kind == "nps":
        out["nps"] = nps_from_counts(counts)
    if kind == "csat":
        out["csat"] = csat_from_counts(counts)
        out["average"] = average_from_counts({k: v for k, v in counts.items() if k in ("1", "2", "3", "4", "5")})
    if kind == "scale":
        out["average"] = average_from_counts(counts)
    return out
