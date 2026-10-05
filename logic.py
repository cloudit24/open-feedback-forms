"""Conditional questions (1.11.0): "show this question only if an earlier
answer matches". Pure functions, no database and no web code, so the server
and the unit tests share one definition. public/index.html has a matching
JavaScript copy (evalLogic) - keep the two in step.

A rule is stored as JSON in form_fields.show_if:
    {"all": [{"field": "nps", "op": "lte", "value": "6"}, ...]}
Every condition must hold (AND). Operators:
    eq, neq        value is a string
    in             value is a list of strings
    gte, lte       value is a number written as a string ("9")

How answers are compared (the browser does exactly the same):
  * every answer is turned into text first: a rating 9 and a dropdown option
    "9" are both "9"; a tick box is "true" or "false"
  * a question with no answer yet makes every condition on it false
  * a question that is itself hidden counts as having no answer
  * questions are checked in the order they appear on the form; a rule that
    points at a LATER (or removed) question sees no answer, so it stays hidden.
    The admin panel warns about that.
"""
import math
import re

OPS = ("eq", "neq", "in", "gte", "lte")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_CONDITIONS = 10
MAX_VALUE_LEN = 200
MAX_IN_VALUES = 30


def clean_show_if(raw):
    """Returns (rule_or_None, error_or_None). None = always shown. Anything
    malformed is an error, so a half-broken rule is never stored."""
    if raw in (None, "", {}, []):
        return None, None
    if not isinstance(raw, dict) or not isinstance(raw.get("all"), list):
        return None, "the rule is not in the expected shape"
    conds = raw["all"]
    if not conds:
        return None, None
    if len(conds) > MAX_CONDITIONS:
        return None, "a question can have at most %d conditions" % MAX_CONDITIONS
    out = []
    for c in conds:
        if not isinstance(c, dict):
            return None, "a condition is not in the expected shape"
        field, op, value = c.get("field"), c.get("op"), c.get("value")
        if not isinstance(field, str) or not KEY_RE.match(field):
            return None, "a condition has no question chosen"
        if op not in OPS:
            return None, "unknown condition type"
        if op == "in":
            if not isinstance(value, list) or not value or len(value) > MAX_IN_VALUES:
                return None, "choose at least one value for 'is one of'"
            vals = [_text(v) for v in value]
            if any(v is None or v == "" or len(v) > MAX_VALUE_LEN for v in vals):
                return None, "a condition value is not valid"
            out.append({"field": field, "op": op, "value": vals})
            continue
        v = _text(value)
        if v is None or v == "" or len(v) > MAX_VALUE_LEN:
            return None, "a condition needs a value"
        if op in ("gte", "lte") and _num(v) is None:
            return None, "'at least' and 'at most' need a number"
        out.append({"field": field, "op": op, "value": v})
    return {"all": out}, None


def _text(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else str(v)
    if isinstance(v, str):
        return v.strip()
    return None


_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _num(s):
    """A plain decimal number written as text, or None. Deliberately strict
    (no 1e5, no 1_0, no nan) so the browser and the server agree."""
    if not isinstance(s, str) or not _NUM_RE.match(s.strip()):
        return None
    n = float(s.strip())
    return n if math.isfinite(n) else None


def answer_text(field_type, value):
    """The comparable text of one submitted value, or None for "no answer"."""
    if field_type == "checkbox":
        return "true" if value is True else "false"
    if isinstance(value, (dict, list)):
        return None
    t = _text(value)
    return t if t else None


def _same(a, b):
    if a == b:
        return True
    na, nb = _num(a), _num(b)
    return na is not None and nb is not None and na == nb


def condition_holds(cond, answer):
    if answer is None:
        return False
    op, val = cond["op"], cond["value"]
    if op == "eq":
        return _same(answer, val)
    if op == "neq":
        return not _same(answer, val)
    if op == "in":
        return any(_same(answer, v) for v in val)
    a, b = _num(answer), _num(val)
    if a is None or b is None:
        return False
    return a >= b if op == "gte" else a <= b


def visible_keys(fields, answers):
    """fields: the form's active questions in order (dicts with field_key,
    field_type and optionally show_if). answers: {field_key: raw value}.
    Returns the set of field keys that are shown."""
    seen = {}
    shown = set()
    for f in fields:
        rule = f.get("show_if")
        ok = True
        if rule:
            ok = all(condition_holds(c, seen.get(c["field"])) for c in rule.get("all", []))
        if ok:
            shown.add(f["field_key"])
            seen[f["field_key"]] = answer_text(f["field_type"], answers.get(f["field_key"]))
        else:
            seen[f["field_key"]] = None
    return shown


# ---- hidden fields filled from the form link (?branch=dubai) ----

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_HIDDEN_NAMES = 10
MAX_HIDDEN_VALUE = 100


def parse_hidden_names(text):
    """'branch, source staff' -> (['branch','source','staff'], error_or_None)."""
    if not isinstance(text, str):
        return [], None
    names = []
    for n in re.split(r"[\s,;]+", text.strip()):
        if not n:
            continue
        if not NAME_RE.match(n):
            return [], "'%s' is not a valid name (letters, numbers, - and _ only, up to 40)" % n[:40]
        if n not in names:
            names.append(n)
    if len(names) > MAX_HIDDEN_NAMES:
        return [], "at most %d link fields" % MAX_HIDDEN_NAMES
    return names, None


def clean_hidden_values(raw, allowed):
    """Only listed names, plain text, at most 100 characters each."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for name in allowed:
        v = raw.get(name)
        if not isinstance(v, str):
            continue
        v = "".join(c for c in v if c >= " ").strip()[:MAX_HIDDEN_VALUE]
        if v:
            out[name] = v
    return out
