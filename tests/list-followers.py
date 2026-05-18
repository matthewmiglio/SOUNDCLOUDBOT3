"""Smoke test: list the top N followers of a user.

Usage:
    poetry run python tests/list-followers.py <username-or-url> [--max 25]
"""

import argparse
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from browser import launch_browser, check_login_status  # noqa: E402
from soundcloud import list_followers, username_from_url  # noqa: E402


async def main_async(target: str, max_users: int, headful: bool) -> int:
    user = username_from_url(target)
    print(f"[test] listing up to {max_users} followers of {user}")

    pw, context, page = await launch_browser(headless=not headful)
    try:
        await page.goto("https://soundcloud.com/", wait_until="domcontentloaded")
        if not await check_login_status(page):
            print("[test] not logged in -- run: python src/main.py login", file=sys.stderr)
            return 2
        rows = await list_followers(page, user, max_users=max_users)
        print(f"[test] got {len(rows)} rows")
        for r in rows:
            print(f"  {r['username']:<28}  {r['profile_url']}")
        return 0 if rows else 1
    finally:
        try:
            await context.close()
        except Exception:
            pass
        await pw.stop()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("target", nargs="?", default="bloodxo", help="username or full profile URL")
    p.add_argument("--max", type=int, default=25)
    p.add_argument("--headful", action="store_true")
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args.target, args.max, args.headful)))


if __name__ == "__main__":
    main()
