"""pmset wakes must cover every launchd slot of the scheduled jobs.

2026-10-15: no wake at 08:45, so launchd ran us-rs-3m-trigger at the next
wake (09:00) before the network was up; the dispatch failed and us-eod fell
back to a stale 3M table. 2026-10-16: no wake at 20:00, hk-eod on battery
with the lid closed only finished when the lid was opened at 20:18."""
import plistlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from schedule_morning_gap_wakes import WAKES, wake_events  # noqa: E402

HKT = ZoneInfo("Asia/Hong_Kong")
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
HK_GAP = ["09:39", "09:49", "09:59", "10:09", "10:19", "10:29"]
US_GAP = [
    "20:59", "21:09", "21:19", "21:24", "21:34", "21:39", "21:44", "21:49",
    "21:54", "21:59", "22:09", "22:19", "22:24", "22:34", "22:39", "22:44",
    "22:49", "22:54", "22:59",
]


def _by_day(events):
    out: dict[str, list[str]] = {}
    for e in events:
        out.setdefault(f"{e:%a}", []).append(f"{e:%H:%M}")
    return out


def test_one_week_from_sunday():
    # Sunday 2026-10-18 18:00 — when the weekly LaunchDaemon reseeds.
    events = wake_events(datetime(2026, 10, 18, 18, 0, tzinfo=HKT), 5)
    days = _by_day(events)
    weekday = HK_GAP + ["18:44", "19:59"] + US_GAP
    assert days["Mon"] == weekday  # no US trigger on Monday
    for d in ("Tue", "Wed", "Thu", "Fri"):
        assert days[d] == ["08:44"] + weekday
    assert "Sat" not in days  # 5 weekdays end on Friday
    assert "Sun" not in days


def test_saturday_us_trigger_and_past_skipped():
    # Friday 2026-10-16 19:00: 08:44, HK gap and 18:44 already passed.
    events = wake_events(datetime(2026, 10, 16, 19, 0, tzinfo=HKT), 2)
    days = _by_day(events)
    assert days["Fri"] == ["19:59"] + US_GAP
    assert days["Sat"] == ["08:44"]  # HK jobs are Mon-Fri only
    assert days["Mon"] == HK_GAP + ["18:44", "19:59"] + US_GAP


@pytest.mark.parametrize(
    "plist",
    [
        "com.xue.finviz-to-tv.morning-gap.plist",
        "com.xue.finviz-to-tv.hk-morning-gap.plist",
        "com.xue.finviz-to-tv.hk-eod.plist",
        "com.xue.finviz-to-tv.hk-rs-trigger.plist",
        "com.xue.finviz-to-tv.us-rs-3m-trigger.plist",
    ],
)
def test_every_plist_slot_has_a_wake_one_min_before(plist):
    # us-eod (com.xue.finviz-to-tv.plist) is covered by the repeating
    # `pmset repeat wakeorpoweron` 09:59 Tue-Sat event instead.
    wakes = dict(WAKES)
    slots = plistlib.loads((SCRIPTS / plist).read_bytes())["StartCalendarInterval"]
    if isinstance(slots, dict):
        slots = [slots]
    for slot in slots:
        lead = datetime(2026, 1, 1, slot["Hour"], slot["Minute"]) - timedelta(minutes=1)
        key = f"{lead:%H:%M}:00"
        py_weekday = (slot["Weekday"] - 1) % 7  # launchd 0=Sun → Python 6
        assert key in wakes, f"{plist} {slot} has no wake at {key}"
        assert py_weekday in wakes[key], f"{plist} {slot}: {key} misses weekday"
