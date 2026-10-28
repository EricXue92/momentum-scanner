#!/usr/bin/env bash
# Wrapper for the US EOD launchd job (10:00 HKT, Tue-Sat). Two responsibilities:
#   1. Rotate output/launchd_US.log if its last-modification date is not today,
#      so each calendar day starts with a fresh log. Multiple same-day
#      invocations (launchd + manual reruns) still append.
#   2. Redirect stdout/stderr to the log itself. The matching plist
#      (com.xue.finviz-to-tv.plist) therefore does NOT set
#      StandardOutPath/StandardErrorPath — launchd would otherwise hold
#      an append-mode fd open through any in-process truncate, leaving
#      a NUL-padded hole at the start of the new day's log.
#
# HK scanning has its own slot at 20:00 HKT — see run_hk_eod.sh /
# com.xue.finviz-to-tv.hk-eod.plist. The US slot uses --mode us-eod so HK is
# explicitly skipped here; running HK at 10 AM HKT would pull incomplete
# intraday k-line bars (HK market opens 09:30 HKT, just 30 min before).
set -euo pipefail

LOG=/Users/xue/momentum-scanner/output/launchd_US.log
mkdir -p "$(dirname "$LOG")"

if [[ -f "$LOG" && "$(date -r "$LOG" +%Y-%m-%d)" != "$(date +%Y-%m-%d)" ]]; then
    : > "$LOG"
fi

exec >> "$LOG" 2>&1

# Sleep assertion for the life of this wrapper (see scripts/lib/hold_awake.sh).
# shellcheck source=lib/hold_awake.sh
source /Users/xue/momentum-scanner/scripts/lib/hold_awake.sh
hold_awake

# Load secrets (ANTHROPIC_API_KEY for the anthropic report backend, or
# DEEPSEEK_API_KEY + TAVILY_API_KEY for the deepseek backend) from the
# project's .env (gitignored). Launchd does NOT inherit the user's interactive
# shell environment, so without this the report step would skip with a
# "backend init failed" warning.
ENV_FILE=/Users/xue/momentum-scanner/.env
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "$ENV_FILE"
    set +a
fi

UV=/Users/xue/.local/bin/uv
PROJECT=/Users/xue/momentum-scanner

# Hard wall-clock cap for the EOD step. A real run is ~3-5 min, but us-eod
# may legitimately WAIT up to 60 min for a fresh 12M RS table
# (rs_rating._STALE_MAX_WAIT_SEC, polling every 5 min) before it does any
# work, so the cap MUST exceed that wait plus a full run: 90 min. The old
# 30 min cap killed the 2026-10-28 run at 10:30:06 while it was still polling
# (rs-log published at 10:42); SIGTERM leaves no trace in the python log, so
# the day looked like "ran, wrote nothing". The finviz lib's SSL hang
# (2026-05-12: 50+ min; 2026-06-16: 12 min, pushed Leaders past the watchdog
# with no .txt written) is bounded in-process by `finviz_timeout.py` (socket
# timeout + capped retries) — this wrapper timer remains as defense-in-depth
# for anything else that might hang. macOS ships no `timeout`, so `set -m`
# puts the backgrounded job in its own process group, then a watchdog logs
# one line and sends SIGTERM (escalates to SIGKILL) to the whole group so
# uv's python grandchild dies with its parent instead of leaking.
EOD_TIMEOUT=${EOD_TIMEOUT:-5400}
set +e
set -m
"$UV" run --directory "$PROJECT" main.py --mode us-eod &
EOD_PID=$!
set +m
( sleep "$EOD_TIMEOUT" \
    && echo "$(date '+%Y-%m-%d %H:%M:%S') [ERROR] [watchdog] us-eod exceeded EOD_TIMEOUT=${EOD_TIMEOUT}s — sending SIGTERM to process group $EOD_PID" \
    && kill -TERM "-$EOD_PID" 2>/dev/null \
    && sleep 15 && kill -KILL "-$EOD_PID" 2>/dev/null ) &
WATCHDOG_PID=$!
wait "$EOD_PID"
EOD_STATUS=$?
kill "$WATCHDOG_PID" 2>/dev/null
wait "$WATCHDOG_PID" 2>/dev/null
set -e

# CANSLIM report step DISABLED 2026-09-17 (operator request: stop the daily
# LLM report). Run `main.py --mode report --market us` by hand if one is
# ever needed. Re-enable by uncommenting the block below.
# set +e
# "$UV" run --directory "$PROJECT" main.py --mode report --market us
# set -e

# Daily strongest-RS snapshot (output/rs_us_<date>.txt): the rs-line audit in
# report-only mode. --dry-run is load-bearing — it skips the y/N prompt AND
# guarantees the scheduled run never prunes eod_seen_US.txt (pruning stays a
# manual, operator-triggered action). Soft side-effect like the report.
set +e
"$UV" run --directory "$PROJECT" main.py --mode rs-line-audit --market us --dry-run
set -e

# Refresh the GitHub Pages ETF RS trend page now that us-eod has finished
# (the local copy under output/Reports/ETF/ is already current). The page
# workflow otherwise rides workflow_run after the 08:45 US 3M RS dispatch,
# which fails whenever that slot wakes without a network (2026-10-23: on
# battery, 10/10 dispatch attempts failed, the page stayed on 10-22 while the
# local .txt was 10-23); GH's backup cron is unreliable. We are awake with a
# working network here, so a direct dispatch is the reliable daily tick.
# Soft: a failure must not change the EOD exit status. Idempotent: the page
# workflow just redeploys (concurrency group `pages`).
GH=${GH:-/opt/homebrew/bin/gh}
export PATH="/opt/homebrew/bin:$PATH"   # launchd PATH lacks homebrew; gh shells out to git
if [[ -x "$GH" ]]; then
    if PAGES_OUT=$("$GH" --repo EricXue92/momentum-scanner workflow run etf_rs_page.yml 2>&1); then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] [ETF RS] dispatched etf_rs_page.yml (Pages refresh)"
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') [WARNING] [ETF RS] gh workflow run etf_rs_page.yml failed: $PAGES_OUT"
    fi
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [WARNING] [ETF RS] gh not found at $GH; Pages page not refreshed"
fi

exit $EOD_STATUS
