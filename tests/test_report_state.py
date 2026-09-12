import os
from pathlib import Path

import pytest

from report import state


def test_priority_order_is_complete():
    assert state.PRIORITY_ORDER == [
        "TheSetup",
        "EarningsGap",
        "HighVolume",
        "Leaders",
        "GapUp",
        "NewHigh52W",
        "IPO",
        "TopGainers",
        "RS",
    ]


def test_max_tickers_cap():
    """Daily cap is intentionally tight (cost vs. signal-quality trade-off);
    surface any change so a careless bump in state.py shows up in review."""
    assert state.MAX_TICKERS_PER_REPORT == 30


def test_get_api_key_returns_env_value(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test123")
    assert state.get_api_key() == "sk-ant-test123"


def test_get_api_key_returns_none_when_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert state.get_api_key() is None


def test_reports_dir_is_under_project_root():
    assert state.OUTPUT_REPORTS_DIR.name == "Reports"
    assert state.OUTPUT_REPORTS_DIR.parent.name == "output"


def test_postmarket_and_premarket_dirs_are_under_reports():
    assert state.POSTMARKET_DIR == state.OUTPUT_REPORTS_DIR / "PostMarket"
    assert state.PREMARKET_DIR == state.OUTPUT_REPORTS_DIR / "PreMarket"


def test_premarket_state_dir_is_output_state():
    assert state.OUTPUT_STATE_DIR == state.PROJECT_ROOT / "output" / "state"


def test_input_dir_for_market():
    us_dir = state.input_dir_for_market("us")
    hk_dir = state.input_dir_for_market("hk")
    assert us_dir.name == "US"
    assert hk_dir.name == "HK"
    assert us_dir.parent.name == "TV"


def test_input_dir_for_market_invalid():
    with pytest.raises(ValueError, match="market"):
        state.input_dir_for_market("uk")


def test_groups_for_us_includes_nine():
    assert state.groups_for_market("us") == [
        "TheSetup", "EarningsGap", "HighVolume", "Leaders", "GapUp",
        "NewHigh52W", "IPO", "TopGainers", "RS",
    ]


def test_groups_for_hk_excludes_newhigh_topgainers():
    assert state.groups_for_market("hk") == [
        "EarningsGap", "HighVolume", "Leaders", "GapUp", "IPO", "RS",
    ]


def test_load_dotenv_populates_env_when_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FOO_BAR", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "ANTHROPIC_API_KEY=sk-ant-xyz\n"
        "FOO_BAR=baz\n"
        "\n"
    )
    state.load_dotenv(env)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-xyz"
    assert os.environ["FOO_BAR"] == "baz"


def test_load_dotenv_does_not_override_existing(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "already-set")
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=should-not-overwrite\n")
    state.load_dotenv(env)
    assert os.environ["ANTHROPIC_API_KEY"] == "already-set"


def test_load_dotenv_handles_export_prefix_and_quotes(monkeypatch, tmp_path):
    monkeypatch.delenv("KEY1", raising=False)
    monkeypatch.delenv("KEY2", raising=False)
    env = tmp_path / ".env"
    env.write_text('export KEY1="quoted"\nKEY2=\'single-quoted\'\n')
    state.load_dotenv(env)
    assert os.environ["KEY1"] == "quoted"
    assert os.environ["KEY2"] == "single-quoted"


def test_load_dotenv_missing_file_is_noop(tmp_path):
    state.load_dotenv(tmp_path / "nope.env")  # must not raise
