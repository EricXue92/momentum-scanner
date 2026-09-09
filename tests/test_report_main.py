import csv
from pathlib import Path

import pytest

from report import __main__ as orch


def test_normalize_rs_key_us():
    assert orch._normalize_rs_key("AAPL", "us") == "AAPL"
    assert orch._normalize_rs_key("aapl", "us") == "AAPL"


def test_normalize_rs_key_hk_strips_futu_prefix_and_padding():
    assert orch._normalize_rs_key("HK.00700", "hk") == "0700.HK"
    assert orch._normalize_rs_key("HK.00001", "hk") == "0001.HK"
    assert orch._normalize_rs_key("HK.09988", "hk") == "9988.HK"


def test_normalize_rs_key_hk_passthrough_when_already_yf_form():
    # Should not double-process if input is already yfinance form
    assert orch._normalize_rs_key("0700.HK", "hk") == "0700.HK"


def test_split_exchange_ticker():
    assert orch._split_exchange_ticker("NASDAQ:AAPL") == ("NASDAQ", "AAPL")
    assert orch._split_exchange_ticker("HKEX:00700") == ("HKEX", "00700")
    assert orch._split_exchange_ticker("AAPL") == ("", "AAPL")


def test_yf_ticker_us_unchanged():
    assert orch._yf_ticker("AAPL", "us") == "AAPL"


def test_yf_ticker_hk_pads_to_four_digits():
    assert orch._yf_ticker("00700", "hk") == "0700.HK"
    assert orch._yf_ticker("700", "hk") == "0700.HK"
    assert orch._yf_ticker("09988", "hk") == "9988.HK"


def test_load_rs_lookup_us_capital_p_column(tmp_path: Path, monkeypatch):
    """Real US CSV uses capital 'Percentile'; lookup must read that column."""
    state_dir = tmp_path / "output" / "state"
    state_dir.mkdir(parents=True)
    csv_path = state_dir / "rs_rating_2026_05_07.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Rank", "Ticker", "Percentile"])
        writer.writerow([1, "AAPL", 95])
        writer.writerow([2, "NVDA", 99])
    monkeypatch.setattr(orch, "PROJECT_ROOT", tmp_path)
    lookup = orch._load_rs_lookup("us", "2026_05_07")
    assert lookup("AAPL") == 95
    assert lookup("NVDA") == 99
    assert lookup("UNKNOWN") is None


def test_load_rs_lookup_hk_normalizes_futu_prefix(tmp_path: Path, monkeypatch):
    """HK CSV stores 'HK.00700'; lookup must normalize to yfinance form '0700.HK'."""
    state_dir = tmp_path / "output" / "state"
    state_dir.mkdir(parents=True)
    csv_path = state_dir / "hk_rs_rating_2026-05-07.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ticker", "percentile"])
        writer.writerow(["HK.00700", 92])
        writer.writerow(["HK.09988", 88])
    monkeypatch.setattr(orch, "PROJECT_ROOT", tmp_path)
    lookup = orch._load_rs_lookup("hk", "2026_05_07")
    assert lookup("0700.HK") == 92
    assert lookup("9988.HK") == 88
    assert lookup("UNKNOWN.HK") is None


def test_load_rs_lookup_missing_cache_returns_passthrough(tmp_path: Path, monkeypatch):
    """No cache file → lookup is callable, returns None for everything."""
    monkeypatch.setattr(orch, "PROJECT_ROOT", tmp_path)
    (tmp_path / "output" / "state").mkdir(parents=True)
    lookup = orch._load_rs_lookup("us", "2026_05_07")
    assert lookup("AAPL") is None


def test_load_rs_lookup_hk_ignores_3m_cache_when_only_3m_present(
    tmp_path: Path, monkeypatch
):
    """If only the 3M cache file is present (no 12M file), the HK lookup
    must NOT silently use the 3M values — it should return an all-None
    lookup. Defends against future regression where a refactor turns the
    candidate list into a glob."""
    state_dir = tmp_path / "output" / "state"
    state_dir.mkdir(parents=True)
    csv_3m = state_dir / "hk_rs_rating_3m_2026-05-07.csv"
    with csv_3m.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ticker", "percentile"])
        writer.writerow(["HK.00700", 92])
    monkeypatch.setattr(orch, "PROJECT_ROOT", tmp_path)
    lookup = orch._load_rs_lookup("hk", "2026_05_07")
    # 12M 表不存在 → 即使 3M 表里有 0700，日报也不应读到它
    assert lookup("0700.HK") is None


def test_load_rs_lookup_hk_uses_12m_cache_when_both_present(
    tmp_path: Path, monkeypatch
):
    """12M and 3M caches coexist; lookup must read the 12M file."""
    state_dir = tmp_path / "output" / "state"
    state_dir.mkdir(parents=True)
    csv_12m = state_dir / "hk_rs_rating_2026-05-07.csv"
    with csv_12m.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ticker", "percentile"])
        writer.writerow(["HK.00700", 92])
    csv_3m = state_dir / "hk_rs_rating_3m_2026-05-07.csv"
    with csv_3m.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ticker", "percentile"])
        writer.writerow(["HK.00700", 77])  # 不同分数，确认读的是 12M
    monkeypatch.setattr(orch, "PROJECT_ROOT", tmp_path)
    lookup = orch._load_rs_lookup("hk", "2026_05_07")
    assert lookup("0700.HK") == 92  # 12M 表的值，不是 3M 表的 77


import asyncio  # noqa: E402
from datetime import date  # noqa: E402

from report import __main__ as report_main  # noqa: E402
from report import evidence  # noqa: E402


def test_load_evidence_config_reads_subsection():
    cfg = report_main._load_evidence_config({"evidence": {"enabled": False, "news_max_items": 4}})
    assert cfg.enabled is False and cfg.news_max_items == 4


def test_load_evidence_config_defaults_when_missing():
    cfg = report_main._load_evidence_config({})
    assert cfg.enabled is True and cfg.news_max_items == 10


async def test_prefetch_evidence_timeout_yields_empty_bundle(monkeypatch):
    def slow(sym, market, cfg, *, as_of=None):
        import time
        time.sleep(0.5)
        return {"never": True}
    monkeypatch.setattr(evidence, "fetch_evidence", slow)
    cfg = evidence.EvidenceConfig(timeout_seconds=0.05)
    ev = await report_main._prefetch_evidence("AXTI", "us", cfg, date(2026, 9, 9))
    assert ev["news_count"] == 0 and ev["errors"] == ["timeout"]


async def test_prefetch_evidence_returns_bundle(monkeypatch):
    bundle = evidence.empty_evidence(date(2026, 9, 9))
    bundle["news_count"] = 7
    monkeypatch.setattr(evidence, "fetch_evidence", lambda sym, market, cfg, *, as_of=None: bundle)
    ev = await report_main._prefetch_evidence("AXTI", "us", evidence.EvidenceConfig(), date(2026, 9, 9))
    assert ev["news_count"] == 7
