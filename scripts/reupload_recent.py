"""Re-upload recent actions.log entries to verify the on_conflict fix.

Usage:  poetry run python scripts/reupload_recent.py
"""
import sys, datetime as dt
sys.path.insert(0, "src")
import supabase_client as sc
from churn import load_actions

acts = load_actions()
since = dt.datetime(2026, 5, 21, 14, 8, 0, tzinfo=dt.timezone.utc)
new = [
    {
        "account": "bloodxo",
        "ts": a["timestamp"],
        "action": a.get("action"),
        "status": a.get("status"),
        "ok": bool(a.get("ok")),
        "profile_url": a.get("profile_url"),
        "username": a.get("username"),
        "reason": a.get("reason") or "",
    }
    for a in acts
    if a.get("_ts") and a["_ts"] >= since
]
print(f"rows to upload: {len(new)}")
for r in new:
    print(f"  {r['ts']}  {r['action']}/{r['status']}  {r['username']}")
result = sc.upload_actions(new)
print(f"result: {result}")
