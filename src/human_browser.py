"""Human-behavior helpers for the authenticated browser session.

All functions take a Playwright `page` and operate on it. No churn / soundcloud
imports so this module stays leaf-level.

The goal is to make the bot's session look like a real user to DataDome's
behavioral scoring: curved cursor paths, lognormal pauses, scrolling,
hovering, and playing a track before any follow/unfollow click.
"""

import asyncio
import math
import random
import time
from typing import Optional


def _lognormal_seconds(median_s: float, sigma: float = 0.4) -> float:
    """Lognormal sample with the given median. Sigma controls spread (0.4 ~
    moderate tail). Use for inter-action pauses -- humans bunch around a
    typical value with occasional long stalls, which uniform() can't capture."""
    mu = math.log(median_s)
    return random.lognormvariate(mu, sigma)


async def lognormal_sleep(median_s: float, sigma: float = 0.4) -> None:
    await asyncio.sleep(_lognormal_seconds(median_s, sigma))


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------

_cursor_pos: dict = {"x": 100.0, "y": 100.0}


def _cubic_bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return (
        u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
        u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1],
    )


async def bezier_mouse_move(page, to_xy: tuple[float, float], *,
                            from_xy: Optional[tuple[float, float]] = None,
                            steps: int = 40,
                            overshoot: bool = True) -> None:
    """Move the cursor from from_xy (or last known) to to_xy along a cubic
    bezier curve. Optionally overshoots the target by a few pixels and
    settles, which is what real cursors do when the user goes too fast."""
    if from_xy is None:
        from_xy = (_cursor_pos["x"], _cursor_pos["y"])
    fx, fy = from_xy
    tx, ty = to_xy
    dx, dy = tx - fx, ty - fy
    dist = math.hypot(dx, dy)
    if dist < 2:
        await page.mouse.move(tx, ty, steps=1)
        _cursor_pos["x"], _cursor_pos["y"] = tx, ty
        return

    # Control points: perpendicular jitter scaled by distance.
    nx, ny = -dy / dist, dx / dist
    j1 = random.uniform(-1, 1) * dist * 0.25
    j2 = random.uniform(-1, 1) * dist * 0.25
    c1 = (fx + dx * 0.33 + nx * j1, fy + dy * 0.33 + ny * j1)
    c2 = (fx + dx * 0.66 + nx * j2, fy + dy * 0.66 + ny * j2)

    overshoot_xy = (tx, ty)
    if overshoot and dist > 60:
        # Push past the target by 5-15px in the direction of motion.
        push = random.uniform(5, 15)
        overshoot_xy = (tx + dx / dist * push, ty + dy / dist * push)

    n = max(8, min(steps, int(dist / 4)))
    for i in range(1, n + 1):
        t = i / n
        x, y = _cubic_bezier((fx, fy), c1, c2, overshoot_xy, t)
        await page.mouse.move(x, y, steps=1)
        # Lognormal micro-pause between samples: a few ms most steps, with
        # the rare longer pause that mimics human jitter.
        await asyncio.sleep(_lognormal_seconds(0.01, 0.6))

    if overshoot_xy != (tx, ty):
        # Settle back to the exact target.
        for i in range(1, 6):
            t = i / 5
            x = overshoot_xy[0] + (tx - overshoot_xy[0]) * t
            y = overshoot_xy[1] + (ty - overshoot_xy[1]) * t
            await page.mouse.move(x, y, steps=1)
            await asyncio.sleep(0.02)

    _cursor_pos["x"], _cursor_pos["y"] = tx, ty


async def _locator_random_point(locator) -> Optional[tuple[float, float]]:
    """Random point inside the locator's bounding box, biased toward center."""
    try:
        box = await locator.bounding_box()
    except Exception:
        return None
    if not box:
        return None
    # Triangular distribution -> mass concentrated near the middle.
    rx = random.triangular(0.2, 0.8, 0.5)
    ry = random.triangular(0.2, 0.8, 0.5)
    return (box["x"] + box["width"] * rx, box["y"] + box["height"] * ry)


async def human_hover_and_click(page, locator) -> None:
    """Move cursor via bezier path to a random point inside the locator,
    dwell, then click with a small input delay.

    Scrolls into view BEFORE capturing the bounding box -- otherwise a
    button that's offscreen will yield stale coordinates and the click
    will land in empty space.
    """
    try:
        await locator.scroll_into_view_if_needed()
    except Exception:
        pass
    # Small settle pause so any scroll-triggered layout shifts complete
    # before we read the bounding box.
    await asyncio.sleep(0.2)
    pt = await _locator_random_point(locator)
    if pt is None:
        await locator.click()
        return
    await bezier_mouse_move(page, pt)
    await asyncio.sleep(_lognormal_seconds(0.4, 0.5))  # dwell
    # Use the locator's own click rather than raw page.mouse.click(x,y):
    # Playwright auto-waits for the element to be actionable (not covered
    # by overlays, not detached). The bezier above gives DataDome the
    # behavioral signal; the locator click guarantees the click lands.
    # We still pass a position offset and input delay for realism.
    try:
        box = await locator.bounding_box()
        if box:
            offx = pt[0] - box["x"]
            offy = pt[1] - box["y"]
            await locator.click(position={"x": offx, "y": offy},
                                delay=random.randint(40, 140))
            return
    except Exception:
        pass
    await locator.click(delay=random.randint(40, 140))


# ---------------------------------------------------------------------------
# Scrolling
# ---------------------------------------------------------------------------

async def random_scroll(page, *, n: tuple[int, int] = (2, 5)) -> None:
    """Scroll the page like a human: variable deltas, occasional up-scrolls,
    lognormal pauses."""
    count = random.randint(*n)
    for _ in range(count):
        # 80% scroll down, 20% scroll up a bit (reading-back behavior).
        if random.random() < 0.8:
            delta = _lognormal_seconds(500.0, 0.5)
        else:
            delta = -_lognormal_seconds(300.0, 0.4)
        try:
            await page.mouse.wheel(0, delta)
        except Exception:
            pass
        await lognormal_sleep(0.7, 0.5)


# ---------------------------------------------------------------------------
# Cookie / consent banner
# ---------------------------------------------------------------------------

_COOKIE_ACCEPT_SELECTORS = [
    '#onetrust-accept-btn-handler',
    '#accept-recommended-btn-handler',
    'button[aria-label*="Accept" i]',
    'button:has-text("Accept All")',
]


async def dismiss_cookie_banner(page, log=print) -> bool:
    """If a OneTrust / cookie consent banner is showing, click Accept so it
    doesn't block subsequent clicks. No-op when banner isn't present.

    SoundCloud serves OneTrust on first profile-page load. The banner
    intercepts clicks on play buttons / follow buttons below it.
    """
    for sel in _COOKIE_ACCEPT_SELECTORS:
        loc = page.locator(sel).first
        try:
            if await loc.count() == 0:
                continue
            if not await loc.is_visible():
                continue
        except Exception:
            continue
        try:
            await loc.click(timeout=2000)
            log(f"[human] dismissed cookie banner via {sel}")
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            log(f"[human] cookie banner click failed ({sel}): {e}")
            continue
    return False


# ---------------------------------------------------------------------------
# Track playback
# ---------------------------------------------------------------------------

_PLAY_SELECTORS = [
    'button.playButton[aria-label*="Play" i]',
    '.sc-button-play[title*="Play" i]',
    'button[aria-label^="Play"]',
    '.playButton',
    '.sc-button-play',
]


async def _find_play_button(page):
    for sel in _PLAY_SELECTORS:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0 and await loc.is_visible():
                return loc
        except Exception:
            continue
    return None


async def play_a_track(page, *, dwell: tuple[float, float] = (20.0, 40.0),
                      log=print) -> bool:
    """Find the first visible Play button on the current page, click it,
    let audio buffer + play for a `dwell` window. Returns True on success.
    Silently returns False if no play button is found (private profile,
    empty feed, etc.)."""
    btn = await _find_play_button(page)
    if not btn:
        log("[human] no play button visible -- skipping track play")
        return False
    try:
        await human_hover_and_click(page, btn)
    except Exception as e:
        log(f"[human] play click failed: {e}")
        return False
    lo, hi = dwell
    secs = random.uniform(lo, hi)
    log(f"[human] playing track ~{secs:.1f}s")
    await asyncio.sleep(secs)
    return True


# ---------------------------------------------------------------------------
# Composite flows
# ---------------------------------------------------------------------------

SC_BASE = "https://soundcloud.com"


async def session_prelude(page, log=print) -> None:
    """Once-per-session warmup. Browse the home feed, scroll, play a track,
    scroll more. Total ~45-75s. Run after login check, before any
    follow/unfollow.
    """
    log("[human] session prelude: navigating to /discover")
    await page.goto(f"{SC_BASE}/discover", wait_until="domcontentloaded")
    await lognormal_sleep(1.5, 0.4)
    await dismiss_cookie_banner(page, log)
    await random_scroll(page, n=(2, 4))
    await play_a_track(page, dwell=(20.0, 40.0), log=log)
    await random_scroll(page, n=(1, 2))
    log("[human] session prelude: done")


async def per_target_warmup(page, profile_url: str, log=print) -> None:
    """Pre-click warmup for a follow/unfollow target.

    Visits the profile (caller still needs to find/click the follow button
    afterwards -- we don't click it here). Scrolls, attempts to play their
    top track, ends near the follow button so the next click is in-context.
    """
    log(f"[human] per-target warmup: {profile_url}")
    await page.goto(profile_url, wait_until="domcontentloaded")
    await lognormal_sleep(1.5, 0.4)
    await dismiss_cookie_banner(page, log)
    await random_scroll(page, n=(1, 2))
    played = await play_a_track(page, dwell=(10.0, 20.0), log=log)
    if not played:
        await random_scroll(page, n=(1, 2))
    log("[human] per-target warmup: done")
