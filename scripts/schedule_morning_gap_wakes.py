#!/usr/bin/env python3
"""Schedule pmset wake events for the morning-gap scans.

The launchd job triggers at 21:10-22:00 HKT (EDT) or 22:10-23:00 HKT (EST),
covering pre-market (-20/-10) and post-open (+10..+30) scans relative to
NY 9:30 open. This script schedules Mac wake-ups 11 min before each
window's first (pre-market -20) scan:
  - 20:59 HKT covers EDT (NY 9:10 AM)
  - 21:59 HKT covers EST (NY 9:10 AM)

It also wakes the Mac 1 min before the launchd RS-workflow triggers
(us-rs-3m-trigger Tue-Sat 08:45, hk-rs-trigger Mon-Fri 18:45). Without a
wake, launchd runs a missed slot on the next wake — 2026-10-15 the US one
fired at 09:00 with no network, so us-eod got a stale 3M table.

HK jobs get the same 1-min lead: every hk-morning-gap slot (Mon-Fri
09:40-10:30, one wake per slot — `pmset sleep 1` puts an idle Mac back to
sleep within a minute, and caffeinate only holds while a scan runs) and
hk-eod (Mon-Fri 20:00). 2026-10-16 hk-eod had no wake on battery with the
lid closed and only finished at 20:18, when the lid was opened.

`pmset schedule` events are one-shot. Re-run periodically (e.g. weekly via
cron/launchd or manually). Re-running is safe — past events expire on their
own, duplicate future events are harmless extra wakes.

Usage:
    sudo uv run scripts/schedule_morning_gap_wakes.py [days]

Default: schedules next 14 weekdays. Verify with `pmset -g sched`.
"""

import os
import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MON_FRI = frozenset(range(5))
TUE_SAT = frozenset(range(1, 6))
# (HKT wall time, Python weekdays it applies to)
WAKES = [
    ("08:44:00", TUE_SAT),  # us-rs-3m-trigger 08:45
    ("09:39:00", MON_FRI),  # hk-morning-gap 09:40
    ("09:49:00", MON_FRI),  # hk-morning-gap 09:50
    ("09:59:00", MON_FRI),  # hk-morning-gap 10:00
    ("10:09:00", MON_FRI),  # hk-morning-gap 10:10
    ("10:19:00", MON_FRI),  # hk-morning-gap 10:20
    ("10:29:00", MON_FRI),  # hk-morning-gap 10:30
    ("18:44:00", MON_FRI),  # hk-rs-trigger 18:45
    ("19:59:00", MON_FRI),  # hk-eod 20:00
    ("20:59:00", MON_FRI),  # morning-gap, EDT
    ("21:59:00", MON_FRI),  # morning-gap, EST
]
DEFAULT_DAYS = 14


def wake_events(now_hk: datetime, n_days: int) -> list[datetime]:
    """Future wake datetimes covering the next `n_days` Mon-Fri days (and
    the Saturdays in between, for the Tue-Sat US trigger)."""
    events = []
    d = now_hk.date()
    days_processed = 0
    while days_processed < n_days:
        for t, weekdays in WAKES:
            if d.weekday() not in weekdays:
                continue
            wake_dt = datetime.combine(
                d,
                datetime.strptime(t, "%H:%M:%S").time(),
                tzinfo=ZoneInfo("Asia/Hong_Kong"),
            )
            if wake_dt > now_hk:
                events.append(wake_dt)
        if d.weekday() < 5:
            days_processed += 1
        d += timedelta(days=1)
    return events


def main() -> int:
    if os.geteuid() != 0:
        print("ERROR: pmset schedule requires root. Re-run with sudo.", file=sys.stderr)
        return 1

    n_days = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DAYS
    now_hk = datetime.now(ZoneInfo("Asia/Hong_Kong"))

    scheduled = 0
    failed = 0
    for wake_dt in wake_events(now_hk, n_days):
        schedule_str = f"{wake_dt:%m/%d/%y %H:%M:%S}"
        cmd = ["pmset", "schedule", "wake", schedule_str]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  scheduled: {schedule_str}")
            scheduled += 1
        else:
            print(
                f"  FAILED: {schedule_str} — {result.stderr.strip()}",
                file=sys.stderr,
            )
            failed += 1

    print(
        f"\nDone: {scheduled} scheduled, {failed} failed."
    )
    print("Verify: pmset -g sched")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
