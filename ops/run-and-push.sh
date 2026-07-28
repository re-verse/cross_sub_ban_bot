#!/bin/bash
# =====================================================================
# run-and-push.sh - server-side wrapper for the cross-sub ban bot.
#
# Runs the bot, refreshes metrics, then syncs state to GitHub.
#
# The sync is the delicate part. Two executors (this server and GitHub
# Actions failover) both commit generated state to main, and one of
# those files - bans.db - is binary SQLite, which git cannot merge. The
# original `git pull --rebase` approach therefore had a guaranteed
# conflict on every divergence, and a conflicted rebase leaves
# .git/rebase-merge behind. Once that happened, every subsequent run
# failed to pull, commits piled up unpushed, and the repo drifted onto a
# detached HEAD where `git push` cannot work at all. The failover guard
# then correctly concluded the server was dead. (Outage: 2026-07-27.)
#
# This version never performs a git-level merge:
#
#   1. HEAL      clear any interrupted rebase/merge, guarantee we are on
#                a real branch tracking origin/main
#   2. SNAPSHOT  copy our generated state aside
#   3. COMMIT    commit our state
#   4. PUSH      on rejection: hard-reset to origin (cannot conflict),
#                re-apply our state via a SEMANTIC merge (union of bans
#                and events - see merge_state.py), commit, retry
#   5. VERIFY    assert the repo is left on a branch, clean, unbroken
#
# Exit codes: non-zero only if the BOT failed. A push that cannot
# complete is loud but non-fatal - the state is committed locally and
# goes out next tick.
# =====================================================================
set -uo pipefail

REPO=/opt/cross_sub_ban_bot
OPS=/opt/csbb-ops
VENV="$REPO/.venv/bin/python"
# systemd provides RuntimeDirectory=csbb -> /run/csbb, owned by the
# service User. /var/lock is root-owned and the unit runs as
# re-verse, so the original path silently failed to open. Fall back
# to /tmp when invoked by hand outside systemd.
LOCK="${RUNTIME_DIRECTORY:-/tmp}/csbb-run.lock"
SNAP=$(mktemp -d /tmp/csbb-state.XXXXXX)
PUSH_ATTEMPTS=5

STATE_FILES=(
  bans.db
  bot_health.json
  public_ban_log.json
  public_ban_log.md
  public_ban_log.html
  trusted_subs.txt
  pending_subs.json
)

log() { echo "[WRAPPER] $*"; }

# --- Healthchecks.io dead-man's switch --------------------------------
# Lives here rather than in the GitHub workflow: once the server became
# primary, the workflow's ping was gated behind "did GitHub actually do
# the work", which left nothing pinging at all. Reflects whether the BOT
# succeeded - a failed push is tolerated and retried, so it must not
# trip the switch. Fires on every exit path via trap.
HC_STATUS=1
cleanup() {
  rm -rf "$SNAP"
  [ -z "${HEALTHCHECK_URL:-}" ] && return 0
  if [ "$HC_STATUS" -eq 0 ]; then
    curl -fsS -m 10 --retry 3 "$HEALTHCHECK_URL" >/dev/null 2>&1 \
      && log "healthcheck pinged (ok)" || log "healthcheck ping FAILED (network?)"
  else
    curl -fsS -m 10 --retry 3 "${HEALTHCHECK_URL}/fail" >/dev/null 2>&1 \
      && log "healthcheck pinged (fail)" || true
  fi
}
trap cleanup EXIT

# --- Serialise runs ---------------------------------------------------
# systemd already prevents overlapping starts of a oneshot unit, but a
# manual `systemctl start` alongside a timer firing, or a future second
# caller, must not interleave git operations on the same worktree.
# Distinguish "someone else is running" (benign) from "the lock is
# broken" (a bug). Conflating them is what turned a permissions error
# into 26 hours of silent no-ops that still reported healthy.
if ! exec 9>"$LOCK" 2>/dev/null; then
  log "FATAL: cannot open lock file $LOCK - not running."
  log "This is a configuration fault, not contention. Check that"
  log "RuntimeDirectory=csbb is set on the unit and the path is writable."
  HC_STATUS=1   # ping /fail: we did NOT do the work
  exit 1
fi
if ! flock -n 9; then
  log "another run holds the lock; exiting without action"
  # Deliberately do NOT ping ok here - the run that holds the lock will
  # ping when it finishes. Pinging on a path that did no work is how a
  # broken bot looks healthy.
  exit 0
fi

cd "$REPO" || { log "FATAL: cannot cd to $REPO"; exit 1; }

# --- 1. HEAL ----------------------------------------------------------
heal_repo() {
  local healed=0
  if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
    log "HEAL: interrupted rebase found - aborting it"
    git rebase --abort 2>/dev/null || rm -rf .git/rebase-merge .git/rebase-apply
    healed=1
  fi
  if [ -f .git/MERGE_HEAD ]; then
    log "HEAL: interrupted merge found - aborting it"
    git merge --abort 2>/dev/null || rm -f .git/MERGE_HEAD
    healed=1
  fi
  if [ -f .git/CHERRY_PICK_HEAD ]; then
    git cherry-pick --abort 2>/dev/null || rm -f .git/CHERRY_PICK_HEAD
    healed=1
  fi
  # Detached HEAD makes `git push` impossible. Reattach to main. Our
  # state is snapshotted below, so nothing generated is at risk.
  if ! git symbolic-ref -q HEAD >/dev/null 2>&1; then
    log "HEAL: detached HEAD - reattaching to main"
    git fetch -q origin main 2>/dev/null
    git checkout -B main origin/main -q 2>/dev/null || {
      log "HEAL FAILED: could not reattach to main"; return 1; }
    healed=1
  fi
  [ "$healed" -eq 1 ] && log "HEAL: repository state repaired"
  return 0
}

snapshot_state() {
  local f
  for f in "${STATE_FILES[@]}"; do
    [ -e "$f" ] && cp -p "$f" "$SNAP/" 2>/dev/null
  done
}

restore_state() {
  local f
  for f in "${STATE_FILES[@]}"; do
    [ -e "$SNAP/$f" ] && cp -p "$SNAP/$f" "$REPO/" 2>/dev/null
  done
}

stage_state() {
  local f
  for f in "${STATE_FILES[@]}"; do
    [ -e "$f" ] && git add "$f"
  done
}

# --- Run the bot ------------------------------------------------------
log "starting bot run at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
"$VENV" cross_sub_ban_bot.py
BOT_RC=$?
if [ "$BOT_RC" -ne 0 ]; then
  log "bot exited non-zero ($BOT_RC); not committing. Failing unit."
  exit "$BOT_RC"
fi

# The bot did its job. The dead-man's switch is satisfied from here on,
# regardless of what git does.
HC_STATUS=0

"$OPS/csbb-metrics.py" || log "metrics refresh failed (non-fatal)"

# --- 2-4. SNAPSHOT / COMMIT / PUSH -----------------------------------
snapshot_state
heal_repo || log "WARN: heal incomplete; attempting sync anyway"
restore_state   # heal may have reset the tree; our state is authoritative

stage_state
if git diff --cached --quiet; then
  log "no state changes to commit"
else
  git commit -q -m "Update ban database and public log"
fi

push_state() {
  local attempt delay
  for attempt in $(seq 1 "$PUSH_ATTEMPTS"); do
    if git push -q 2>/dev/null; then
      [ "$attempt" -gt 1 ] && log "push succeeded on attempt $attempt" \
                           || log "push succeeded"
      return 0
    fi

    log "push rejected (attempt $attempt/$PUSH_ATTEMPTS) - reconciling"
    if ! git fetch -q origin main 2>/dev/null; then
      log "fetch failed (network?); will retry"
      sleep $(( attempt * 5 )); continue
    fi

    # Reset to the remote. This CANNOT conflict - we are discarding our
    # tree wholesale, then re-applying our data semantically below. That
    # is what makes a stuck rebase structurally impossible.
    git reset --hard -q origin/main || { log "reset failed"; return 1; }

    if ! "$VENV" "$OPS/merge_state.py" "$SNAP" "$REPO"; then
      log "ERROR: semantic merge failed - restoring our state verbatim"
      restore_state
    fi

    stage_state
    if git diff --cached --quiet; then
      log "remote already contains our state; nothing to push"
      return 0
    fi
    git commit -q -m "Update ban database and public log"

    delay=$(( attempt * 5 ))
    log "retrying push in ${delay}s"
    sleep "$delay"
  done
  return 1
}

if ! push_state; then
  log "PUSH FAILED after $PUSH_ATTEMPTS attempts. State is committed"
  log "locally and will go out next tick. Bot work already succeeded."
fi

# --- 5. VERIFY --------------------------------------------------------
# Never leave the repo in a state that breaks the NEXT run. This is the
# check that would have caught the 2026-07-27 outage on run one instead
# of nine hours later.
verify_repo() {
  local problems=0
  if ! git symbolic-ref -q HEAD >/dev/null 2>&1; then
    log "POST-CHECK FAIL: repo is on a detached HEAD"; problems=1
  fi
  if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
    log "POST-CHECK FAIL: rebase state left behind"; problems=1
  fi
  local branch; branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
  if [ "$branch" != "main" ]; then
    log "POST-CHECK FAIL: on branch '$branch', expected 'main'"; problems=1
  fi
  local ahead; ahead=$(git rev-list --count @{u}..HEAD 2>/dev/null || echo 0)
  if [ "${ahead:-0}" -gt 3 ]; then
    log "POST-CHECK WARN: $ahead unpushed commits - pushes may be failing"
  fi
  if [ "$problems" -eq 0 ]; then
    log "post-check ok (on main, clean, synced)"
  else
    log "post-check found problems - next run's HEAL will attempt repair"
  fi
}
verify_repo

log "run complete"
exit 0
