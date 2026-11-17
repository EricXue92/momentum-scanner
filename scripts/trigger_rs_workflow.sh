#!/usr/bin/env bash
# Pre-EOD trigger: dispatches a GH Actions workflow via `gh workflow run` so
# the pipeline doesn't depend on GH's scheduled cron, which is notoriously
# delayed (5+ hours observed) or skipped entirely under GH load. The
# workflow's commit step is idempotent (git diff --staged --quiet → exit 0),
# so this is safe even if GH's own cron also fires later for the same day.
#
# Watches the run and ntfy-alerts on failure so a stale-fallback EOD doesn't
# get noticed only after the run completes silently. A failed run is
# re-dispatched a few times (RERUN_TRIES) before giving up.
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

# Same topic as every other pipeline alert. It is NOT in git (public repo):
# NTFY_TOPIC from the environment, else from the project's .env. Only that
# one line is read — the API keys in .env have no business in gh's env.
ENV_FILE="${TRIGGER_ENV_FILE:-/Users/xue/momentum-scanner/.env}"
NTFY_TOPIC="${NTFY_TOPIC:-}"
if [[ -z "$NTFY_TOPIC" && -f "$ENV_FILE" ]]; then
    NTFY_TOPIC=$(sed -n 's/^NTFY_TOPIC=//p' "$ENV_FILE" | tail -1 | tr -d "\"'")
fi
ntfy_alert() {
    local title="$1" body="$2"
    local priority="${3:-high}"
    if [[ -z "$NTFY_TOPIC" ]]; then
        echo "[$(ts)] [trigger:$MARKET] NTFY_TOPIC not set (.env) — alert not sent: $title"
        return 0
    fi
    "$CURL" -fsS -m 10 \
        -H "Title: $title" \
        -H "Priority: $priority" \
        -d "$body" \
        "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1 || true
}

# launchd runs a missed calendar slot on the next wake, often before the
# network is back: 2026-10-15 fired at 09:00 with "error connecting to
# api.github.com", gave up, and us-eod used a stale 3M table. Retry for up
# to ~5 min (the ntfy alert needs the network too).
DISPATCH_TRIES=10
DISPATCH_GAP="${TRIGGER_DISPATCH_GAP:-30}"

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

# A FAILED run is re-dispatched. 2026-11-17 Yahoo served the latest daily
# row with NaN Close; the cloud job waited 15 min for SPY, failed and
# published nothing, and this script alerted and exited — nothing would have
# published even if Yahoo had recovered while us-eod was still polling for a
# fresh table (rs_rating._STALE_MAX_WAIT_SEC, 120 min from 10:00). Re-runs
# are 15 min apart and sized per market so the last one can still help:
#   US (trigger 08:45): 5 — six 15-min failing runs end ~11:30, a late
#       success ~11:45, inside the wait that ends 12:00.
#   HK (trigger 18:45, ~5-min runs): 3 — hk-eod at 20:00 does not wait.
# Change the US budget together with _STALE_MAX_WAIT_SEC.
# An unconfirmed outcome is NOT re-dispatched (the first run may be alive).
if [[ "$MARKET" == "US" ]]; then
    DEFAULT_RERUN_TRIES=5
else
    DEFAULT_RERUN_TRIES=3
fi
RERUN_TRIES="${TRIGGER_RERUN_TRIES:-$DEFAULT_RERUN_TRIES}"
RERUN_GAP="${TRIGGER_RERUN_GAP:-900}"
ATTEMPTS=$((RERUN_TRIES + 1))

RUN_ID=""
STATUS=""
CONCLUSION=""

# One dispatch + watch. Sets RUN_ID / STATUS / CONCLUSION. Returns:
#   0 run succeeded      1 run failed (conclusion known)
#   2 outcome unconfirmed within the poll window
#   3 could not dispatch 4 dispatched, run ID unknown (not watched)
dispatch_and_watch() {
    local i DISPATCH_OUT="" STATE=""
    RUN_ID=""
    STATUS=""
    CONCLUSION=""

    echo
    echo "[$(ts)] [trigger:$MARKET] dispatching $WORKFLOW"
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
            return 3
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
        return 4
    fi

    echo "[$(ts)] [trigger:$MARKET] dispatched run $RUN_ID, watching"

    # `gh run watch` polls every 3s and blocks until the run finishes.
    # Historical runtimes: US ~10-12 min, HK ~3-5 min.
    if "$GH" --repo "$REPO" run watch "$RUN_ID" --exit-status >/dev/null 2>&1; then
        echo "[$(ts)] [trigger:$MARKET] run $RUN_ID completed successfully"
        return 0
    fi

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
        return 0
    elif [[ "$STATUS" == "completed" ]]; then
        return 1
    fi
    return 2
}

for ((attempt = 1; attempt <= ATTEMPTS; attempt++)); do
    dispatch_and_watch
    case $? in
        0)
            if ((attempt > 1)); then
                ntfy_alert "$MARKET RS workflow recovered" \
                    "Run $RUN_ID succeeded on attempt $attempt/$ATTEMPTS — today's table is published." \
                    default
            fi
            exit 0
            ;;
        2)
            echo "[$(ts)] [trigger:$MARKET] run $RUN_ID: could not confirm the outcome within the poll window (last status=${STATUS:-unreachable}) — not claiming failure"
            ntfy_alert "$MARKET RS workflow unconfirmed" \
                "Run $RUN_ID: outcome not confirmed (last status=${STATUS:-unreachable}). Check GitHub Actions; EOD falls back to yesterday's CSV if it did not publish." \
                default
            exit 2
            ;;
        3) exit 1 ;;
        4) exit 0 ;;
    esac

    # The run itself failed.
    if ((attempt == ATTEMPTS)); then
        AFTER=""
        ((ATTEMPTS > 1)) && AFTER=" after $ATTEMPTS attempts"
        echo "[$(ts)] [trigger:$MARKET] run $RUN_ID FAILED (conclusion=${CONCLUSION:-unknown})$AFTER — EOD will use stale fallback"
        ntfy_alert "$MARKET RS workflow failed" \
            "Run $RUN_ID failed (conclusion=${CONCLUSION:-unknown})$AFTER. EOD will fall back to yesterday's CSV (≤3 days OK)."
        exit 1
    fi
    echo "[$(ts)] [trigger:$MARKET] run $RUN_ID FAILED (conclusion=${CONCLUSION:-unknown}) — re-dispatching (attempt $((attempt + 1))/$ATTEMPTS) in ${RERUN_GAP}s"
    if ((attempt == 1)); then
        ntfy_alert "$MARKET RS workflow failed" \
            "Run $RUN_ID failed (conclusion=${CONCLUSION:-unknown}). Re-dispatching up to $RERUN_TRIES more time(s), ${RERUN_GAP}s apart; a second push follows with the outcome."
    fi
    sleep "$RERUN_GAP"
done
