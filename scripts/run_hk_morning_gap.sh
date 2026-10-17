#!/usr/bin/env bash
# Wrapper for the HK morning-gap launchd job (Mon-Fri 09:35/09:40/09:45/
# 09:50/09:55/10:00 HKT — i.e. +5/+10/+15/+20/+25/+30 minutes after HK
# market open at 09:30). Same log-rotation pattern as run_eod.sh.
#
# HK has no pre-auction data via Futu (verified 2026-05-11) so this scan is
# post-open only. Self-validates the scan window via `_get_hkt_scan_offset`
# and exits cleanly outside any window — DO NOT add a hard error path here.
set -euo pipefail

LOG=/Users/xue/momentum-scanner/output/launchd_HK_morning_gap.log
mkdir -p "$(dirname "$LOG")"

if [[ -f "$LOG" && "$(date -r "$LOG" +%Y-%m-%d)" != "$(date +%Y-%m-%d)" ]]; then
    : > "$LOG"
fi

exec >> "$LOG" 2>&1

UV=/Users/xue/.local/bin/uv
PROJECT=/Users/xue/momentum-scanner

# caffeinate -i -s: hold a sleep assertion for the scan's lifetime (same
# battery/DarkWake failure mode as run_eod.sh, 2026-10-17); exits with the
# scan's status.
exec /usr/bin/caffeinate -i -s "$UV" run --directory "$PROJECT" main.py --mode hk-morning-gap
