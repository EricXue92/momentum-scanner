"""Power-source probe for scheduled runs (macOS `pmset -g batt`).

Why it matters: the launchd pipeline is designed for a Mac on AC. On battery
`pmset -g` has `sleep 1`, so a few minutes after a pmset wake the machine
idle-sleeps and a running scan only advances inside 5-15s DarkWake windows
where Wi-Fi/DNS is not yet up (2026-10-17: 55-min us-eod, yfinance empty,
Futu 网络中断, conditional RS lost to NameResolutionError). The wrappers now
hold a `caffeinate -i` assertion, but a battery run is still degraded, so
the pipeline warns once per day (see `notify.notify_on_battery`).

Pure read; soft-fails to None on any error (non-macOS, pmset missing).
"""

from __future__ import annotations

import logging
import re
import subprocess
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
