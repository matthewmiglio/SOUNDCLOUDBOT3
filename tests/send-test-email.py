"""One-shot: render with sample data and send a real Resend email to EMAIL_TO."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from emailer import render_html, send_email  # noqa: E402


SAMPLE = {
    "greeting": "Good afternoon",
    "now": "16:34:18 05/17/2026",
    "session_followed": 7,
    "session_unfollowed": 2,
    "total_followed": 23,
    "total_unfollowed": 4,
    "cron_runs": 12,
}


def main():
    SAMPLE.update({
        "profile_followers": "1,872",
        "profile_following": "928",
        "delta_followers": "+14",
        "delta_following": "-3",
    })
    html = render_html(SAMPLE, name="Matthew")
    result = send_email(html, subject="SoundCloudBot Report -- TEST EMAIL (with profile stats)")
    print(result)


if __name__ == "__main__":
    main()
