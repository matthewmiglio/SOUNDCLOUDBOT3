"""Unauthenticated SoundCloud api-v2 client.

Pulls `window.__sc_hydration` JSON from a public profile HTML page to extract:
  - the user's numeric id
  - a public `client_id` token

Then talks to https://api-v2.soundcloud.com directly with stdlib urllib.
No browser, no auth, no captcha exposure.
"""

import json
import re
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
API = "https://api-v2.soundcloud.com"
WEB = "https://soundcloud.com"

_cached_client_id: str | None = None


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def _parse_hydration(html: str) -> list:
    m = re.search(r"window\.__sc_hydration\s*=\s*(\[.*?\]);", html, re.DOTALL)
    if not m:
        raise RuntimeError("hydration JSON not found in profile HTML")
    return json.loads(m.group(1))


def fetch_profile_meta(handle: str) -> dict:
    """Hit /<handle> and pull {user, client_id} out of the hydration JSON."""
    global _cached_client_id
    html = _http_get(f"{WEB}/{handle}").decode("utf-8", errors="ignore")
    entries = _parse_hydration(html)
    user = None
    client_id = None
    for e in entries:
        if e.get("hydratable") == "user" and isinstance(e.get("data"), dict):
            user = e["data"]
        elif e.get("hydratable") == "apiClient" and isinstance(e.get("data"), dict):
            client_id = e["data"].get("id")
    if user is None:
        raise RuntimeError(f"user hydration not found for {handle!r}")
    if client_id:
        _cached_client_id = client_id
    return {"user": user, "client_id": client_id or _cached_client_id}


def get_client_id() -> str:
    if _cached_client_id:
        return _cached_client_id
    fetch_profile_meta("discover")
    if not _cached_client_id:
        raise RuntimeError("could not obtain a client_id from hydration")
    return _cached_client_id


def api_get(path: str, **params) -> dict:
    params.setdefault("client_id", get_client_id())
    qs = urllib.parse.urlencode(params)
    raw = _http_get(f"{API}{path}?{qs}")
    return json.loads(raw.decode("utf-8", errors="ignore"))


def _ensure_client_id(url: str) -> str:
    # api-v2's next_href omits client_id, which 401s on the next call.
    parts = urllib.parse.urlsplit(url)
    qs = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    if "client_id" not in qs:
        qs["client_id"] = get_client_id()
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(qs), parts.fragment)
    )


def page_all(initial_path: str, limit: int = 50, max_pages: int = 20, **params) -> list:
    """Walk pagination via next_href until exhausted or max_pages reached."""
    params["limit"] = limit
    out: list = []
    data = api_get(initial_path, **params)
    out.extend(data.get("collection", []))
    next_href = data.get("next_href")
    pages = 1
    while next_href and pages < max_pages:
        raw = _http_get(_ensure_client_id(next_href))
        d = json.loads(raw.decode("utf-8", errors="ignore"))
        out.extend(d.get("collection", []))
        next_href = d.get("next_href")
        pages += 1
    return out
