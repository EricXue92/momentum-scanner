#!/usr/bin/env bash
# Wrapper for the US morning-gap launchd job (Mon-Fri, 9 scan offsets in both
# the EDT and EST mappings — see com.xue.finviz-to-tv.morning-gap.plist).
# Same log-rotation pattern as run_eod.sh: the plist sets no
# StandardOutPath/StandardErrorPath, so the log no longer grows forever
# (launchd_morning_gap.log had reached 2.5MB).
#
# main.py self-validates the ET scan window via `_get_et_scan_offset` and
# exits cleanly outside any window — DO NOT add a hard error path here.
# No .env here: the catalyst-report subprocess loads its own keys
# (report.state.load_dotenv).
set -euo pipefail

LOG=/Users/xue/momentum-scanner/output/launchd_morning_gap.log
mkdir -p "$(dirname "$LOG")"

if [[ -f "$LOG" && "$(date -r "$LOG" +%Y-%m-%d)" != "$(date +%Y-%m-%d)" ]]; then
    : > "$LOG"
fi

exec >> "$LOG" 2>&1

UV=/Users/xue/.local/bin/uv
PROJECT=/Users/xue/momentum-scanner

# caffeinate -i -s: hold a sleep assertion for the scan's lifetime (battery /
# DarkWake failure mode, 2026-10-17); exits with the scan's status.
exec /usr/bin/caffeinate -i -s "$UV" run --directory "$PROJECT" main.py --mode morning-gap
