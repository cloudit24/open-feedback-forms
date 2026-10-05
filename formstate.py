"""Open / scheduled / closed: one place that decides what state a form is in.

Pure functions (no database, no clock of their own) so they are easy to test.
The server asks this on every page load AND on every submission, so the rules
are enforced on the server, not only by the page."""

from datetime import date, datetime

OPEN = "open"
SCHEDULED = "scheduled"
CLOSED_DATE = "closed_date"
CLOSED_LIMIT = "closed_limit"


def _as_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def form_state(form, response_count, today=None):
    """Returns {"state", "opens_on", "expiry_date", "max_responses"}.

    Order matters: a form that has not opened yet is "scheduled" whatever
    else is set. An expiry date counts from its own day (same as before this
    release). The limit closes the form once the count reaches N."""
    form = form or {}
    today = today or datetime.now().date()
    opens_on = _as_date(form.get("opens_on"))
    expiry = _as_date(form.get("expiry_date"))
    limit = form.get("max_responses")
    try:
        limit = int(limit) if limit not in (None, "") else None
    except (TypeError, ValueError):
        limit = None
    if limit is not None and limit < 1:
        limit = None

    if opens_on and opens_on > today:
        state = SCHEDULED
    elif expiry and expiry <= today:
        state = CLOSED_DATE
    elif limit is not None and (response_count or 0) >= limit:
        state = CLOSED_LIMIT
    else:
        state = OPEN
    return {"state": state,
            "opens_on": opens_on.isoformat() if opens_on else None,
            "expiry_date": expiry.isoformat() if expiry else None,
            "max_responses": limit}
