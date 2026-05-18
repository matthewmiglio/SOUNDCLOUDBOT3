import asyncio
import os
import random
import time

from playwright.async_api import async_playwright

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROFILE_DIR = os.path.join(ROOT, "data", "browser_profile")
DEBUG_DIR = os.path.join(ROOT, "data", "debug")
DATA_DIR = os.path.join(ROOT, "data")
ACTIONS_LOG = os.path.join(DATA_DIR, "actions.log")

DEBUG_MODE = False


def set_debug(enabled: bool):
    global DEBUG_MODE
    DEBUG_MODE = enabled


_STEALTH_JS = r"""
// Drop the obvious webdriver tell.
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
// Make navigator.plugins look populated.
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
// Make navigator.languages plausible.
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
// Spoof chrome object presence.
window.chrome = window.chrome || { runtime: {} };
// Patch permissions.query for notifications (common detection vector).
const _origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (_origQuery) {
  window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : _origQuery(parameters)
  );
}
"""


async def launch_browser(headless: bool = True):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    os.makedirs(DEBUG_DIR, exist_ok=True)
    pw = await async_playwright().start()
    launch_kwargs = dict(
        user_data_dir=PROFILE_DIR,
        headless=headless,
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--no-default-browser-check",
            "--no-first-run",
        ],
        ignore_default_args=["--enable-automation"],
    )
    # Prefer the user's installed Chrome over the bundled Chromium —
    # SoundCloud fingerprints Chromium specifically.
    try:
        context = await pw.chromium.launch_persistent_context(
            channel="chrome", **launch_kwargs
        )
    except Exception:
        context = await pw.chromium.launch_persistent_context(**launch_kwargs)
    await context.add_init_script(_STEALTH_JS)
    page = context.pages[0] if context.pages else await context.new_page()
    return pw, context, page


async def login_session():
    pw, context, page = await launch_browser(headless=False)
    await page.goto("https://soundcloud.com/signin", wait_until="domcontentloaded")
    print("Log in to SoundCloud. Close the browser window when you're done.")
    try:
        await context.pages[0].wait_for_event("close", timeout=0)
    except Exception:
        pass
    try:
        await context.close()
    except Exception:
        pass
    await pw.stop()


async def check_login_status(page) -> bool:
    """Detects an authenticated session by the presence of the user nav menu."""
    try:
        loc = page.locator('.header__userNav, .userNav, [aria-label="User"]').first
        if await loc.count() > 0:
            return True
    except Exception:
        pass
    try:
        sign_in = page.locator('a[href*="/signin"]').first
        if await sign_in.count() > 0:
            return False
    except Exception:
        pass
    url = page.url.lower()
    if "/signin" in url:
        return False
    return True


async def human_delay(min_s: float = 1.0, max_s: float = 3.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def dump_page(page, label: str, force: bool = False):
    if not (force or DEBUG_MODE):
        return None
    os.makedirs(DEBUG_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(DEBUG_DIR, f"{ts}-{label}")
    try:
        with open(base + ".html", "w", encoding="utf-8") as f:
            f.write(f"<!-- url: {page.url} -->\n")
            f.write(await page.content())
        await page.screenshot(path=base + ".png", full_page=True)
    except Exception as e:
        print(f"[debug] dump failed: {e}")
    return base
