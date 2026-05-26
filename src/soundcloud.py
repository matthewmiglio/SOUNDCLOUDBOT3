"""SoundCloud actions: list followers, follow, unfollow.

Selectors are based on soundcloud.com DOM as of 2026-05. Key anchors:
  - Follower list container: ul.lazyLoadingList__list
  - User row:                li.badgeList__item > .userBadgeListItem
  - Profile link:            a.userBadgeListItem__image[href="/{slug}"]
  - Follow button (header):  button.sc-button-follow
      not following: title/aria-label = "Follow" or "Follow back"
      following:     title/aria-label = "Unfollow" / "Following"
                     and class contains "sc-button-selected"
"""

import asyncio
import json
import os
import time
from urllib.parse import urlparse

from browser import human_delay, dump_page, ACTIONS_LOG, DATA_DIR
import human_browser


SC_BASE = "https://soundcloud.com"


def _log_action(action: str, profile_url: str, result: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "action": action,
        "profile_url": profile_url,
        "username": username_from_url(profile_url),
        "ok": result.get("ok"),
        "status": result.get("status"),
        "reason": result.get("reason", ""),
    }
    with open(ACTIONS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def username_from_url(url_or_handle: str) -> str:
    s = url_or_handle.strip()
    if s.startswith("@"):
        return s[1:]
    if "://" in s:
        path = urlparse(s).path.strip("/")
        return path.split("/")[0]
    return s.strip("/").split("/")[0]


async def is_datadome_captcha(page) -> bool:
    """Quick check for SoundCloud's DataDome bot-verification challenge.

    SoundCloud now serves DataDome as an iframe overlaid on the page after
    an offending API call (e.g. POST /me/followings/<id>). The challenge
    iframe loads from geo.captcha-delivery.com -- that domain is the most
    reliable marker. The older container selectors are kept as a fallback
    in case DataDome reverts to inline DOM challenges.
    """
    try:
        n = await page.locator(
            ', '.join([
                'iframe[src*="captcha-delivery.com"]',
                'iframe[title="Verification system"]',
                'iframe[id^="ddChallengeBody"]',
                '[data-dd-captcha-container]',
                '#captcha-container',
                '[data-dd-captcha-human-title]',
            ])
        ).count()
        return n > 0
    except Exception:
        return False


async def _wait_for_followers_list(page, timeout_ms: int = 15000) -> bool:
    try:
        await page.wait_for_selector(
            'ul.lazyLoadingList__list, .userBadgeListItem', timeout=timeout_ms
        )
        return True
    except Exception:
        return False


async def _collect_visible(page) -> list[dict]:
    rows = await page.eval_on_selector_all(
        'li.badgeList__item',
        """
        items => items.map(item => {
            let username = null;
            const a = item.querySelector('a.userBadgeListItem__image, a.userBadgeListItem__heading');
            if (a) {
                const href = a.getAttribute('href') || '';
                const m = href.match(/^\\/([^\\/?#]+)/);
                if (m) username = m[1];
            }
            return { username };
        })
        """,
    )
    out = []
    for r in rows:
        u = (r.get("username") or "").strip()
        if not u:
            continue
        out.append({"username": u, "is_private": False})
    return out


async def _scroll_and_collect(page, max_users: int | None = None) -> list[dict]:
    seen: dict[str, dict] = {}
    stagnant_rounds = 0
    last_count = 0

    while True:
        for r in await _collect_visible(page):
            if r["username"] not in seen:
                seen[r["username"]] = r
        if max_users is not None and len(seen) >= max_users:
            break
        if len(seen) == last_count:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
            last_count = len(seen)
        if stagnant_rounds >= 4:
            break
        # Slower scroll cadence: SoundCloud's DataDome detector treats fast
        # scroll-spam on the follower list as bot signal.
        await page.mouse.wheel(0, 1500)
        await human_delay(2.0, 4.0)

    items = list(seen.values())
    if max_users is not None:
        items = items[:max_users]
    return items


class CaptchaDetected(Exception):
    """Raised when SoundCloud's DataDome bot-verification page interrupts a scrape.

    The caller should stop the whole run -- continuing to hit pages just digs
    the rate-limit hole deeper.
    """


# When set > 0, if a captcha is detected we sleep this many seconds first,
# then re-check; if it's gone we continue, else we raise. Headful runs use
# this as a manual-solve window. Headless production sets it to 0.
CAPTCHA_GRACE_SECONDS: float = 5.0


async def _captcha_check_with_grace(page, where: str, dump_label: str) -> None:
    """If a captcha is currently shown, give the user `CAPTCHA_GRACE_SECONDS`
    to solve it manually. If still there afterward, dump page and raise.
    No-op when no captcha is present."""
    if not await is_datadome_captcha(page):
        return
    print(f"[captcha] detected at {where} -- waiting {CAPTCHA_GRACE_SECONDS}s for manual solve")
    if CAPTCHA_GRACE_SECONDS > 0:
        await asyncio.sleep(CAPTCHA_GRACE_SECONDS)
        if not await is_datadome_captcha(page):
            print(f"[captcha] cleared after grace window at {where}; continuing")
            return
    await dump_page(page, dump_label, force=True)
    raise CaptchaDetected(f"DataDome challenge at {where}")


async def list_followers(page, username: str, max_users: int | None = None) -> list[dict]:
    """Visit /{username}/followers and scrape rows. Returns [{username, profile_url, is_private}].

    Raises CaptchaDetected if SoundCloud serves the DataDome challenge.
    """
    url = f"{SC_BASE}/{username}/followers"
    await page.goto(url, wait_until="domcontentloaded")
    await human_delay(2.0, 3.5)
    await _captcha_check_with_grace(page, f"/{username}/followers", f"followers-{username}-captcha")
    if not await _wait_for_followers_list(page):
        await dump_page(page, f"followers-{username}-noload", force=True)
        return []
    rows = await _scroll_and_collect(page, max_users=max_users)
    return [
        {
            "username": r["username"],
            "profile_url": f"{SC_BASE}/{r['username']}",
            "is_private": r["is_private"],
        }
        for r in rows
    ]


async def get_profile_stats(page, username: str) -> dict:
    """Visit /{username} and parse followers/following counts.

    Reads from the `infoStats__statLink` title attrs (e.g. "1,872 followers",
    "Following 928 people"). Returns {followers: int|None, following: int|None}.
    """
    import re
    url = f"{SC_BASE}/{username}"
    try:
        await page.goto(url, wait_until="domcontentloaded")
        await human_delay(1.2, 2.4)
        followers = None
        following = None
        try:
            t = await page.locator('a.infoStats__statLink[href$="/followers"]').first.get_attribute("title")
            if t:
                m = re.search(r'([\d,]+)', t)
                if m:
                    followers = int(m.group(1).replace(",", ""))
        except Exception:
            pass
        try:
            t = await page.locator('a.infoStats__statLink[href$="/following"]').first.get_attribute("title")
            if t:
                m = re.search(r'([\d,]+)', t)
                if m:
                    following = int(m.group(1).replace(",", ""))
        except Exception:
            pass
        return {"followers": followers, "following": following}
    except Exception:
        return {"followers": None, "following": None}


async def list_following(page, username: str, max_users: int | None = None) -> list[dict]:
    url = f"{SC_BASE}/{username}/following"
    await page.goto(url, wait_until="domcontentloaded")
    await human_delay(2.0, 3.5)
    if not await _wait_for_followers_list(page):
        await dump_page(page, f"following-{username}-noload", force=True)
        return []
    rows = await _scroll_and_collect(page, max_users=max_users)
    return [
        {
            "username": r["username"],
            "profile_url": f"{SC_BASE}/{r['username']}",
            "is_private": r["is_private"],
        }
        for r in rows
    ]


async def _find_profile_follow_button(page, timeout_ms: int = 5000):
    """Return the profile header follow/unfollow button locator, or None.

    SoundCloud profile pages have one prominent sc-button-follow next to the
    user's name. Sidebar suggestions can also have sc-button-follow buttons,
    so we prefer the one inside the profile header region when possible.

    Waits up to `timeout_ms` for *any* sc-button-follow to appear before
    giving up -- the profile page renders async, and querying immediately
    after `domcontentloaded` will miss the button. Pass timeout_ms=0 for
    an immediate non-blocking check.
    """
    if timeout_ms > 0:
        try:
            await page.wait_for_selector('.sc-button-follow', timeout=timeout_ms)
        except Exception:
            return None
    for sel in [
        '.profileHeaderInfo .sc-button-follow',
        '.profileHero .sc-button-follow',
        'header .sc-button-follow',
        '.userInfoBar .sc-button-follow',
        '.sc-button-follow',
    ]:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0:
                return loc
        except Exception:
            pass
    return None


async def _button_state(button) -> str:
    """Returns 'follow' (not following), 'unfollow' (following), or 'unknown'."""
    try:
        cls = (await button.get_attribute("class")) or ""
        if "sc-button-selected" in cls:
            return "unfollow"
        title = (await button.get_attribute("title")) or ""
        aria = (await button.get_attribute("aria-label")) or ""
        t = (title + " " + aria).lower()
        if "unfollow" in t or t.strip() == "following":
            return "unfollow"
        if "follow" in t:
            return "follow"
    except Exception:
        pass
    return "unknown"


async def get_follow_state(page, profile_url: str) -> str:
    """Visit a profile and return 'follow', 'unfollow', or 'unknown'."""
    await page.goto(profile_url, wait_until="domcontentloaded")
    await human_delay(1.2, 2.4)
    btn = await _find_profile_follow_button(page)
    if not btn:
        return "unknown"
    return await _button_state(btn)


async def follow_user(
    page,
    profile_url: str,
    skip_private: bool = True,
    simulate_human: bool = True,
) -> dict:
    """Navigate to a profile and click Follow. Returns {ok, status, reason}.

    When `simulate_human` is True (default), routes through
    `human_browser.per_target_warmup` (scroll, play track, bezier mouse
    movement) instead of a plain sleep. Tests can pass False for raw speed.
    """
    result = await _follow_user_impl(page, profile_url, simulate_human=simulate_human)
    _log_action("follow", profile_url, result)
    return result


async def _follow_user_impl(page, profile_url: str, simulate_human: bool) -> dict:
    if simulate_human:
        await human_browser.per_target_warmup(page, profile_url)
        await _captcha_check_with_grace(page, f"follow goto {profile_url}", "follow-captcha")
    else:
        await page.goto(profile_url, wait_until="domcontentloaded")
        await _captcha_check_with_grace(page, f"follow goto {profile_url}", "follow-captcha")

    btn = await _find_profile_follow_button(page)
    if not btn:
        await dump_page(page, "follow-no-button", force=True)
        return {"ok": False, "status": "error", "reason": "follow button not found"}

    state = await _button_state(btn)
    if state == "unfollow":
        return {"ok": True, "status": "noop", "reason": "already following"}
    if state != "follow":
        return {"ok": False, "status": "error", "reason": f"unexpected button state: {state}"}

    try:
        if simulate_human:
            await human_browser.human_hover_and_click(page, btn)
        else:
            await btn.scroll_into_view_if_needed()
            await human_delay(0.3, 0.9)
            await btn.click()
    except Exception as e:
        return {"ok": False, "status": "error", "reason": f"click failed: {e}"}

    after_state = "unknown"
    for _ in range(10):
        await asyncio.sleep(2.0)
        # DataDome serves the captcha as a post-API-call iframe overlay --
        # the follow POST silently triggers it. Check every poll so we bail
        # the run instead of "successfully" returning state=follow forever.
        await _captcha_check_with_grace(
            page, f"follow post-click {profile_url}", "follow-post-click-captcha"
        )
        after = await _find_profile_follow_button(page, timeout_ms=0)
        after_state = await _button_state(after) if after else "unknown"
        if after_state == "unfollow":
            return {"ok": True, "status": "followed", "reason": ""}
    return {"ok": False, "status": "error", "reason": f"state after click: {after_state}"}


async def unfollow_user(
    page,
    profile_url: str,
    simulate_human: bool = True,
) -> dict:
    result = await _unfollow_user_impl(page, profile_url, simulate_human=simulate_human)
    _log_action("unfollow", profile_url, result)
    return result


async def _unfollow_user_impl(page, profile_url: str, simulate_human: bool) -> dict:
    if simulate_human:
        await human_browser.per_target_warmup(page, profile_url)
        await _captcha_check_with_grace(page, f"unfollow goto {profile_url}", "unfollow-captcha")
    else:
        await page.goto(profile_url, wait_until="domcontentloaded")
        await _captcha_check_with_grace(page, f"unfollow goto {profile_url}", "unfollow-captcha")

    btn = await _find_profile_follow_button(page)
    if not btn:
        await dump_page(page, "unfollow-no-button", force=True)
        return {"ok": False, "status": "error", "reason": "follow button not found"}

    state = await _button_state(btn)
    if state == "follow":
        return {"ok": True, "status": "noop", "reason": "not following"}
    if state != "unfollow":
        return {"ok": False, "status": "error", "reason": f"unexpected button state: {state}"}

    try:
        if simulate_human:
            await human_browser.human_hover_and_click(page, btn)
        else:
            await btn.scroll_into_view_if_needed()
            await human_delay(0.3, 0.9)
            await btn.click()
        await human_delay(0.6, 1.2)
    except Exception as e:
        return {"ok": False, "status": "error", "reason": f"click failed: {e}"}

    after_state = "unknown"
    for _ in range(10):
        await asyncio.sleep(2.0)
        await _captcha_check_with_grace(
            page, f"unfollow post-click {profile_url}", "unfollow-post-click-captcha"
        )
        after = await _find_profile_follow_button(page, timeout_ms=0)
        after_state = await _button_state(after) if after else "unknown"
        if after_state == "follow":
            return {"ok": True, "status": "unfollowed", "reason": ""}
    return {"ok": False, "status": "error", "reason": f"state after click: {after_state}"}
