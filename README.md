# SoundCloudBot3

Playwright-driven SoundCloud follow/unfollow churn bot. Mirrors TwitterBot3.

## Setup

```
poetry install
poetry run playwright install chromium
poetry run python src/main.py login   # one-time interactive login
```

## Commands

```
poetry run python src/main.py followers <username> [--max N]
poetry run python src/main.py follow <profile-url>
poetry run python src/main.py unfollow <profile-url>
poetry run python src/main.py churn [--dry-run] [--headful]
poetry run python src/main.py reconcile
```

## Cron

Register `cron/run-churn.ps1` with Windows Task Scheduler. 3h trigger + built-in 0-2h random jitter -> effective 3-5h cadence.
