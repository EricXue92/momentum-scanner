#!/usr/bin/env bash
# Wrapper for the HK EOD launchd job (20:00 HKT, Mon-Fri). Mirrors run_eod.sh
# but writes its own log file (launchd_HK.log) and runs --mode hk-eod so the
# US pipeline is skipped. HK market closes at 16:00 HKT; the 20:00 slot leaves
# 4 hours for k-line data to finalize.
set -euo pipefail

LOG=/Users/xue/momentum-scanner/output/launchd_HK.log
mkdir -p "$(dirname "$LOG")"

if [[ -f "$LOG" && "$(date -r "$LOG" +%Y-%m-%d)" != "$(date +%Y-%m-%d)" ]]; then
    : > "$LOG"
fi

exec >> "$LOG" 2>&1

# Load secrets from the project's .env (gitignored) — kept for parity with
# run_eod.sh even though HK no longer runs the report step. Launchd does NOT
# inherit the user's interactive shell environment.
ENV_FILE=/Users/xue/momentum-scanner/.env
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "$ENV_FILE"
    set +a
fi

UV=/Users/xue/.local/bin/uv
PROJECT=/Users/xue/momentum-scanner

# Hard wall-clock cap — HK pulls ~2,400 tickers from yfinance + Futu snapshots,
# any of which can hang on a half-closed socket (see run_eod.sh comment). HK
# legitimately takes longer than US so the default ceiling is higher. macOS
# has no `timeout`; `set -m` puts the job in its own pgrp so the watchdog can
# kill uv's python grandchild along with the parent.
EOD_TIMEOUT=${EOD_TIMEOUT:-2700}
set +e
set -m
"$UV" run --directory "$PROJECT" main.py --mode hk-eod &
EOD_PID=$!
set +m
( sleep "$EOD_TIMEOUT" && kill -TERM "-$EOD_PID" 2>/dev/null \
    && sleep 15 && kill -KILL "-$EOD_PID" 2>/dev/null ) &
WATCHDOG_PID=$!
wait "$EOD_PID"
EOD_STATUS=$?
kill "$WATCHDOG_PID" 2>/dev/null
wait "$WATCHDOG_PID" 2>/dev/null
set -e

# No CANSLIM report for HK (US-only since 2026-09-09; run
# `main.py --mode report --market hk` by hand if one is ever needed).

# Daily strongest-RS snapshot (output/hk_rs_<date>.txt): the rs-line audit in
# report-only mode. --dry-run is load-bearing — it skips the y/N prompt AND
# guarantees the scheduled run never prunes eod_seen_HK.txt (pruning stays a
# manual, operator-triggered action). Soft side-effect like the report.
set +e
"$UV" run --directory "$PROJECT" main.py --mode rs-line-audit --market hk --dry-run
set -e

exit $EOD_STATUS
