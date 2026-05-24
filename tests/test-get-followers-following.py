"""Smoke test for src/get/followers.py + src/get/following.py.

Hits 10 profiles sequentially with NO sleep between calls. Goal is twofold:
  1. Confirm response shape (valid JSON, expected keys, counts that match
     the profile metadata).
  2. Surface any rate-limit behavior from api-v2 (HTTP 429, empty payloads,
     latency spikes, IP cool-down).

Run from the repo root:
    python tests/test-get-followers-following.py
"""

import json
import os
import sys
import time
import urllib.error

# Make stdout tolerant of non-cp1252 characters (SoundCloud usernames often
# contain emoji / non-Latin glyphs that would otherwise crash on Windows).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Make src/ importable when running this script directly.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
sys.path.insert(0, _SRC)

from get.followers import get_followers  # noqa: E402
from get.following import get_following  # noqa: E402


HANDLES = [
    "oxylawt",
    "bloodxo",
    "shiesty-ricch",
    "froyz-gang",
    "blvck2002",
    "wheroas",
    "dollhouseband",
    "slygster",
    "fl-breeze",
    "nemoglitchin",
]

MAX_USERS = 30


def _call(fn, handle: str) -> dict:
    t0 = time.monotonic()
    try:
        result = fn(handle, max_users=MAX_USERS)
        ok = True
        err = None
    except urllib.error.HTTPError as e:
        result = None
        ok = False
        err = {"type": "HTTPError", "code": e.code, "reason": str(e)}
    except Exception as e:
        result = None
        ok = False
        err = {"type": type(e).__name__, "reason": str(e)}
    elapsed = time.monotonic() - t0
    return {"ok": ok, "elapsed_s": round(elapsed, 3), "result": result, "err": err}


def _summary(result: dict, count_key: str) -> dict:
    if not result["ok"]:
        return {"ok": False, "elapsed_s": result["elapsed_s"], "err": result["err"]}
    r = result["result"]
    coll = r.get("collection", [])
    sample = coll[0] if coll else None
    return {
        "ok": True,
        "elapsed_s": result["elapsed_s"],
        "user_id": r.get("user_id"),
        count_key: r.get(count_key),
        "fetched": r.get("fetched"),
        "sample_keys": sorted(sample.keys())[:12] if sample else [],
        "first_user": (
            {
                "username": sample.get("username"),
                "permalink": sample.get("permalink"),
                "followers_count": sample.get("followers_count"),
            } if sample else None
        ),
    }


def main() -> int:
    report = []
    started = time.monotonic()
    for i, h in enumerate(HANDLES, 1):
        print(f"[{i}/{len(HANDLES)}] {h} ...", flush=True)

        f_call = _call(get_followers, h)
        g_call = _call(get_following, h)

        entry = {
            "handle": h,
            "followers": _summary(f_call, "followers_count"),
            "following": _summary(g_call, "followings_count"),
        }

        # Sanity check: fetched <= max(profile total, MAX_USERS)
        for kind, count_key in (("followers", "followers_count"),
                                ("following", "followings_count")):
            s = entry[kind]
            if s.get("ok"):
                total = s.get(count_key) or 0
                fetched = s.get("fetched") or 0
                expected_max = min(total, MAX_USERS)
                s["count_sanity_ok"] = fetched == expected_max
        report.append(entry)
        print(f"    followers: {entry['followers']}")
        print(f"    following: {entry['following']}")

    elapsed = time.monotonic() - started
    aggregate = {
        "total_handles":   len(HANDLES),
        "total_elapsed_s": round(elapsed, 2),
        "ok_followers":    sum(1 for e in report if e["followers"].get("ok")),
        "ok_following":    sum(1 for e in report if e["following"].get("ok")),
        "failures": [
            {"handle": e["handle"],
             "followers_err": e["followers"].get("err"),
             "following_err": e["following"].get("err")}
            for e in report
            if not e["followers"].get("ok") or not e["following"].get("ok")
        ],
        "sanity_failures": [
            e["handle"] for e in report
            if (e["followers"].get("ok") and not e["followers"].get("count_sanity_ok"))
            or (e["following"].get("ok") and not e["following"].get("count_sanity_ok"))
        ],
    }
    print("\n=== AGGREGATE ===")
    print(json.dumps(aggregate, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
