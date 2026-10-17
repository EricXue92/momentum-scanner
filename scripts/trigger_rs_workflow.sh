#!/usr/bin/env bash
# Pre-EOD trigger: dispatches a GH Actions workflow via `gh workflow run` so
# the pipeline doesn't depend on GH's scheduled cron, which is notoriously
# delayed (5+ hours observed) or skipped entirely under GH load. The
# workflow's commit step is idempotent (git diff --staged --quiet → exit 0),
# so this is safe even if GH's own cron also fires later for the same day.
#
# Watches the run and ntfy-alerts on failure so a stale-fallback EOD doesn't
# get noticed only after the run completes silently.
#
# Usage: trigger_rs_workflow.sh <workflow-file> <market-label>
#   e.g.: trigger_rs_workflow.sh update_us_rs_3m.yml US
#
# TRIGGER_* env overrides exist for the test suite (fake gh/curl, tmp log,
# zero sleeps) — tests/test_trigger_rs_workflow.py runs this script for real.
set -uo pipefail

WORKFLOW="${1:?workflow file required (e.g. update_us_rs_3m.yml)}"
MARKET="${2:?market label required (US or HK)}"
REPO="EricXue92/momentum-scanner"
GH="${TRIGGER_GH:-/opt/homebrew/bin/gh}"
CURL="${TRIGGER_CURL:-/usr/bin/curl}"
CAFFEINATE="${TRIGGER_CAFFEINATE:-/usr/bin/caffeinate}"

LOG="${TRIGGER_LOG:-/Users/xue/momentum-scanner/output/launchd_rs_trigger.log}"
mkdir -p "$(dirname "$LOG")"

# Rotate on date change (same pattern as run_eod.sh).
if [[ -f "$LOG" && "$(date -r "$LOG" +%Y-%m-%d)" != "$(date +%Y-%m-%d)" ]]; then
    : > "$LOG"
fi

exec >> "$LOG" 2>&1

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Sleep assertion for the life of this script (see scripts/lib/hold_awake.sh;
# 2026-10-17 `gh run watch` froze when the Mac slept 2s after dispatching).
# shellcheck source=lib/hold_awake.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/hold_awake.sh"
hold_awake "$CAFFEINATE"

# launchd inherits no PATH; gh shells out to git which expects a usable PATH.
export PATH="/opt/homebrew/bin:/usr/bin:/bin"

# ntfy topic is reused from the morning-gap subscriber so all pipeline alerts
# land in the same place (config.toml [notify]).
NTFY_TOPIC="xue-finviz-morning-gap-9f3k2"
ntfy_alert() {
    local title="$1" body="$2"
    local priority="${3:-high}"
    "$CURL" -fsS -m 10 \
        -H "Title: $title" \
        -H "Priority: $priority" \
        -d "$body" \
        "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1 || true
}

echo
echo "[$(ts)] [trigger:$MARKET] dispatching $WORKFLOW"

# launchd runs a missed calendar slot on the next wake, often before the
# network is back: 2026-10-15 fired at 09:00 with "error connecting to
# api.github.com", gave up, and us-eod used a stale 3M table. Retry for up
# to ~5 min (the ntfy alert needs the network too).
DISPATCH_TRIES=10
DISPATCH_GAP="${TRIGGER_DISPATCH_GAP:-30}"
DISPATCH_OUT=""
for ((i = 1; i <= DISPATCH_TRIES; i++)); do
    if DISPATCH_OUT=$("$GH" --repo "$REPO" workflow run "$WORKFLOW" 2>&1); then
        echo "$DISPATCH_OUT"
        break
    fi
    echo "$DISPATCH_OUT"
    if ((i == DISPATCH_TRIES)); then
        echo "[$(ts)] [trigger:$MARKET] gh workflow run FAILED after $DISPATCH_TRIES tries (network? auth?)"
        ntfy_alert "$MARKET RS trigger failed" \
            "gh workflow run $WORKFLOW failed $DISPATCH_TRIES times — network or auth. Check 'gh auth status'."
        exit 1
    fi
    echo "[$(ts)] [trigger:$MARKET] dispatch attempt $i/$DISPATCH_TRIES failed, retrying in ${DISPATCH_GAP}s"
    sleep "$DISPATCH_GAP"
done

# gh prints the new run's URL (.../actions/runs/<id>); fall back to the
# newest dispatch run if it ever stops doing so.
RUN_ID=$(grep -oE 'actions/runs/[0-9]+' <<<"$DISPATCH_OUT" | head -1 | cut -d/ -f3)
if [[ -z "$RUN_ID" ]]; then
    sleep 5  # let the new run show up in `gh run list`
    RUN_ID=$("$GH" --repo "$REPO" run list \
        --workflow "$WORKFLOW" --event workflow_dispatch --limit 1 \
        --json databaseId --jq '.[0].databaseId' 2>/dev/null)
fi

if [[ -z "$RUN_ID" ]]; then
    echo "[$(ts)] [trigger:$MARKET] could not resolve run ID — dispatched but not watched"
    exit 0
fi

echo "[$(ts)] [trigger:$MARKET] dispatched run $RUN_ID, watching"

# `gh run watch` polls every 3s and blocks until the run finishes. Historical
# runtimes: US ~10-12 min, HK ~3-5 min.
if "$GH" --repo "$REPO" run watch "$RUN_ID" --exit-status >/dev/null 2>&1; then
    echo "[$(ts)] [trigger:$MARKET] run $RUN_ID completed successfully"
    exit 0
fi

# `gh run watch` exits non-zero for more than a failed run: a race with the
# final job state, a GH services blip, or (2026-10-17) the Mac sleeping
# under it and the connection dying. Only the run's own status/conclusion
# is authoritative — poll it until the run reports `completed` (a still
# running run keeps us waiting; an unreachable GH is retried), and decide
# only then. 2026-10-17 the single re-check ran before Wi-Fi was back,
# got an empty conclusion and pushed "failed (conclusion=unknown)" for a
# run that went green 3 minutes later.
POLL_TRIES="${TRIGGER_POLL_TRIES:-40}"   # 40 × 30s ≈ 20 min — longer than any US run
POLL_GAP="${TRIGGER_POLL_GAP:-30}"
STATUS=""
CONCLUSION=""
for ((i = 1; i <= POLL_TRIES; i++)); do
    STATE=$("$GH" --repo "$REPO" run view "$RUN_ID" \
        --json status,conclusion --jq '.status + " " + (.conclusion // "")' 2>/dev/null) || STATE=""
    STATUS="${STATE%% *}"
    CONCLUSION="${STATE#* }"
    [[ "$CONCLUSION" == "$STATE" ]] && CONCLUSION=""   # no space → no conclusion
    if [[ "$STATUS" == "completed" ]]; then
        break
    fi
    if [[ -z "$STATE" ]]; then
        echo "[$(ts)] [trigger:$MARKET] run $RUN_ID: gh run view unreachable (check $i/$POLL_TRIES)"
    else
        echo "[$(ts)] [trigger:$MARKET] run $RUN_ID: status=$STATUS, still running (check $i/$POLL_TRIES)"
    fi
    if ((i < POLL_TRIES)); then
        sleep "$POLL_GAP"
    fi
done

if [[ "$STATUS" == "completed" && "$CONCLUSION" == "success" ]]; then
    echo "[$(ts)] [trigger:$MARKET] run $RUN_ID watch exited non-zero but conclusion=success — no alert"
    exit 0
elif [[ "$STATUS" == "completed" ]]; then
    echo "[$(ts)] [trigger:$MARKET] run $RUN_ID FAILED (conclusion=${CONCLUSION:-unknown}) — EOD will use stale fallback"
    ntfy_alert "$MARKET RS workflow failed" \
        "Run $RUN_ID failed (conclusion=${CONCLUSION:-unknown}). EOD will fall back to yesterday's CSV (≤3 days OK)."
    exit 1
else
    echo "[$(ts)] [trigger:$MARKET] run $RUN_ID: could not confirm the outcome within the poll window (last status=${STATUS:-unreachable}) — not claiming failure"
    ntfy_alert "$MARKET RS workflow unconfirmed" \
        "Run $RUN_ID: outcome not confirmed (last status=${STATUS:-unreachable}). Check GitHub Actions; EOD falls back to yesterday's CSV if it did not publish." \
        default
    exit 2
fi
