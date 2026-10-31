"""Tests for scripts/build_etf_rs_page.py — the GitHub Pages build of the ETF
RS trend page. yfinance is never hit: the kline fetch layer is monkeypatched."""

import random
import sys
from pathlib import Path

import pandas as pd
import pytest

import etf_holdings_rs
import etf_rs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_etf_rs_page  # noqa: E402
import etf_rs_leaders  # noqa: E402


@pytest.fixture(autouse=True)
def _no_leaders_prune_network(monkeypatch):
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", lambda tickers: None)


def _walk(seed: int, n: int = 120) -> pd.DataFrame:
    rng = random.Random(seed)
    closes, px = [], 100.0
    for _ in range(n):
        px *= 1 + rng.uniform(-0.03, 0.03)
        closes.append(px)
    return pd.DataFrame({
        "time_key": pd.bdate_range(end="2026-09-11", periods=n),
        "close": closes,
    })


def _config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


_CFG = '[etf_rs]\nbenchmark = "SPY"\n[etf_rs.tickers]\nAAA = "甲"\nBBB = "乙"\n'


def test_build_writes_index_html_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {
        "AAA": _walk(1), "BBB": _walk(2), "SPY": _walk(9),
    })
    site = tmp_path / "_site"
    assert build_etf_rs_page.build(site, _config(tmp_path, _CFG)) == 0
    assert [p.name for p in site.iterdir()] == ["index.html"]
    html = (site / "index.html").read_text(encoding="utf-8")
    assert 'id="etf-rs-data"' in html and '"AAA"' in html and "甲" in html


def test_build_fails_without_touching_the_site_when_fetch_returns_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {})
    site = tmp_path / "_site"
    assert build_etf_rs_page.build(site, _config(tmp_path, _CFG)) == 1
    assert not site.exists()


def test_build_fails_when_the_list_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")))
    site = tmp_path / "_site"
    cfg = _config(tmp_path, _CFG.replace("[etf_rs]\n", "[etf_rs]\nenabled = false\n"))
    assert build_etf_rs_page.build(site, cfg) == 1
    assert not site.exists()


def test_build_runs_the_holdings_step_and_embeds_its_line(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {
        "AAA": _walk(1), "BBB": _walk(2), "SPY": _walk(9)})
    monkeypatch.setattr(etf_holdings_rs, "compute_extras",
                        lambda *a, **k: {"AAA": "1M强: X > Y"})
    site = tmp_path / "site"
    assert build_etf_rs_page.build(site, _config(tmp_path, _CFG)) == 0
    assert '"strong":"1M强: X > Y"' in (site / "index.html").read_text(encoding="utf-8")
