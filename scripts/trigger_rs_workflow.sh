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
set -uo pipefail

WORKFLOW="${1:?workflow file required (e.g. update_us_rs_3m.yml)}"
MARKET="${2:?market label required (US or HK)}"
REPO="EricXue92/momentum-scanner"
GH=/opt/homebrew/bin/gh

LOG=/Users/xue/momentum-scanner/output/launchd_rs_trigger.log
mkdir -p "$(dirname "$LOG")"

# Rotate on date change (same pattern as run_eod.sh).
if [[ -f "$LOG" && "$(date -r "$LOG" +%Y-%m-%d)" != "$(date +%Y-%m-%d)" ]]; then
    : > "$LOG"
fi

exec >> "$LOG" 2>&1

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# launchd inherits no PATH; gh shells out to git which expects a usable PATH.
export PATH="/opt/homebrew/bin:/usr/bin:/bin"

# ntfy topic is reused from the morning-gap subscriber so all pipeline alerts
# land in the same place (config.toml [notify]).
NTFY_TOPIC="xue-finviz-morning-gap-9f3k2"
ntfy_alert() {
    local title="$1" body="$2"
    /usr/bin/curl -fsS -m 10 \
        -H "Title: $title" \
        -H "Priority: high" \
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
DISPATCH_GAP=30
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
else
    # `gh run watch` can race with the final job state (or hiccup on a GH
    # services blip during post-job cleanup) and exit non-zero on a run
    # that actually concluded success. Re-check the authoritative conclusion
    # before firing an alert.
    CONCLUSION=$("$GH" --repo "$REPO" run view "$RUN_ID" \
        --json conclusion --jq '.conclusion' 2>/dev/null)
    if [[ "$CONCLUSION" == "success" ]]; then
        echo "[$(ts)] [trigger:$MARKET] run $RUN_ID watch exited non-zero but conclusion=success — suppressing alert"
    else
        echo "[$(ts)] [trigger:$MARKET] run $RUN_ID FAILED (conclusion=${CONCLUSION:-unknown}) — EOD will use stale fallback"
        ntfy_alert "$MARKET RS workflow failed" \
            "Run $RUN_ID failed (conclusion=${CONCLUSION:-unknown}). EOD will fall back to yesterday's CSV (≤3 days OK)."
        exit 1
    fi
fi
