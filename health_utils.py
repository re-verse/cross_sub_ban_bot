"""
Per-sub access health tracking.

Writes/reads bot_health.json to track which trusted subs the bot can
successfully read modlogs for. Emits clear log lines on state changes:

  [HEALTH-ALERT]            sub flipped from healthy to failing
  [HEALTH-RECOVERY]         sub recovered after previous failures
  [HEALTH-ALERT-PERSISTENT] sub has been failing for N runs (escalation)
  [HEALTH-SUMMARY]          end-of-run rollup

Alertable transitions are also collected into state["pending_events"]
and drained by dispatch_alerts() at end-of-run, which DMs the bot owner
via bot_config.notify_owner.

Alerting policy (configured by user):
- Alert on first failure of a sub (consecutive_failures transitions
  from 0 -> 1) and on each escalation threshold (3 / 12 / 72 runs).
- Recoveries are NOT alerted. They're visible in the log line and in
  the next [HEALTH-SUMMARY], which is enough.
- Throttle: a given (sub, reason) combination won't re-alert within
  THROTTLE_HOURS regardless of how many escalation marks pass.
"""
import json
import os
from datetime import datetime, timezone, timedelta

HEALTH_FILE = "bot_health.json"

# Cron is every 20 min. Escalate at ~1h, ~4h, ~24h of continuous failures.
_ESCALATION_RUNS = {3, 12, 72}

# Don't re-alert on the same (sub, event_type) inside this window.
THROTTLE_HOURS = 24

# DM the owner when the gap since the previous run exceeds this.
# Cron is */20. Observed GHA free-tier behaviour: ordinary slippage is
# 30-80 min, and genuine runner-starvation incidents produce gaps of
# ~80+ min. 75 min catches the latter while tolerating the former.
CRON_GAP_ALERT_MINUTES = 20
MUTED_SUBS_FILE = "muted_subs.txt"


def _muted_subs():
    """Subs whose failures should not DM the owner (still tracked/shown)."""
    try:
        with open(MUTED_SUBS_FILE) as f:
            return {ln.strip().lower() for ln in f
                    if ln.strip() and not ln.strip().startswith("#")}
    except OSError:
        return set()



def _now():
    return datetime.now(timezone.utc)


def _now_iso():
    return _now().isoformat(timespec="seconds")


def load_health():
    """Load existing health state, or return a fresh skeleton."""
    if not os.path.exists(HEALTH_FILE):
        return _fresh_state()
    try:
        with open(HEALTH_FILE) as f:
            data = json.load(f)
        # Defensive: ensure expected shape
        data.setdefault("subs", {})
        data.setdefault("last_run", None)
        data.setdefault("alert_history", {})
        # pending_events is rebuilt each run, never persisted
        data["pending_events"] = []
        return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"[HEALTH-WARN] Could not parse {HEALTH_FILE} ({e}); starting fresh.")
        return _fresh_state()


def _fresh_state():
    return {
        "subs": {},
        "last_run": None,
        "alert_history": {},
        "pending_events": [],
    }


def save_health(state):
    """Persist current state. Bumps last_run timestamp. Drops transient fields."""
    state["last_run"] = _now_iso()
    to_save = {
        "subs": state.get("subs", {}),
        "last_run": state["last_run"],
        "alert_history": state.get("alert_history", {}),
    }
    try:
        with open(HEALTH_FILE, "w") as f:
            json.dump(to_save, f, indent=2, sort_keys=True)
    except OSError as e:
        print(f"[HEALTH-ERROR] Could not write {HEALTH_FILE}: {e}")

    # Private per-sub status export for the internal Grafana dashboard
    # (served loopback-only from /opt/csbb-private; never pushed to git).
    try:
        muted = _muted_subs()
        # Collapse per-check entries (e.g. "caps" + "caps:modmail") into ONE
        # row per sub: worst state wins. One broken check is enough to care.
        grouped = {}
        for sub, s in to_save["subs"].items():
            base = sub.split(":")[0]
            g = grouped.setdefault(base, {"fails": 0, "last_error": "", "last_ok": ""})
            fails = s.get("consecutive_failures", 0)
            if fails > g["fails"]:
                g["fails"] = fails
                g["last_error"] = s.get("last_error", "")
            ok = s.get("last_ok", "")
            # oldest fully-ok moment: only meaningful if every check reported ok
            if not g["last_ok"] or (ok and ok < g["last_ok"]):
                g["last_ok"] = ok
        rows = []
        for base, g in sorted(grouped.items()):
            is_muted = base.lower() in muted
            status = ("ok" if g["fails"] == 0
                      else "disconnected (muted)" if is_muted
                      else "FAILING")
            rows.append({
                "sub": base,
                "status": status,
                "consecutive_failures": g["fails"],
                "last_ok": g["last_ok"],
                "last_error": g["last_error"],
            })
        with open("/opt/csbb-private/sub_health.json", "w") as f:
            json.dump(rows, f, indent=2)
    except OSError as e:
        print(f"[HEALTH-WARN] Could not write private health export: {e}")


def record_success(state, sub):
    """Mark a sub as successfully accessed this run."""
    s = state["subs"].setdefault(sub, {"consecutive_failures": 0})
    was_failing = s.get("consecutive_failures", 0) > 0
    s["last_ok"] = _now_iso()
    s["consecutive_failures"] = 0
    s.pop("last_error", None)
    if was_failing:
        print(f"[HEALTH-RECOVERY] r/{sub} is healthy again after previous failures.")


def record_failure(state, sub, error):
    """Mark a sub as failing and queue an alert event on transition/escalation."""
    s = state["subs"].setdefault(sub, {"consecutive_failures": 0})
    s["last_failure"] = _now_iso()
    s["last_error"] = str(error)[:200]
    s["consecutive_failures"] = s.get("consecutive_failures", 0) + 1

    n = s["consecutive_failures"]
    if n == 1:
        print(f"[HEALTH-ALERT] r/{sub} just started failing: {s['last_error']}")
        _queue_event(state, sub, "first_failure", n, s["last_error"])
    elif n in _ESCALATION_RUNS:
        approx_hours = n * 20 // 60
        print(
            f"[HEALTH-ALERT-PERSISTENT] r/{sub} has failed {n} consecutive runs "
            f"(~{approx_hours}h): {s['last_error']}"
        )
        _queue_event(state, sub, f"escalation_{n}", n, s["last_error"])


def _queue_event(state, sub, event_type, n_failures, last_error):
    """Append an alertable event to the per-run queue."""
    if sub.split(":")[0].lower() in _muted_subs():
        print(f"[HEALTH-MUTED] r/{sub} failing but muted; no alert sent.")
        return
    state.setdefault("pending_events", []).append({
        "sub": sub,
        "event_type": event_type,
        "n_failures": n_failures,
        "last_error": last_error,
    })


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


def dispatch_alerts(state, notify_func, dry_run=False):
    """
    Drain pending_events, apply the THROTTLE_HOURS suppression, and send
    a single consolidated DM if anything survives.

    notify_func: callable matching bot_config.notify_owner signature
        (subject, body, dry_run=False) -> bool. Injected so this module
        stays free of bot_config imports (avoids a cycle).
    """
    events = state.get("pending_events", [])
    if not events:
        return

    history = state.setdefault("alert_history", {})
    cutoff = _now() - timedelta(hours=THROTTLE_HOURS)

    surviving = []
    for ev in events:
        key = f"{ev['sub']}::{ev['event_type']}"
        last_sent = _parse_iso(history.get(key))
        if last_sent and last_sent > cutoff:
            print(
                f"[ALERT-THROTTLED] {key} suppressed "
                f"(last sent {history[key]}, within {THROTTLE_HOURS}h window)"
            )
            continue
        surviving.append(ev)

    if not surviving:
        return

    subject, body = _format_alert(surviving)
    if notify_func(subject, body, dry_run=dry_run):
        # Record send timestamps only on real sends. Dry-runs must not
        # pollute alert_history — otherwise a dry-run would suppress the
        # next real alert for 24h.
        if not dry_run:
            stamp = _now_iso()
            for ev in surviving:
                key = f"{ev['sub']}::{ev['event_type']}"
                history[key] = stamp


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _format_alert(events):
    """Build (subject, body) for a consolidated alert DM."""
    if len(events) == 1:
        ev = events[0]
        subject = f"[xsub-pact-bot] r/{ev['sub']} {_describe(ev['event_type'])}"
    else:
        subject = f"[xsub-pact-bot] {len(events)} sub health alerts"

    lines = ["The pact bot health tracker flagged the following:", ""]
    for ev in events:
        lines.append(
            f"- r/{ev['sub']}: {_describe(ev['event_type'])} "
            f"({ev['n_failures']} consecutive failure"
            f"{'s' if ev['n_failures'] != 1 else ''})"
        )
        lines.append(f"    last error: {ev['last_error']}")
    lines.append("")
    lines.append(
        f"Throttled to one alert per (sub, event) per {THROTTLE_HOURS}h. "
        "See bot_health.json on main for full state."
    )
    return subject, "\n".join(lines)


def _describe(event_type):
    if event_type == "first_failure":
        return "started failing"
    if event_type.startswith("escalation_"):
        n = int(event_type.split("_")[1])
        approx_hours = n * 20 // 60
        return f"has failed {n} consecutive runs (~{approx_hours}h)"
    return event_type


def maybe_alert_cron_gap(state, notify_func, dry_run=False):
    """
    Called at the START of a run, before last_run is bumped: if the gap
    since the previous run exceeds CRON_GAP_ALERT_MINUTES, DM the owner.
    Detects GHA scheduler degradation after the fact — the bot can only
    report a gap once it finally runs again, so this complements (not
    replaces) external monitoring.

    Throttled via alert_history under 'scheduler::cron_gap' so chronic
    flakiness produces at most one DM per THROTTLE_HOURS.
    """
    prev = _parse_iso(state.get("last_run"))
    if prev is None:
        return  # first run ever, nothing to compare against
    gap_min = int((_now() - prev).total_seconds() // 60)
    if gap_min <= CRON_GAP_ALERT_MINUTES:
        return

    print(f"[CRON-GAP] {gap_min} min since previous run (expected ~20).")

    history = state.setdefault("alert_history", {})
    key = "scheduler::cron_gap"
    last_sent = _parse_iso(history.get(key))
    if last_sent and last_sent > _now() - timedelta(hours=THROTTLE_HOURS):
        print(f"[ALERT-THROTTLED] {key} suppressed (within {THROTTLE_HOURS}h window)")
        return

    subject = f"[xsub-pact-bot] cron gap: {gap_min} min since previous run"
    body = (
        f"The bot just ran after a {gap_min}-minute gap (schedule is every "
        f"5 min; alert threshold {CRON_GAP_ALERT_MINUTES} min).\n\n"
        "This usually means GitHub Actions deprioritized the scheduled "
        "job. The watchdog workflow re-dispatches stale runs, but if "
        "these DMs become frequent, the long-term fixes are enrolling "
        "healthchecks.io (repo secret HEALTHCHECK_URL) or migrating the "
        f"cron to a server-side systemd timer.\n\n"
        f"Throttled to one DM per {THROTTLE_HOURS}h."
    )
    if notify_func(subject, body, dry_run=dry_run):
        if not dry_run:
            history[key] = _now_iso()
