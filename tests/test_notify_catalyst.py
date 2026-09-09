"""Tests for notify_morning_catalyst_ready."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from notify import notify_morning_catalyst_ready


def test_notify_morning_catalyst_ready_posts_when_enabled() -> None:
    cfg = {"notify": {"enabled": True, "ntfy_topic": "topic-xyz"}}
    with patch("notify._ntfy_post") as mock_post:
        notify_morning_catalyst_ready(
            report_path=Path("/tmp/PreMarket/2026_06_03_us_premarket.html"),
            offset_min=-20,
            n_tickers=5,
            config=cfg,
        )
    mock_post.assert_called_once()
    args = mock_post.call_args.args
    assert args[1] == "topic-xyz"  # topic
    assert "-20min" in args[2]      # title
    assert "5" in args[2]
    assert "2026_06_03_us_premarket.html" in args[3]  # body


def test_notify_morning_catalyst_ready_skipped_when_disabled() -> None:
    cfg = {"notify": {"enabled": False, "ntfy_topic": "t"}}
    with patch("notify._ntfy_post") as mock_post:
        notify_morning_catalyst_ready(
            report_path=Path("/tmp/x.md"),
            offset_min=-10,
            n_tickers=1,
            config=cfg,
        )
    mock_post.assert_not_called()
