"""Smoke test: unfollow a single user by handle or URL.

Dumps HTML + screenshot to data/debug/ at each navigation checkpoint so the
failure mode can be inspected afterward without printing the page content
into stdout. Skips the production warmup delay.

Usage:
    poetry run python tests/unfollow-user.py <username-or-url>
    poetry run python tests/unfollow-user.py <username-or-url> --headful
"""

import argparse
import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from browser import launch_browser, check_login_status, dump_page  # noqa: E402
from soundcloud import (  # noqa: E402
    unfollow_user,
    username_from_url,
    is_datadome_captcha,
    _find_profile_follow_button,
    _button_state,
)


async def _checkpoint(page, label: str) -> None:
    base = await dump_page(page, label, force=True)
    captcha = await is_datadome_captcha(page)
    btn = await _find_profile_follow_button(page, timeout_ms=0)
    btn_state = await _button_state(btn) if btn else "absent"
    print(f"[checkpoint:{label}] url={page.url} captcha={captcha} button={btn_state} dump={base}")


async def main_async(target: str, headful: bool) -> int:
    user = username_from_url(target)
    url = f"https://soundcloud.com/{user}"
    print(f"[test] unfollow target: {url}  headful={headful}")
    run_id = time.strftime("%Y%m%d-%H%M%S")

    pw, context, page = await launch_browser(headless=not headful)
    try:
        await page.goto("https://soundcloud.com/", wait_until="domcontentloaded")
        await _checkpoint(page, f"{run_id}-unfollow-01-after-root")
        if not await check_login_status(page):
            print("[test] not logged in -- run: python src/main.py login", file=sys.stderr)
            return 2

        await page.goto(url, wait_until="domcontentloaded")
        await _checkpoint(page, f"{run_id}-unfollow-02-profile-domcontentloaded")
        await asyncio.sleep(2.0)
        await _checkpoint(page, f"{run_id}-unfollow-03-profile-after-2s")

        result = await unfollow_user(page, url, warmup=(0, 0))
        await _checkpoint(page, f"{run_id}-unfollow-04-after-click")
        print(f"[test] result: {result}")
        return 0 if result.get("ok") else 1
    finally:
        try:
            await context.close()
        except Exception:
            pass
        await pw.stop()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("target", help="username or full profile URL")
    p.add_argument("--headful", action="store_true")
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args.target, args.headful)))


if __name__ == "__main__":
    main()
