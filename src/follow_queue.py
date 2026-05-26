"""On-disk CSV of users-to-follow (the scrape -> follow handoff).

Append-only. A row's "pending" status is derived dynamically from
actions.log: a row is pending iff its username has no successful follow
attempt recorded. This keeps the queue idempotent -- safe to re-scan, no
need to mutate rows when we follow them.
"""

import csv
import os
from datetime import datetime, timezone

import config


def _path() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        config.FOLLOW_QUEUE_PATH,
    )


def _ensure_file() -> str:
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if not os.path.exists(p):
        with open(p, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["username", "profile_url", "scraped_at"])
    return p


def load_all() -> list[dict]:
    p = _ensure_file()
    out = []
    with open(p, "r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            u = (row.get("username") or "").strip().lower()
            if not u:
                continue
            out.append({
                "username":    u,
                "profile_url": (row.get("profile_url") or "").strip(),
                "scraped_at":  (row.get("scraped_at") or "").strip(),
            })
    return out


def append(rows: list[dict]) -> int:
    """Append rows, skipping any username already in the file. Returns the
    number actually written."""
    if not rows:
        return 0
    p = _ensure_file()
    existing = {r["username"] for r in load_all()}
    to_write = []
    seen = set()
    for r in rows:
        u = (r.get("username") or "").strip().lower()
        if not u or u in existing or u in seen:
            continue
        seen.add(u)
        to_write.append({
            "username":    u,
            "profile_url": r.get("profile_url") or f"https://soundcloud.com/{u}",
            "scraped_at":  datetime.now(timezone.utc).isoformat(),
        })
    if not to_write:
        return 0
    with open(p, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["username", "profile_url", "scraped_at"])
        for r in to_write:
            w.writerow(r)
    return len(to_write)


def pending(already_acted: set[str]) -> list[dict]:
    """Rows whose username we have NOT yet followed (or otherwise acted on)."""
    return [r for r in load_all() if r["username"] not in already_acted]
