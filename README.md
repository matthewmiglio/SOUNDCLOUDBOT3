# SoundCloudBot3

Playwright-driven SoundCloud follow/unfollow churn bot. Mirrors
[`TwitterBot3`](../TwitterBot3) — same churn logic, same `data/actions.log`
shape, same persistent browser-profile model. Every churn run uploads its
results to Supabase so progress can be tracked over time from the
[`SoundCloudTwitterBotsDashboard`](../SoundCloudTwitterBotsDashboard) Next.js
dashboard.

## Setup

```bash
poetry install
poetry run playwright install chromium
poetry run python src/main.py login   # one-time interactive login
```

Create a `.env` at the repo root (gitignored):

```env
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-jwt>
```

The service-role key is required because the bot writes to RLS-protected
tables. Never commit `.env` — it is in `.gitignore`.

## Commands

```bash
poetry run python src/main.py followers <username> [--max N]
poetry run python src/main.py follow <profile-url>
poetry run python src/main.py unfollow <profile-url>
poetry run python src/main.py churn [--dry-run] [--headful]
poetry run python src/main.py reconcile
```

## Churn flow

Each cron invocation is a state machine that picks ONE mode based on disk
state + rate-limit windows read from `data/actions.log`:

1. **Scrape** — if the on-disk follow queue (`data/follow_queue.csv`) has
   fewer than `FOLLOW_QUEUE_MIN_THRESHOLD` pending rows, harvest
   `SCRAPE_BATCH_SIZE` fresh candidates from `RANDOM_SEED_COUNT` seeds and
   append them.
2. **Unfollow** — else if there are follows older than
   `MAX_FOLLOW_AGE_DAYS` that haven't been unfollowed, and the hourly/daily
   unfollow caps aren't hit, unfollow up to `UNFOLLOWS_PER_RUN` of them.
3. **Follow** — else, while follow caps aren't hit, randomly pop up to
   `FOLLOWS_PER_RUN` rows from the queue and follow them.

Idempotent: `actions.log` is the source of truth for "already acted" and
for the rate-limit counters. The queue is append-only; a row is "pending"
iff its username has no successful follow recorded. Follow caps and
unfollow caps are kept 1:1 so net followings stay flat.

All tunables live in `src/config.py`.

## Supabase reporting

At the end of every non-dry-run `churn` invocation, `src/supabase_client.py`
posts to two PostgREST tables in the shared project
`rxwdtssnaymiebnhudix.supabase.co`:

- **`soundcloud_actions`** — every follow/unfollow attempt from this session
  (account, ts, action, status, ok, profile_url, username, reason). A unique
  constraint on `(account, ts, profile_url, action)` makes the upload
  idempotent.
- **`soundcloud_runs`** — one row per cron invocation summarising
  `session_followed`, `session_unfollowed`, current `profile_followers` /
  `profile_following`, and `exit_code`.

The `account` column is taken from `config.MY_USERNAME` so multiple
SoundCloud handles can share the same tables. The dashboard reads via
`security definer` RPCs (`bot_summary`, `actions_over_time`,
`runs_over_time`, `follower_trajectory`, `recent_actions`, `accounts`).

Email reporting (Resend) was removed — the dashboard replaces it.

## Cron

Register `cron/run-churn.ps1` with Windows Task Scheduler — 2h trigger plus
the wrapper's built-in 0-30min random jitter gives an effective 2h-2h30min
cadence. The `SoundCloudBot3-Churn` task is staggered to `:15` so it doesn't
overlap with the Twitter tasks (`:00`, `:30`). Run `cron/install-task.cmd`
to register/refresh the task.
