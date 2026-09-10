"""
Thin stdlib-only client for the public LibreTranslate API
(https://libretranslate.com) — used only when an admin adds a language
beyond the built-in English/Arabic pair, to pre-fill its labels instead of
leaving them all blank. No new pip dependency: just `urllib.request`, in
keeping with the rest of this project.

Every function here fails soft: a network problem, a bad response, or a
timeout is caught and turned into an empty result rather than raised, so a
translation-service hiccup never blocks an admin from adding the language —
worst case, they get 0 auto-translated strings and fill them in by hand,
same as before this feature existed.
"""

import json
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "https://libretranslate.com"
_TIMEOUT = 8

# LibreTranslate's /languages response has no directionality field, so a
# small static list stands in for it — anything not listed defaults to ltr,
# and the admin can flip it afterward in Language Labels either way.
RTL_CODES = {"ar", "he", "fa", "ur", "yi", "dv", "ps"}

_languages_cache = None


# libretranslate.com returns 403 Forbidden to Python's default urllib User-Agent
# (likely blocking obvious non-browser clients) — a plain, ordinary one gets through.
_HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (OpenFeedbackForms)"}


def _post_json(url, payload, api_key=None):
    if api_key:
        payload = dict(payload, api_key=api_key)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers=dict(_HEADERS, **{"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url):
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_languages(endpoint=None):
    """[{code, name, dir}, ...]. Cached for the process lifetime — this list
    changes rarely and every admin session would otherwise re-fetch it."""
    global _languages_cache
    if _languages_cache is not None:
        return _languages_cache
    endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")
    try:
        raw = _get_json(endpoint + "/languages")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return []
    out = []
    for entry in raw:
        code = entry.get("code")
        if not code:
            continue
        out.append({"code": code, "name": entry.get("name") or code,
                     "dir": "rtl" if code in RTL_CODES else "ltr"})
    out.sort(key=lambda l: l["name"])
    _languages_cache = out
    return out


def translate_batch(texts, target, source="en", endpoint=None, api_key=None):
    """texts: list of strings (order preserved, duplicates and empty
    strings allowed). Returns a same-length list — a string that failed to
    translate comes back as None so the caller can tell "translated" from
    "left blank" apart, rather than silently reusing the English text.
    The public libretranslate.com instance requires a free API key
    (portal.libretranslate.com) for this endpoint even though /languages is
    open — without one every string just comes back None, same as any other
    translation failure, so the caller's fallback path already covers it."""
    endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")
    results = [None] * len(texts)
    indices_to_send = [i for i, t in enumerate(texts) if t]
    if not indices_to_send:
        return results
    # Try one batched call first (recent LibreTranslate accepts an array
    # for `q`); fall back to one call per string if the server rejects it.
    try:
        body = _post_json(endpoint + "/translate", {
            "q": [texts[i] for i in indices_to_send],
            "source": source, "target": target, "format": "text"}, api_key=api_key)
        translated = body.get("translatedText")
        if isinstance(translated, list) and len(translated) == len(indices_to_send):
            for i, value in zip(indices_to_send, translated):
                results[i] = value
            return results
    except (urllib.error.URLError, TimeoutError, ValueError, OSError, KeyError):
        pass
    # Fall back to one call per string — but probe with just the first one:
    # if the *service itself* is unreachable or rejecting us (no API key,
    # for instance), every remaining call would fail the exact same way, so
    # hammering the endpoint hundreds more times only adds minutes of
    # latency for nothing. A per-string failure after a successful probe is
    # assumed to be that one string, not the whole service.
    probe_failed = False
    for n, i in enumerate(indices_to_send):
        if probe_failed:
            break
        try:
            body = _post_json(endpoint + "/translate", {
                "q": texts[i], "source": source, "target": target, "format": "text"}, api_key=api_key)
            results[i] = body.get("translatedText")
        except (urllib.error.URLError, TimeoutError, ValueError, OSError, KeyError):
            results[i] = None
            if n == 0:
                probe_failed = True
    return results
