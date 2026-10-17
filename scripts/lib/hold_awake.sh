#!/usr/bin/env bash
# Sourced by the launchd wrappers. `hold_awake [caffeinate-binary]` holds a
# sleep assertion for the life of the calling script.
#
# The schedule assumes AC power; on battery `pmset -g` has `sleep 1`, and on
# 2026-10-17 the 10:00 us-eod idle-slept 3 min in (lid closed, 12%): the
# process then only advanced inside 5-15s DarkWake windows with no Wi-Fi/DNS
# (55 min, yfinance empty, Futu 网络中断, conditional RS lost). -i prevents
# idle sleep (works on battery), -s system sleep (AC only); a lid-close by
# the user still sleeps the Mac. -w $$ releases the assertion when the
# caller exits; a watchdog's process-group kill does not reach it
# (caffeinate stays in the wrapper's group). Logs the power source once.
hold_awake() {
    local bin="${1:-/usr/bin/caffeinate}"
    if [[ -x "$bin" ]]; then
        "$bin" -i -s -w $$ &
    fi
    if [[ -x /usr/bin/pmset ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] power: $(/usr/bin/pmset -g batt | head -1)"
    fi
}
