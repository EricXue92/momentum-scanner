"""Power probes for scheduled runs (macOS): power source and time asleep.

What actually breaks a scheduled scan is **battery + closed lid**, not
battery alone (a week of `pmset -g log`, 2026-11-12..19): with the lid open
a pmset wake is a full wake and the wrappers' `caffeinate -i` holds the scan;
with the lid closed the wake is a 5-19s DarkWake, the assertion is ignored,
and a job launched inside one stays frozen until the lid opens (2026-11-19:
hk-eod launched 20:05, resumed 20:23). Before caffeinate the same state cost
a whole run (2026-10-17: 55-min us-eod, yfinance empty, Futu 网络中断,
conditional RS lost to NameResolutionError).

So the pipeline alerts on the two things that matter (see
`main._alert_if_on_battery`): a low battery (`power_status`) and a run that
was frozen by sleep (`asleep_seconds`).

Pure reads; soft-fail to None on any error (non-macOS, pmset missing).
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

_SOURCE_RE = re.compile(r"Now drawing from '([^']+)'")
_PERCENT_RE = re.compile(r"(\d{1,3})%")


@dataclass(frozen=True)
class PowerStatus:
    on_battery: bool
    percent: int | None


def parse_pmset_batt(text: str) -> PowerStatus | None:
    """Parse `pmset -g batt` output. None when the source line is absent."""
    m = _SOURCE_RE.search(text or "")
    if not m:
        return None
    source = m.group(1).lower()
    on_battery = "battery" in source
    pm = _PERCENT_RE.search(text)
    percent = int(pm.group(1)) if pm else None
    return PowerStatus(on_battery=on_battery, percent=percent)


def _run_pmset() -> str:
    return subprocess.run(
        ["/usr/bin/pmset", "-g", "batt"],
        capture_output=True, text=True, timeout=10, check=False,
    ).stdout


def power_status(runner: Callable[[], str] = _run_pmset) -> PowerStatus | None:
    """Current power source, or None when it cannot be determined."""
    try:
        return parse_pmset_batt(runner())
    except Exception as e:  # noqa: BLE001 — probe must never raise
        logger.debug(f"[power] pmset probe failed: {e}")
        return None


def _continuous_clock() -> float:
    return time.clock_gettime(time.CLOCK_MONOTONIC_RAW)  # keeps counting asleep


def _awake_clock() -> float:
    return time.clock_gettime(time.CLOCK_UPTIME_RAW)  # stops while asleep


def asleep_seconds(
    continuous: Callable[[], float] = _continuous_clock,
    awake: Callable[[], float] = _awake_clock,
) -> float | None:
    """Seconds the system has spent asleep since boot, or None when unknown.

    The difference of two kernel clocks, so the delta between two calls is
    the time a process was frozen by sleep in between — independent of the
    wall clock and of when launchd got around to starting the job.
    """
    try:
        return continuous() - awake()
    except Exception as e:  # noqa: BLE001 — probe must never raise
        logger.debug(f"[power] asleep-clock probe failed: {e}")
        return None
