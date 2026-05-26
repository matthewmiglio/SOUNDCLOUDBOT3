"""Churn flow configuration. Edit these values to tune the bot.

The bot runs as a state machine on every cron invocation. Each run picks ONE
mode based on disk state + rate-limit windows:

  SCRAPE   - queue of users-to-follow on disk is below threshold
  UNFOLLOW - stale follows exist and unfollow caps not yet hit
  FOLLOW   - drain queue, subject to follow caps

Follow and unfollow caps are deliberately kept 1:1 so the bot stays at a
steady state instead of drifting net-positive or net-negative on followings.

NOTE: the bot's own SoundCloud handle is *not* here -- it lives in
data/profile.json (per-clone, gitignored) and is read via src/identity.py.
"""

# --- Rate limits (follow == unfollow on purpose; keeps net followings flat) ---
MAX_FOLLOWS_PER_HOUR   = 8
MAX_FOLLOWS_PER_DAY    = 30
MAX_UNFOLLOWS_PER_HOUR = 8
MAX_UNFOLLOWS_PER_DAY  = 30

# --- Unfollow policy ---
MAX_FOLLOW_AGE_DAYS = 10
UNFOLLOWS_PER_RUN   = 10

# --- Follow policy ---
FOLLOWS_PER_RUN = 10

# --- Follow queue (CSV on disk) ---
# Pending = rows in the CSV whose username has no successful follow in
# actions.log yet. When pending drops below the threshold, the next run is
# spent scraping instead of following. Pop order is random, not FIFO.
FOLLOW_QUEUE_PATH          = "data/follow_queue.csv"
FOLLOW_QUEUE_MIN_THRESHOLD = 100

# --- Discovery / scrape mode ---
# How many fresh candidates we aim to append to the queue per scrape run.
SCRAPE_BATCH_SIZE = 250
# If a scrape pass yields zero new candidates by bad luck (all dupes /
# private), retry with a fresh seed sample up to this many times. Captcha
# bails immediately regardless.
SCRAPE_MAX_RETRIES = 3

# Layer 1: pool of own followers to sub-sample seeds from (one api-v2 page).
RECENT_FOLLOWERS_POOL = 500
# Layer 1.5: random seeds drawn from the pool. Each costs one followers-page
# load, so this is the main rate-limit knob for scraping.
RANDOM_SEED_COUNT     = 15
# Layer 2: per seed, scrape this many of their followers then sample down to
# PER_SEED_FOLLOWERS_TOP_Y for the candidate pool.
PER_SEED_SCRAPE_MAX       = 500
PER_SEED_FOLLOWERS_TOP_Y  = 25

# --- Pacing ---
SECONDS_BETWEEN_FOLLOWS   = (60, 120)
SECONDS_BETWEEN_UNFOLLOWS = (15, 30)
SECONDS_BETWEEN_SEEDS     = (60, 120)
