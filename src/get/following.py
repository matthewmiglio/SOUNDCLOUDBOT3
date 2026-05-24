"""Fetch who a user is following via SoundCloud's public api-v2 (no auth, no browser).

Returns a JSON-serializable dict:
    {
        "handle":           str,
        "user_id":          int,
        "followings_count": int,
        "fetched":          int,
        "collection":       [user_dict, ...],
    }
"""

import json
import sys

from ._client import fetch_profile_meta, page_all


def get_following(handle: str, max_users: int | None = 200) -> dict:
    meta = fetch_profile_meta(handle)
    user = meta["user"]
    uid = user["id"]
    total = user.get("followings_count")

    limit = min(200, max_users) if max_users else 200
    if max_users:
        max_pages = max(1, (max_users + limit - 1) // limit)
    else:
        max_pages = 20

    coll = page_all(f"/users/{uid}/followings", limit=limit, max_pages=max_pages)
    if max_users:
        coll = coll[:max_users]

    return {
        "handle": handle,
        "user_id": uid,
        "followings_count": total,
        "fetched": len(coll),
        "collection": coll,
    }


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m get.following <handle> [max]", file=sys.stderr)
        return 2
    handle = argv[0]
    max_users = int(argv[1]) if len(argv) > 1 else 50
    print(json.dumps(get_following(handle, max_users=max_users), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
