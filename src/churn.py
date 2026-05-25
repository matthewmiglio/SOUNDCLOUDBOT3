"""Churn flow: unfollow stale follows + discover and follow new users.

Idempotent -- re-reading actions.log on every run means duplicate follows are
skipped and rate limits self-enforce. Safe to schedule on a cron.

Flow
----
1. Load actions.log -> compute follow state per user + rate-limit windows.
2. If MAX_FOLLOWS_PER_HOUR or MAX_FOLLOWS_PER_DAY already met -> exit early.
3. Unfollow phase: anyone we followed > MAX_FOLLOW_AGE_DAYS ago (and haven't
   already unfollowed), up to MAX_UNFOLLOWS_PER_RUN.
4. Discovery phase: SEED_FOLLOWERS_TOP_X of my followers -> for each, pull
   PER_SEED_FOLLOWERS_TOP_Y of *their* followers -> dedupe, filter, follow
   up to FOLLOWS_PER_RUN_Z (also respecting hour/day caps).
"""

import asyncio
import json
import os
import random
import time
import traceback
from datetime import datetime, timedelta, timezone

import config
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


def _api_rows(api_collection: list[dict]) -> list[dict]:
    """Convert api-v2 user dicts into the {username, profile_url} shape the
    follow/unfollow paths downstream expect."""
    return [
        {
            "username": u["permalink"],
            "display_name": u.get("username"),
            "profile_url": f"{SC_BASE}/{u['permalink']}",
        }
        for u in api_collection
    ]

SC_BASE = "https://soundcloud.com"
LOGS_DIR = os.path.join(os.path.dirname(ACTIONS_LOG), "..", "logs")
LOGS_DIR = os.path.abspath(LOGS_DIR)


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def successful_follow_count_since(actions: list[dict], since: datetime) -> int:
    n = 0
    for a in actions:
        if a.get("action") != "follow":
            continue
        if a.get("status") != "followed":
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
                "username": u,
                "profile_url": info["profile_url"],
                "followed_at": ts,
            })
    stale.sort(key=lambda x: x["followed_at"])
    return stale


async def _sleep_between(window):
    lo, hi = window
    await asyncio.sleep(random.uniform(lo, hi))


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


async def run_churn(dry_run: bool = False, headful: bool = False) -> int:
    logger = _SessionLogger("churn")
    log = logger.log
    started_at = _now()
    exit_code = 1
    stats = {"followed": 0, "unfollowed": 0, "profile_followers": None, "profile_following": None,
             "followed_urls": [], "unfollowed_urls": []}
    try:
        exit_code = await _run_churn_impl(log, stats, dry_run=dry_run, headful=headful)
    except Exception as e:
        # Crash inside the churn run -- record it so the dashboard can show
        # *why* the bot failed (not just that exit_code != 0). Best-effort:
        # if the error upload itself fails, we still want to fall through to
        # the regular run upload below.
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
        log(f"[churn] session log written to {logger.path}")
        if not dry_run:
            # Surface terminal-but-handled failure modes as their own kind so
            # the dashboard can tell "expected" trouble (reauth, captcha) from
            # an unexpected crash.
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


async def _run_churn_impl(log, stats: dict, dry_run: bool, headful: bool) -> int:
    actions = load_actions()
    now = _now()
    follows_last_hour = successful_follow_count_since(actions, now - timedelta(hours=1))
    follows_last_day = successful_follow_count_since(actions, now - timedelta(days=1))

    log(f"[churn] dry_run={dry_run} headful={headful}")
    log(f"[churn] follows in last hour: {follows_last_hour}/{config.MAX_FOLLOWS_PER_HOUR}")
    log(f"[churn] follows in last day:  {follows_last_day}/{config.MAX_FOLLOWS_PER_DAY}")

    if follows_last_hour >= config.MAX_FOLLOWS_PER_HOUR:
        log("[churn] hourly follow cap reached -- quitting.")
        return 0
    if follows_last_day >= config.MAX_FOLLOWS_PER_DAY:
        log("[churn] daily follow cap reached -- quitting.")
        return 0

    hour_room = config.MAX_FOLLOWS_PER_HOUR - follows_last_hour
    day_room = config.MAX_FOLLOWS_PER_DAY - follows_last_day
    follow_budget = min(config.FOLLOWS_PER_RUN_Z, hour_room, day_room)

    stale = stale_follows(actions, config.MAX_FOLLOW_AGE_DAYS)
    unfollow_budget = min(config.MAX_UNFOLLOWS_PER_RUN, len(stale))
    log(f"[churn] stale follows eligible to unfollow: {len(stale)} (will do up to {unfollow_budget})")
    log(f"[churn] follow budget this run: {follow_budget}")

    if dry_run:
        log("[churn] --- DRY RUN ---")
        log(f"[churn] would unfollow {unfollow_budget}:")
        for s in stale[:unfollow_budget]:
            age = (now - s["followed_at"]).days
            log(f"          {s['profile_url']}  (followed {age}d ago)")

    pw, context, page = await launch_browser(headless=not headful)
    try:
        await page.goto(f"{SC_BASE}/", wait_until="domcontentloaded")
        if not await check_login_status(page):
            log("[churn] not logged in -- run: python src/main.py login")
            return 2

        try:
            meta = api_fetch_profile_meta(get_username())
            u = meta["user"]
            stats["profile_followers"] = u.get("followers_count")
            stats["profile_following"] = u.get("followings_count")
            log(f"[churn] profile stats (api-v2): followers={stats['profile_followers']} following={stats['profile_following']}")
        except Exception as e:
            log(f"[churn] profile stats fetch failed: {e}")

        for s in stale[:unfollow_budget]:
            age = (now - s["followed_at"]).days
            if dry_run:
                continue
            log(f"[churn] unfollow {s['profile_url']} (followed {age}d ago)")
            try:
                result = await unfollow_user(page, s["profile_url"])
            except CaptchaDetected as e:
                log(f"[churn] BAIL: {e}. Aborting run on captcha.")
                return 3
            log(f"          ->{result}")
            if result.get("status") == "unfollowed":
                stats["unfollowed"] += 1
                stats["unfollowed_urls"].append(s["profile_url"])
            await _sleep_between(config.SECONDS_BETWEEN_UNFOLLOWS)

        already = already_acted_usernames(load_actions())
        already.add(get_username().lower())

        log(f"[churn] pulling recent {config.RECENT_FOLLOWERS_POOL} followers of {get_username()} as seed pool (api-v2)")
        try:
            api_pool = api_get_followers(get_username(), max_users=config.RECENT_FOLLOWERS_POOL)
            pool = _api_rows(api_pool["collection"])
        except Exception as e:
            log(f"[churn] BAIL: pool fetch failed: {e}.")
            return 3
        log(f"[churn] pool size: {len(pool)}")

        # Randomly pick RANDOM_SEED_COUNT seeds from the pool. Cap at pool size
        # in case SoundCloud handed us fewer than expected (rate-limit / private).
        if pool:
            picks = random.sample(pool, k=min(config.RANDOM_SEED_COUNT, len(pool)))
        else:
            picks = []
        log(f"[churn] randomly picked {len(picks)} seeds: {[p['username'] for p in picks]}")

        candidates: list[dict] = []
        seen = set()
        for i, seed in enumerate(picks):
            # Long human-style pause BEFORE each seed mining (except the first).
            if i > 0:
                lo, hi = config.SECONDS_BETWEEN_SEEDS
                pause = random.uniform(lo, hi)
                log(f"[churn] sleeping {pause:.1f}s before next seed")
                await asyncio.sleep(pause)

            log(f"[churn] mining up to {config.PER_SEED_SCRAPE_MAX} followers of {seed['username']} via api-v2, will sample {config.PER_SEED_FOLLOWERS_TOP_Y}")
            try:
                api_sub = api_get_followers(seed["username"], max_users=config.PER_SEED_SCRAPE_MAX)
                sub_all = _api_rows(api_sub["collection"])
            except Exception as e:
                log(f"[churn] api fetch for seed {seed['username']} failed: {e}. Skipping seed.")
                continue
            if not sub_all:
                log(f"[churn]   seed {seed['username']} has no followers; skipping")
                continue
            sub = random.sample(sub_all, k=min(config.PER_SEED_FOLLOWERS_TOP_Y, len(sub_all)))
            log(f"[churn]   scraped {len(sub_all)}, sampled {len(sub)}")
            for r in sub:
                u = r["username"].lower()
                if u in seen or u in already:
                    continue
                seen.add(u)
                candidates.append(r)

        # Shuffle so we don't always follow the first seed's followers first.
        random.shuffle(candidates)

        log(f"[churn] {len(candidates)} fresh candidates after dedup/filter")

        if dry_run:
            log(f"[churn] would follow up to {follow_budget}:")
            for c in candidates[:follow_budget]:
                log(f"          {c['profile_url']}")
            return 0

        for c in candidates:
            if stats["followed"] >= follow_budget:
                break
            acts = load_actions()
            if successful_follow_count_since(acts, _now() - timedelta(hours=1)) >= config.MAX_FOLLOWS_PER_HOUR:
                log("[churn] hourly cap hit mid-run -- stopping follows.")
                break
            if successful_follow_count_since(acts, _now() - timedelta(days=1)) >= config.MAX_FOLLOWS_PER_DAY:
                log("[churn] daily cap hit mid-run -- stopping follows.")
                break

            log(f"[churn] follow {c['profile_url']}")
            try:
                result = await follow_user(page, c["profile_url"])
            except CaptchaDetected as e:
                log(f"[churn] BAIL: {e}. Aborting run on captcha.")
                return 3
            log(f"          ->{result}")
            if result.get("status") == "rate_limited":
                log("[churn] rate-limited. Stopping follow loop for this session.")
                break
            if result.get("status") == "followed":
                stats["followed"] += 1
                stats["followed_urls"].append(c["profile_url"])
            await _sleep_between(config.SECONDS_BETWEEN_FOLLOWS)

        log(f"[churn] done. unfollowed={stats['unfollowed']}, followed={stats['followed']}")
        return 0
    finally:
        try:
            await context.close()
        except Exception:
            pass
        await pw.stop()
