"""Churn state machine: each cron run picks ONE of {scrape, unfollow, follow}.

Mode selection (in order):
  1. If pending queue rows < FOLLOW_QUEUE_MIN_THRESHOLD  -> SCRAPE
  2. Else if stale follows exist and unfollow caps OK    -> UNFOLLOW
  3. Else if follow caps OK                              -> FOLLOW
  4. Else                                                -> NOOP exit 0

Idempotent: actions.log is the source of truth for rate-limit windows and
'already acted' membership; the on-disk follow queue is just a buffer.
"""

import asyncio
import json
import os
import random
import time
import traceback
from datetime import datetime, timedelta, timezone

import config
import follow_queue
import human_browser
from browser import (
    launch_browser,
    check_login_status,
    set_debug,
    human_delay,
    ACTIONS_LOG,
)
from soundcloud import (
    list_followers,
    follow_user,
    unfollow_user,
    username_from_url,
    get_follow_state,
    get_profile_stats,
    CaptchaDetected,
)
from get.followers import get_followers as api_get_followers
from get._client import fetch_profile_meta as api_fetch_profile_meta
from supabase_client import upload_actions, upload_run, upload_error
from identity import get_username


SC_BASE = "https://soundcloud.com"
LOGS_DIR = os.path.abspath(os.path.join(os.path.dirname(ACTIONS_LOG), "..", "logs"))


def _api_rows(api_collection: list[dict]) -> list[dict]:
    return [
        {
            "username":     u["permalink"],
            "display_name": u.get("username"),
            "profile_url":  f"{SC_BASE}/{u['permalink']}",
        }
        for u in api_collection
    ]


class _SessionLogger:
    def __init__(self, prefix: str):
        os.makedirs(LOGS_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        self.path = os.path.join(LOGS_DIR, f"{prefix}-{ts}.log")
        self._fh = open(self.path, "a", encoding="utf-8")

    def log(self, msg: str = ""):
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {msg}"
        print(msg)
        try:
            self._fh.write(line + "\n")
            self._fh.flush()
        except Exception:
            pass

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


def _parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_actions() -> list[dict]:
    if not os.path.exists(ACTIONS_LOG):
        return []
    out = []
    with open(ACTIONS_LOG, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            entry["_ts"] = _parse_ts(entry.get("timestamp"))
            out.append(entry)
    return out


def _count_since(actions: list[dict], action: str, status: str, since: datetime) -> int:
    n = 0
    for a in actions:
        if a.get("action") != action or a.get("status") != status:
            continue
        ts = a.get("_ts")
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= since:
            n += 1
    return n


def already_acted_usernames(actions: list[dict]) -> set[str]:
    s = set()
    for a in actions:
        u = (a.get("username") or "").lower()
        if not u:
            continue
        if a.get("action") == "follow":
            s.add(u)
        if a.get("action") == "unfollow" and a.get("status") == "unfollowed":
            s.add(u)
    return s


def stale_follows(actions: list[dict], max_age_days: int) -> list[dict]:
    now = _now()
    cutoff = now - timedelta(days=max_age_days)

    sorted_actions = sorted(
        [a for a in actions if a.get("_ts") is not None],
        key=lambda a: a["_ts"],
    )
    state: dict[str, dict] = {}
    for a in sorted_actions:
        u = (a.get("username") or "").lower()
        if not u:
            continue
        action = a.get("action")
        status = a.get("status")
        if action == "follow" and status == "followed":
            state[u] = {
                "state": "following",
                "ts": a["_ts"],
                "profile_url": a.get("profile_url") or f"{SC_BASE}/{u}",
            }
        elif action == "unfollow" and status == "unfollowed":
            state[u] = {
                "state": "unfollowed",
                "ts": a["_ts"],
                "profile_url": a.get("profile_url") or f"{SC_BASE}/{u}",
            }

    stale = []
    for u, info in state.items():
        if info["state"] != "following":
            continue
        ts = info["ts"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts <= cutoff:
            stale.append({
                "username":    u,
                "profile_url": info["profile_url"],
                "followed_at": ts,
            })
    stale.sort(key=lambda x: x["followed_at"])
    return stale


async def _sleep_between(window):
    lo, hi = window
    await asyncio.sleep(random.uniform(lo, hi))


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------

def _pick_mode(log, actions: list[dict]) -> tuple[str, dict]:
    now = _now()
    follows_h   = _count_since(actions, "follow",   "followed",   now - timedelta(hours=1))
    follows_d   = _count_since(actions, "follow",   "followed",   now - timedelta(days=1))
    unfollows_h = _count_since(actions, "unfollow", "unfollowed", now - timedelta(hours=1))
    unfollows_d = _count_since(actions, "unfollow", "unfollowed", now - timedelta(days=1))

    already = already_acted_usernames(actions)
    pending = follow_queue.pending(already)
    stale   = stale_follows(actions, config.MAX_FOLLOW_AGE_DAYS)

    meta = {
        "follows_h": follows_h, "follows_d": follows_d,
        "unfollows_h": unfollows_h, "unfollows_d": unfollows_d,
        "pending": len(pending), "stale": len(stale),
    }
    log(f"[churn] follows  h={follows_h}/{config.MAX_FOLLOWS_PER_HOUR}  d={follows_d}/{config.MAX_FOLLOWS_PER_DAY}")
    log(f"[churn] unfollows h={unfollows_h}/{config.MAX_UNFOLLOWS_PER_HOUR}  d={unfollows_d}/{config.MAX_UNFOLLOWS_PER_DAY}")
    log(f"[churn] queue pending={len(pending)} (threshold {config.FOLLOW_QUEUE_MIN_THRESHOLD})  stale follows={len(stale)}")

    if len(pending) < config.FOLLOW_QUEUE_MIN_THRESHOLD:
        return "scrape", meta

    if stale:
        if unfollows_d >= config.MAX_UNFOLLOWS_PER_DAY:
            log("[churn] unfollow daily cap reached -- skipping unfollow mode.")
        elif unfollows_h >= config.MAX_UNFOLLOWS_PER_HOUR:
            log("[churn] unfollow hourly cap reached -- skipping unfollow mode.")
        else:
            return "unfollow", meta

    if follows_d >= config.MAX_FOLLOWS_PER_DAY:
        log("[churn] follow daily cap reached -- nothing to do.")
        return "noop", meta
    if follows_h >= config.MAX_FOLLOWS_PER_HOUR:
        log("[churn] follow hourly cap reached -- nothing to do.")
        return "noop", meta
    if not pending:
        log("[churn] follow queue empty -- nothing to do.")
        return "noop", meta
    return "follow", meta


# ---------------------------------------------------------------------------
# Public entrypoints
# ---------------------------------------------------------------------------

async def run_churn(dry_run: bool = False, headful: bool = False) -> int:
    logger = _SessionLogger("churn")
    log = logger.log
    started_at = _now()
    exit_code = 1
    stats = {"followed": 0, "unfollowed": 0, "scraped": 0, "mode": "unknown",
             "profile_followers": None, "profile_following": None,
             "followed_urls": [], "unfollowed_urls": []}
    try:
        exit_code = await _run_churn_impl(log, stats, dry_run=dry_run, headful=headful)
    except Exception as e:
        tb = traceback.format_exc()
        log(f"[churn] CRASH: {type(e).__name__}: {e}")
        log(tb)
        if not dry_run:
            try:
                upload_error({
                    "account":        get_username(),
                    "source":         "churn",
                    "kind":           "crash",
                    "exit_code":      1,
                    "message":        f"{type(e).__name__}: {e}",
                    "traceback":      tb,
                    "run_started_at": started_at.isoformat(),
                })
            except Exception:
                pass
    finally:
        log(f"[churn] mode={stats['mode']} scraped={stats['scraped']} followed={stats['followed']} unfollowed={stats['unfollowed']}")
        log(f"[churn] session log written to {logger.path}")
        if not dry_run:
            if exit_code in (2, 3):
                try:
                    upload_error({
                        "account":        get_username(),
                        "source":         "churn",
                        "kind":           "session_expired" if exit_code == 2 else "captcha",
                        "exit_code":      exit_code,
                        "message":        ("session expired -- needs reauth"
                                           if exit_code == 2 else
                                           "captcha detected, aborted run"),
                        "run_started_at": started_at.isoformat(),
                    })
                except Exception:
                    pass
            new_rows = [
                {
                    "account":     get_username(),
                    "ts":          a["timestamp"],
                    "action":      a.get("action"),
                    "status":      a.get("status"),
                    "ok":          bool(a.get("ok")),
                    "profile_url": a.get("profile_url"),
                    "username":    a.get("username"),
                    "reason":      a.get("reason") or "",
                }
                for a in load_actions()
                if a.get("_ts") is not None and a["_ts"] >= started_at - timedelta(minutes=1)
            ]
            r1 = upload_actions(new_rows)
            log(f"[churn] supabase actions upload: ok={r1.get('ok')} status={r1.get('status') or r1.get('error')} rows={len(new_rows)}")
            r2 = upload_run({
                "account":            get_username(),
                "started_at":         started_at.isoformat(),
                "finished_at":        _now().isoformat(),
                "session_followed":   stats["followed"],
                "session_unfollowed": stats["unfollowed"],
                "profile_followers":  stats["profile_followers"],
                "profile_following":  stats["profile_following"],
                "exit_code":          exit_code,
            })
            log(f"[churn] supabase run upload:     ok={r2.get('ok')} status={r2.get('status') or r2.get('error')}")
        logger.close()
    return exit_code


async def _open_browser_logged_in(log, headful: bool):
    pw, context, page = await launch_browser(headless=not headful)
    await page.goto(f"{SC_BASE}/", wait_until="domcontentloaded")
    if not await check_login_status(page):
        log("[churn] not logged in -- run: python src/main.py login")
        return pw, context, page, False
    return pw, context, page, True


async def _capture_profile_stats(log, stats: dict):
    try:
        meta = api_fetch_profile_meta(get_username())
        u = meta["user"]
        stats["profile_followers"] = u.get("followers_count")
        stats["profile_following"] = u.get("followings_count")
        log(f"[churn] profile stats: followers={stats['profile_followers']} following={stats['profile_following']}")
    except Exception as e:
        log(f"[churn] profile stats fetch failed: {e}")


async def _run_churn_impl(log, stats: dict, dry_run: bool, headful: bool) -> int:
    log(f"[churn] dry_run={dry_run} headful={headful}")
    actions = load_actions()
    mode, meta = _pick_mode(log, actions)
    stats["mode"] = mode
    log(f"[churn] mode selected: {mode}")

    if mode == "noop":
        return 0

    if dry_run:
        log("[churn] --- DRY RUN -- not opening browser ---")
        return 0

    pw, context, page, logged_in = await _open_browser_logged_in(log, headful)
    try:
        if not logged_in:
            return 2
        await _capture_profile_stats(log, stats)

        if mode == "scrape":
            # Scrape hits unauthenticated api-v2 via urllib -- no need to
            # warm the browser session, no captcha exposure.
            return await _do_scrape(log, page, stats)

        # Once-per-session human warmup before any follow/unfollow.
        # Looks like a real user browsing the feed and playing a track
        # before taking an action.
        try:
            await human_browser.session_prelude(page, log)
        except Exception as e:
            log(f"[churn] session prelude failed (continuing anyway): {e}")

        if mode == "unfollow":
            return await _do_unfollow(log, page, stats, actions)
        if mode == "follow":
            return await _do_follow(log, page, stats, actions)
        return 0
    finally:
        try:
            await context.close()
        except Exception:
            pass
        await pw.stop()


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

async def _scrape_once(log, already: set[str]) -> list[dict]:
    """One full scrape pass: pool -> seeds -> per-seed sample -> dedup."""
    log(f"[churn/scrape] pulling pool of {config.RECENT_FOLLOWERS_POOL} of own followers")
    api_pool = api_get_followers(get_username(), max_users=config.RECENT_FOLLOWERS_POOL)
    pool = _api_rows(api_pool["collection"])
    log(f"[churn/scrape] pool size: {len(pool)}")
    if not pool:
        return []

    picks = random.sample(pool, k=min(config.RANDOM_SEED_COUNT, len(pool)))
    log(f"[churn/scrape] picked {len(picks)} seeds: {[p['username'] for p in picks]}")

    candidates: list[dict] = []
    seen = set()
    for i, seed in enumerate(picks):
        # No seed-pause: scraping hits the unauthenticated api-v2 with a
        # rotated UA per request, no DataDome risk, no rate-limit cost.
        log(f"[churn/scrape] mining up to {config.PER_SEED_SCRAPE_MAX} followers of {seed['username']}")
        try:
            api_sub = api_get_followers(seed["username"], max_users=config.PER_SEED_SCRAPE_MAX)
            sub_all = _api_rows(api_sub["collection"])
        except Exception as e:
            log(f"[churn/scrape] api fetch for seed {seed['username']} failed: {e}. Skipping.")
            continue
        if not sub_all:
            continue
        sub = random.sample(sub_all, k=min(config.PER_SEED_FOLLOWERS_TOP_Y, len(sub_all)))
        for r in sub:
            u = r["username"].lower()
            if u in seen or u in already:
                continue
            seen.add(u)
            candidates.append(r)
    return candidates


async def _do_scrape(log, page, stats: dict) -> int:
    actions = load_actions()
    already = already_acted_usernames(actions)
    already.add(get_username().lower())
    already.update(r["username"] for r in follow_queue.load_all())

    target = config.SCRAPE_BATCH_SIZE
    collected: list[dict] = []
    attempts = 0
    while len(collected) < target and attempts < config.SCRAPE_MAX_RETRIES:
        attempts += 1
        log(f"[churn/scrape] attempt {attempts}/{config.SCRAPE_MAX_RETRIES} (have {len(collected)}/{target})")
        try:
            batch = await _scrape_once(log, already)
        except CaptchaDetected as e:
            log(f"[churn/scrape] BAIL: {e}. Captcha -- exiting 0 (logged to db).")
            return 3
        for r in batch:
            already.add(r["username"].lower())
        collected.extend(batch)
        if not batch:
            log("[churn/scrape] zero new candidates this pass; retrying with fresh seeds.")

    written = follow_queue.append(collected[:target])
    stats["scraped"] = written
    log(f"[churn/scrape] appended {written} new rows to follow queue (collected={len(collected)})")
    return 0


async def _do_unfollow(log, page, stats: dict, actions: list[dict]) -> int:
    stale = stale_follows(actions, config.MAX_FOLLOW_AGE_DAYS)
    budget = min(config.UNFOLLOWS_PER_RUN, len(stale))
    log(f"[churn/unfollow] {len(stale)} stale, will unfollow up to {budget}")

    for s in stale[:budget]:
        # Re-check caps mid-run; if hourly cap hit, stop.
        acts = load_actions()
        now = _now()
        if _count_since(acts, "unfollow", "unfollowed", now - timedelta(hours=1)) >= config.MAX_UNFOLLOWS_PER_HOUR:
            log("[churn/unfollow] hourly cap hit mid-run -- stopping.")
            break
        if _count_since(acts, "unfollow", "unfollowed", now - timedelta(days=1)) >= config.MAX_UNFOLLOWS_PER_DAY:
            log("[churn/unfollow] daily cap hit mid-run -- stopping.")
            break

        age = (now - s["followed_at"]).days
        log(f"[churn/unfollow] {s['profile_url']} (followed {age}d ago)")
        try:
            result = await unfollow_user(page, s["profile_url"])
        except CaptchaDetected as e:
            log(f"[churn/unfollow] BAIL: {e}.")
            return 3
        log(f"          -> {result}")
        if result.get("status") == "unfollowed":
            stats["unfollowed"] += 1
            stats["unfollowed_urls"].append(s["profile_url"])
        await _sleep_between(config.SECONDS_BETWEEN_UNFOLLOWS)
    return 0


async def _do_follow(log, page, stats: dict, actions: list[dict]) -> int:
    already = already_acted_usernames(actions)
    already.add(get_username().lower())
    pending = follow_queue.pending(already)
    if not pending:
        log("[churn/follow] queue empty after dedup against actions.log")
        return 0

    random.shuffle(pending)
    budget = config.FOLLOWS_PER_RUN
    log(f"[churn/follow] {len(pending)} pending, will follow up to {budget} (random order)")

    for c in pending:
        if stats["followed"] >= budget:
            break
        acts = load_actions()
        now = _now()
        if _count_since(acts, "follow", "followed", now - timedelta(hours=1)) >= config.MAX_FOLLOWS_PER_HOUR:
            log("[churn/follow] hourly cap hit mid-run -- stopping.")
            break
        if _count_since(acts, "follow", "followed", now - timedelta(days=1)) >= config.MAX_FOLLOWS_PER_DAY:
            log("[churn/follow] daily cap hit mid-run -- stopping.")
            break

        log(f"[churn/follow] {c['profile_url']}")
        try:
            result = await follow_user(page, c["profile_url"])
        except CaptchaDetected as e:
            log(f"[churn/follow] BAIL: {e}.")
            return 3
        log(f"          -> {result}")
        if result.get("status") == "rate_limited":
            log("[churn/follow] rate-limited -- stopping for this run.")
            break
        if result.get("status") == "followed":
            stats["followed"] += 1
            stats["followed_urls"].append(c["profile_url"])
        await _sleep_between(config.SECONDS_BETWEEN_FOLLOWS)
    return 0


# ---------------------------------------------------------------------------
# Reconcile (unchanged from previous version)
# ---------------------------------------------------------------------------

async def run_reconcile(headful: bool = False) -> int:
    if not os.path.exists(ACTIONS_LOG):
        print("[reconcile] no log file at", ACTIONS_LOG)
        return 0

    with open(ACTIONS_LOG, "r", encoding="utf-8") as f:
        raw_lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    entries = []
    for ln in raw_lines:
        try:
            entries.append(json.loads(ln))
        except Exception:
            entries.append(None)

    targets = [i for i, e in enumerate(entries)
               if e and e.get("action") == "follow" and e.get("status") == "error"]
    print(f"[reconcile] {len(targets)} follow entries with status=error to verify")
    if not targets:
        return 0

    pw, context, page = await launch_browser(headless=not headful)
    try:
        await page.goto(f"{SC_BASE}/", wait_until="domcontentloaded")
        if not await check_login_status(page):
            print("[reconcile] not logged in -- run: python src/main.py login")
            return 2

        changed = 0
        for i in targets:
            e = entries[i]
            url = e.get("profile_url")
            print(f"[reconcile] checking {url}")
            state = await get_follow_state(page, url)
            print(f"             actual state -> {state}")
            if state == "unfollow":
                e["ok"] = True
                e["status"] = "followed"
                e["reason"] = f"reconciled ({state})"
                changed += 1
            elif state == "follow":
                e["reason"] = "reconciled (still not following)"
            else:
                e["reason"] = f"reconciled (state={state})"
            await human_delay(1.5, 3.5)

        with open(ACTIONS_LOG, "w", encoding="utf-8") as f:
            for orig_line, e in zip(raw_lines, entries):
                if e is None:
                    f.write(orig_line + "\n")
                else:
                    f.write(json.dumps(e) + "\n")
        print(f"[reconcile] done. updated {changed} entries to status=followed.")
        return 0
    finally:
        try:
            await context.close()
        except Exception:
            pass
        await pw.stop()
