---
paths:
  - "scripts/**"
  - "power.py"
  - "net_ready.py"
  - "tests/test_schedule_wakes.py"
  - "tests/test_trigger_rs_workflow.py"
  - "tests/test_net_ready.py"
---

# Scheduling: pmset wakes, caffeinate, RS workflow trigger

Loaded only when working with the files above (moved out of the root
`CLAUDE.md` on 2026-11-04). Incident history: `docs/decisions.md`. The morning-gap
window rule and the caffeinate prohibition stay in the root file.

- pmset wake keyword is **`wakeorpoweron`** on macOS 26+ (`wakepoweron` no longer
  parses). US EOD uses mode `us-eod` not `eod` (HK bar is incomplete at 10:00 HKT).
- **The schedule assumes AC power; every scheduled job holds a `caffeinate
  -i -s` assertion** (`run_eod.sh` / `run_hk_eod.sh` / `trigger_rs_workflow.sh`
  source `scripts/lib/hold_awake.sh` → `caffeinate -i -s -w $$ &`;
  `run_morning_gap.sh` / `run_hk_morning_gap.sh` `exec caffeinate -i -s uv
  run …`; the detached catalyst subprocess prepends its own
  `/usr/bin/caffeinate -i -s`, `main._CAFFEINATE_BIN`). Do not remove. `-i`
  works on battery, `-s` is AC-only; a lid-close by the user still sleeps
  the Mac (→ decisions 2026-10-17). Belt-and-suspenders: EOD/in-window
  morning-gap runs probe `pmset -g batt` (`power.py`,
  `main._alert_if_on_battery`) and ntfy once per HKT day
  (`state/on_battery_alerted_<date>.txt`, 2-day cleanup); the conditional
  RS group retries network errors (`_retry_on_network_error`: wait for
  finviz reachable ≤ 180s, 3 attempts; non-network errors propagate); and
  the trigger script polls `gh run view` status/conclusion for up to ~20 min
  before alerting — `completed`+non-success → "failed", never confirmed →
  "unconfirmed" (default priority, exit 2). `TRIGGER_*` env overrides exist
  only for `tests/test_trigger_rs_workflow.py`. The US morning-gap plist
  runs `run_morning_gap.sh` (log rotated daily like the other wrappers).
- **RS workflow self-trigger:** GH Actions' scheduled cron is unreliable
  (delayed hours, sometimes skipped; → decisions 2026-06-16). Launchd
  dispatches the workflow via `gh workflow run` 75 min before EOD:
  `us-rs-3m-trigger` (Tue-Sat 08:45) and `hk-rs-trigger` (Mon-Fri 18:45) →
  `scripts/trigger_rs_workflow.sh`. The GH-side cron is kept as
  belt-and-suspenders; the workflow's commit step is idempotent (`git diff
  --staged --quiet → exit 0`) so a double-fire is harmless. Failures ntfy via
  the morning-gap topic. Each trigger has a pmset wake 1 min before it
  (08:44 Tue-Sat / 18:44 Mon-Fri, seeded weekly by
  `schedule_morning_gap_wakes.py` with the morning-gap wakes) and the script
  retries the dispatch ~5 min (→ decisions 2026-10-15). Every other launchd
  slot gets the same 1-min-lead wake (Mon-Fri): one per US / HK morning-gap
  slot (`US_GAP_SLOTS` / `HK_GAP_SLOTS` — `pmset sleep 1` re-sleeps an idle
  Mac within a minute and caffeinate only holds during a scan, so one wake
  covers one slot; the 20:59 / 21:59 11-min-lead wakes stay) and 19:59 for
  hk-eod (→ decisions 2026-10-16). `tests/test_schedule_wakes.py` reads the
  plists and fails when a slot has no wake — edit the slot lists together
  with the plists.
