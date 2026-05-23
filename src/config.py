"""Churn flow configuration. Edit these values to tune the bot."""

# Your SoundCloud handle/slug (the path segment after soundcloud.com/).
# Used as the seed account whose followers we mine.
MY_USERNAME = "bloodxo"

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
# Layer 1: pull MY_USERNAME's most-recent N followers into a pool. This is one
# follower-page scrape, then we sub-sample it. Bigger pool = more diversity,
# but ~50 is already a single SoundCloud page and stays cheap.
RECENT_FOLLOWERS_POOL = 40
# How many of those pool entries to randomly pick as seeds for layer 2.
# Each pick costs one follower-page load, so this is the main rate-limit knob.
RANDOM_SEED_COUNT = 3
# Layer 2: for each randomly-picked seed, pull this many of their followers as
# candidates.
PER_SEED_FOLLOWERS_TOP_Y = 25
# How many candidates to actually follow this run (subject to rate limits).
FOLLOWS_PER_RUN_Z = 3

# --- Pacing ---
# Extra seconds to sleep between follow clicks, on top of the human_delay
# inside follow_user(). Helps avoid burst-detection.
SECONDS_BETWEEN_FOLLOWS = (120, 240)
SECONDS_BETWEEN_UNFOLLOWS = (30, 60)
# Long pause between mining each random seed's followers. SoundCloud's
# DataDome detector trips on rapid follower-list page loads, so we make these
# look more like a human casually browsing tab-to-tab.
SECONDS_BETWEEN_SEEDS = (60, 180)
