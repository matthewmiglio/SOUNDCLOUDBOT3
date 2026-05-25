"""Churn flow configuration. Edit these values to tune the bot.

NOTE: the bot's own SoundCloud handle is *not* here -- it lives in
data/profile.json (per-clone, gitignored) and is read via src/identity.py.
Keeping tunables separate from identity means this file is byte-identical
between clones and won't drift on `git pull`.
"""

# --- Rate limits ---
# Hard caps on follow actions. If either is exceeded based on actions.log,
# the churn run quits immediately (idempotent — safe to re-run).
MAX_FOLLOWS_PER_HOUR = 8
MAX_FOLLOWS_PER_DAY = 30

# --- Unfollow policy ---
# Unfollow anyone we followed more than this many days ago (per actions.log).
MAX_FOLLOW_AGE_DAYS = 10
# Cap unfollows per run so a single churn doesn't dump hundreds at once.
MAX_UNFOLLOWS_PER_RUN = 10

# --- Discovery (random-sample 2-layer follower mining) ---
# Layer 1: pull the bot's most-recent N followers into a pool. This is one
# follower-page scrape, then we sub-sample it. Bigger pool = more diversity,
# but ~50 is already a single SoundCloud page and stays cheap.
RECENT_FOLLOWERS_POOL = 300
# How many of those pool entries to randomly pick as seeds for layer 2.
# Each pick costs one follower-page load, so this is the main rate-limit knob.
RANDOM_SEED_COUNT = 5
# Layer 2: for each randomly-picked seed, pull this many of their followers
# from api-v2, then randomly sample PER_SEED_FOLLOWERS_TOP_Y of them to add to
# the candidate pool. Wide scrape + narrow random pick reduces clustering
# around the most-recently-followed-of-followed users.
PER_SEED_SCRAPE_MAX = 500
PER_SEED_FOLLOWERS_TOP_Y = 10
# How many candidates to actually follow this run (subject to rate limits).
FOLLOWS_PER_RUN_Z = 10

# --- Pacing ---
# Extra seconds to sleep between follow clicks, on top of the human_delay
# inside follow_user(). Helps avoid burst-detection.
SECONDS_BETWEEN_FOLLOWS = (60, 120)
SECONDS_BETWEEN_UNFOLLOWS = (15, 30)
# Long pause between mining each random seed's followers. SoundCloud's
# DataDome detector trips on rapid follower-list page loads, so we make these
# look more like a human casually browsing tab-to-tab.
SECONDS_BETWEEN_SEEDS = (60, 120)
