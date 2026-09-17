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

# Hard wall-clock cap for the EOD step. A real run is ~3-5 min; anything past
# 30 min means a hung socket. The finviz lib's SSL hang (2026-05-12: 50+ min;
# 2026-06-16: 12 min, pushed Leaders past the watchdog with no .txt written)
# is now bounded in-process by `finviz_timeout.py` (socket timeout + capped
# retries) — this wrapper timer remains as defense-in-depth for anything else
# that might hang. macOS ships no `timeout`, so `set -m` puts the backgrounded
# job in its own process group, then a watchdog sends SIGTERM (escalates to
# SIGKILL) to the whole group so uv's python grandchild dies with its parent
# instead of leaking.
EOD_TIMEOUT=${EOD_TIMEOUT:-1800}
set +e
set -m
"$UV" run --directory "$PROJECT" main.py --mode us-eod &
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

exit $EOD_STATUS
