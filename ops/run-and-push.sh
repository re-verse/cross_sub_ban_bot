#!/bin/bash
# =====================================================================
# run-and-push.sh — server-side wrapper for the cross-sub ban bot.
#
# The GitHub Actions workflow committed/pushed state as a separate YAML
# step after running the bot. When running under systemd there's no such
# wrapper, so this script reproduces it: run the bot, then commit and
# push the state files it changed, with the same push-retry backoff the
# workflow used.
#
# Invoked by csbb.service. Exits non-zero if the bot itself fails (so
# systemd marks the unit failed and the timer's failure is visible);
# a push failure is logged loudly but does not fail the unit, because
# the bot work (Reddit propagation) already succeeded and will be
# re-pushed next tick.
# =====================================================================
set -uo pipefail
cd /opt/cross_sub_ban_bot || exit 1

VENV=/opt/cross_sub_ban_bot/.venv/bin/python

# --- Healthchecks.io dead-man's switch -------------------------------
# This used to live in the GitHub Actions workflow. When the server
# became primary, the workflow's ping got gated behind "did GitHub
# actually do the work" (correctly — GitHub shouldn't rubber-stamp a
# cycle it skipped), which left NOTHING pinging the check. The switch
# then fired every night for the only reason it could: silence.
#
# The ping reflects whether the BOT did its job, not whether the push
# succeeded — a failed push is already tolerated and retried next tick.
# Fires on every exit path via trap so it can't be missed.
HC_STATUS=1
hc_finish() {
  [ -z "${HEALTHCHECK_URL:-}" ] && return 0
  if [ "$HC_STATUS" -eq 0 ]; then
    curl -fsS -m 10 --retry 3 "$HEALTHCHECK_URL" >/dev/null 2>&1 \
      && echo "[WRAPPER] healthcheck pinged (ok)" \
      || echo "[WRAPPER] healthcheck ping FAILED (network?)"
  else
    curl -fsS -m 10 --retry 3 "${HEALTHCHECK_URL}/fail" >/dev/null 2>&1 \
      && echo "[WRAPPER] healthcheck pinged (fail)" || true
  fi
}
trap hc_finish EXIT

echo "[WRAPPER] Starting bot run at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
"$VENV" cross_sub_ban_bot.py
BOT_RC=$?
if [ "$BOT_RC" -ne 0 ]; then
  echo "[WRAPPER] Bot exited non-zero ($BOT_RC); not committing. Failing unit."
  exit "$BOT_RC"
fi


# The bot did its job — the dead-man's switch is satisfied regardless of
# what the git push does below.
HC_STATUS=0

# Refresh Prometheus metrics from the freshly-updated log (non-fatal).
/opt/csbb-ops/csbb-metrics.py || echo "[WRAPPER] metrics refresh failed (non-fatal)"
# Stage only the state files the bot maintains (never .venv, pycache,
# workflows, etc). -- ignore missing files without masking real errors.
for f in bans.db bot_health.json public_ban_log.json public_ban_log.md \
         public_ban_log.html trusted_subs.txt pending_subs.json; do
  [ -e "$f" ] && git add "$f"
done

if git diff --cached --quiet; then
  echo "[WRAPPER] No state changes to commit."
  exit 0
fi

git commit -q -m "Update ban database and public log"

# Push with backoff — the datacenter box is reliable, but a transient
# GitHub 5xx shouldn't wedge the run. Mirror the workflow's 4 attempts.
for delay in 0 5 30 120; do
  if [ "$delay" != "0" ]; then
    echo "[WRAPPER] Push attempt failed; retrying in ${delay}s..."
    sleep "$delay"
  fi
  if git pull --rebase -q origin main && git push -q; then
    echo "[WRAPPER] Push succeeded."
    exit 0
  fi
done

echo "[WRAPPER] Push failed after 4 attempts. State is committed locally"
echo "[WRAPPER] and will be pushed on the next tick. Not failing the unit."
exit 0
