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

Idempotent — re-reads `data/actions.log` on every run, so duplicate follows
are skipped and rate limits self-enforce.

1. Read `data/actions.log`; if `MAX_FOLLOWS_PER_HOUR` or
   `MAX_FOLLOWS_PER_DAY` already met, exit early.
2. **Unfollow phase** — anyone followed > `MAX_FOLLOW_AGE_DAYS` ago that
   we haven't already unfollowed, up to `MAX_UNFOLLOWS_PER_RUN`.
3. **Discovery phase** — scrape `SEED_FOLLOWERS_TOP_X` of your followers,
   then `PER_SEED_FOLLOWERS_TOP_Y` of each seed's followers; dedupe; follow
   up to `FOLLOWS_PER_RUN_Z` while respecting live rate caps.

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

Register `cron/run-churn.ps1` with Windows Task Scheduler — 3h trigger plus
the wrapper's built-in 0-2h random jitter gives an effective 3-5h cadence.
The shared `SoundCloudBot3-Churn` task is staggered to `:15` so it doesn't
overlap with the Twitter tasks (`:00`, `:30`).
