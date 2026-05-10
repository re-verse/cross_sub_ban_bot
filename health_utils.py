"""
Per-sub access health tracking.

Writes/reads bot_health.json to track which trusted subs the bot can
successfully read modlogs for. Emits clear log lines on state changes:

  [HEALTH-ALERT]            sub flipped from healthy to failing
  [HEALTH-RECOVERY]         sub recovered after previous failures
  [HEALTH-ALERT-PERSISTENT] sub has been failing for N runs (escalation)
  [HEALTH-SUMMARY]          end-of-run rollup

The structured file makes the regression that bit us (silent loss of
mod permission on r/floridapanthers + r/caps for ~10 months) detectable
on the very next run instead of whenever someone notices the public log
has gone quiet.
"""
import json
import os
from datetime import datetime, timezone

HEALTH_FILE = "bot_health.json"

# Cron is every 20 min. Escalate at ~1h, ~4h, ~24h of continuous failures.
_ESCALATION_RUNS = {3, 12, 72}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_health():
    """Load existing health state, or return a fresh skeleton."""
    if not os.path.exists(HEALTH_FILE):
        return {"subs": {}, "last_run": None}
    try:
        with open(HEALTH_FILE) as f:
            data = json.load(f)
        # Defensive: ensure expected shape
        data.setdefault("subs", {})
        data.setdefault("last_run", None)
        return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"[HEALTH-WARN] Could not parse {HEALTH_FILE} ({e}); starting fresh.")
        return {"subs": {}, "last_run": None}


def save_health(state):
    """Persist current state. Bumps last_run timestamp."""
    state["last_run"] = _now()
    try:
        with open(HEALTH_FILE, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except OSError as e:
        print(f"[HEALTH-ERROR] Could not write {HEALTH_FILE}: {e}")


def record_success(state, sub):
    """Mark a sub as successfully accessed this run."""
    s = state["subs"].setdefault(sub, {"consecutive_failures": 0})
    was_failing = s.get("consecutive_failures", 0) > 0
    s["last_ok"] = _now()
    s["consecutive_failures"] = 0
    s.pop("last_error", None)
    if was_failing:
        print(f"[HEALTH-RECOVERY] r/{sub} is healthy again after previous failures.")


def record_failure(state, sub, error):
    """Mark a sub as failing and emit an alert on transition or escalation."""
    s = state["subs"].setdefault(sub, {"consecutive_failures": 0})
    s["last_failure"] = _now()
    s["last_error"] = str(error)[:200]
    s["consecutive_failures"] = s.get("consecutive_failures", 0) + 1

    n = s["consecutive_failures"]
    if n == 1:
        print(f"[HEALTH-ALERT] r/{sub} just started failing: {s['last_error']}")
    elif n in _ESCALATION_RUNS:
        approx_hours = n * 20 // 60
        print(
            f"[HEALTH-ALERT-PERSISTENT] r/{sub} has failed {n} consecutive runs "
            f"(~{approx_hours}h): {s['last_error']}"
        )


def summary(state):
    """Print a one-line rollup of the health state."""
    subs = state.get("subs", {})
    if not subs:
        print("[HEALTH-SUMMARY] No subs tracked yet.")
        return
    failing = sorted(
        sub for sub, s in subs.items() if s.get("consecutive_failures", 0) > 0
    )
    total = len(subs)
    if failing:
        print(
            f"[HEALTH-SUMMARY] {total - len(failing)}/{total} subs healthy. "
            f"Failing: {', '.join('r/' + s for s in failing)}"
        )
    else:
        print(f"[HEALTH-SUMMARY] {total}/{total} subs healthy.")
