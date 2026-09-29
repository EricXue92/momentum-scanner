"""Tests for the OpenD-down ntfy alert.

Regression: OpenD died on 2026-09-28 between 10:30 and 20:34 HKT and every
scan after that (HK EOD, 9 US morning-gap scans, next day's US EOD + HK
morning-gap) soft-failed with only a WARNING in the log — KOD's pre-market
gap was never scanned. The alert makes the outage visible the first time a
scheduled run hits it, once per day.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import main
from cleanup import cleanup_old_outputs
from notify import notify_opend_down

_CFG = {"notify": {"enabled": True, "ntfy_topic": "t"}}
_TODAY = "2026_09_28"


def _marker(output_dir: Path, today: str = _TODAY) -> Path:
    return output_dir / "state" / f"opend_down_alerted_{today}.txt"


def test_posts_high_priority_alert_naming_mode_and_endpoint(tmp_path: Path) -> None:
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        notify_opend_down("morning-gap", "127.0.0.1", 11111, _CFG, tmp_path, _TODAY)
    assert mock_post.call_count == 1
    _server, _topic, title, body = mock_post.call_args.args
    assert "OpenD" in title
    assert "morning-gap" in body
    assert "127.0.0.1:11111" in body
    assert mock_post.call_args.kwargs["priority"] == "high"


def test_title_is_ascii_safe(tmp_path: Path) -> None:
    """Title goes into an HTTP header — must survive latin-1 encoding."""
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        notify_opend_down("hk-eod", "127.0.0.1", 11111, _CFG, tmp_path, _TODAY)
    mock_post.call_args.args[2].encode("ascii")


def test_second_call_same_day_is_silent(tmp_path: Path) -> None:
    """9 morning-gap scans against a dead OpenD must produce ONE banner."""
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        notify_opend_down("hk-eod", "127.0.0.1", 11111, _CFG, tmp_path, _TODAY)
        notify_opend_down("morning-gap", "127.0.0.1", 11111, _CFG, tmp_path, _TODAY)
    assert mock_post.call_count == 1


def test_next_day_alerts_again(tmp_path: Path) -> None:
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        notify_opend_down("hk-eod", "127.0.0.1", 11111, _CFG, tmp_path, "2026_09_28")
        notify_opend_down("us-eod", "127.0.0.1", 11111, _CFG, tmp_path, "2026_09_29")
    assert mock_post.call_count == 2


def test_failed_post_does_not_consume_the_daily_alert(tmp_path: Path) -> None:
    """If the push itself failed, the next scan must retry instead of the
    day's only alert being lost to a network blip."""
    with patch("notify._ntfy_post", return_value=False):
        notify_opend_down("hk-eod", "127.0.0.1", 11111, _CFG, tmp_path, _TODAY)
    assert not _marker(tmp_path).exists()
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        notify_opend_down("morning-gap", "127.0.0.1", 11111, _CFG, tmp_path, _TODAY)
    assert mock_post.call_count == 1
    assert _marker(tmp_path).exists()


def test_disabled_config_is_noop(tmp_path: Path) -> None:
    cfg = {"notify": {"enabled": False, "ntfy_topic": "t"}}
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        notify_opend_down("morning-gap", "127.0.0.1", 11111, cfg, tmp_path, _TODAY)
    assert not mock_post.called
    assert not _marker(tmp_path).exists()


def test_missing_topic_is_noop(tmp_path: Path) -> None:
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        notify_opend_down(
            "morning-gap", "127.0.0.1", 11111,
            {"notify": {"enabled": True}}, tmp_path, _TODAY,
        )
    assert not mock_post.called


def test_unwritable_state_dir_never_raises(tmp_path: Path) -> None:
    """Same contract as the rest of notify.py: a side-effect, never raises."""
    blocker = tmp_path / "state"
    blocker.write_text("i am a file, not a directory")
    with patch("notify._ntfy_post", return_value=True):
        notify_opend_down("morning-gap", "127.0.0.1", 11111, _CFG, tmp_path, _TODAY)


# --- main._alert_if_opend_down: the probe that decides whether to alert ---


def test_probe_unreachable_sends_alert(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main, "_opend_reachable", lambda h, p, **kw: False)
    cfg = {**_CFG, "futu": {"host": "10.0.0.5", "port": 22222}}
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        main._alert_if_opend_down("morning-gap", cfg, tmp_path, _TODAY)
    assert mock_post.call_count == 1
    assert "10.0.0.5:22222" in mock_post.call_args.args[3]


def test_probe_reachable_is_silent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main, "_opend_reachable", lambda h, p, **kw: True)
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        main._alert_if_opend_down("morning-gap", _CFG, tmp_path, _TODAY)
    assert not mock_post.called
    assert not _marker(tmp_path).exists()


def test_probe_error_never_raises(tmp_path: Path, monkeypatch) -> None:
    def boom(h, p, **kw):
        raise RuntimeError("socket exploded")

    monkeypatch.setattr(main, "_opend_reachable", boom)
    main._alert_if_opend_down("morning-gap", _CFG, tmp_path, _TODAY)


# --- cleanup: the per-day marker ages out like the other state caches ---


def test_cleanup_ages_out_old_markers(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    for d in ("2026_09_29", "2026_09_28", "2026_09_27"):
        (state / f"opend_down_alerted_{d}.txt").write_text("x")
    cleanup_old_outputs(tmp_path, date(2026, 9, 29))
    assert (state / "opend_down_alerted_2026_09_29.txt").exists()
    assert (state / "opend_down_alerted_2026_09_28.txt").exists()
    assert not (state / "opend_down_alerted_2026_09_27.txt").exists()
