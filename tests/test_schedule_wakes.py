"""pmset wakes must cover the launchd RS-workflow triggers.

2026-10-15: no wake at 08:45, so launchd ran us-rs-3m-trigger at the next
wake (09:00) before the network was up; the dispatch failed and us-eod fell
back to a stale 3M table."""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from schedule_morning_gap_wakes import wake_events  # noqa: E402

HKT = ZoneInfo("Asia/Hong_Kong")


def _by_day(events):
    out: dict[str, list[str]] = {}
    for e in events:
        out.setdefault(f"{e:%a}", []).append(f"{e:%H:%M}")
    return out


def test_one_week_from_sunday():
    # Sunday 2026-10-18 18:00 — when the weekly LaunchDaemon reseeds.
    events = wake_events(datetime(2026, 10, 18, 18, 0, tzinfo=HKT), 5)
    days = _by_day(events)
    weekday = ["18:44", "20:59", "21:59"]
    assert days["Mon"] == weekday  # no US trigger on Monday
    for d in ("Tue", "Wed", "Thu", "Fri"):
        assert days[d] == ["08:44"] + weekday
    assert "Sat" not in days  # 5 weekdays end on Friday
    assert "Sun" not in days


def test_saturday_us_trigger_and_past_skipped():
    # Friday 2026-10-16 19:00: 08:44 and 18:44 already passed.
    events = wake_events(datetime(2026, 10, 16, 19, 0, tzinfo=HKT), 2)
    days = _by_day(events)
    assert days["Fri"] == ["20:59", "21:59"]
    assert days["Sat"] == ["08:44"]
    assert days["Mon"] == ["18:44", "20:59", "21:59"]
