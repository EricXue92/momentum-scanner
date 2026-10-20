"""`finviz_screener.Screener` — the redesigned Finviz screener page.

Regression: 2026-10-20 Finviz moved the screener to `/screener/stocks` with
the rows embedded as JSON; finviz 2.0.0 parsed no rows, raised NoResults,
and every US EOD screen came back empty with no error. These tests pin the
JSON parsing, paging (20 rows/page, 1000-row cap), the NoResults contract
that a page without a rows block is an error, not an empty result, and
that a filter code the page does not define (silently ignored by Finviz —
`ta_perf_52w150o` returned the whole market) raises instead.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
from finviz.helper_functions.error_handling import NoResults

import finviz_screener as fs
import main
from cleanup import cleanup_old_outputs
from notify import notify_finviz_broken


_FILTER_DEFS = {
    "filters": [
        {"key": "sh_price", "options": [{"value": "", "label": "Any"}, {"value": "o20", "label": "Over $20"}]},
        {"key": "ta_perf", "options": [{"value": "52w100o"}, {"value": "52w200o"}, {"value": "dup"}]},
        {"key": "ta_sma50", "options": [{"value": "pa"}]},
    ]
}


def _page_html(
    tickers: list[str], total: int, first_row: int = 0, defs: dict | None = None
) -> str:
    rows = [
        {
            "text": {"ticker": t, "company": f"{t} Inc", "type": "stock"},
            "numbers": {"marketCap": 3050.5, "price": 25.0, "order": first_row + i},
        }
        for i, t in enumerate(tickers)
    ]
    blob = {
        "url": "/screener/stocks?v=111",
        "screenerType": "stocks",
        "selectedColumns": ["row", "ticker", "marketCap"],
        "data": {"rows": rows, "totalCount": total, "firstRow": first_row, "pageSize": 20},
        **({"filters": defs} if defs else {}),
    }
    return (
        '<html><head><script type="text/javascript">var x = 1;</script>'
        '<script type="application/json" data-ssr-props="ssr-island-1">'
        f"{json.dumps(blob)}</script></head><body></body></html>"
    )


class _Resp:
    def __init__(self, text: str = "", status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            resp = requests.Response()
            resp.status_code = self.status_code
            raise requests.exceptions.HTTPError(str(self.status_code), response=resp)


def _fake_site(universe: list[str], calls: list[dict], cap: int = fs.PAGE_CAP):
    """Finviz-like: 20 rows/page, 1-based `r`, honours `o`, 403 past `cap`."""

    def fetch(params: dict) -> _Resp:
        calls.append(dict(params))
        r = int(params.get("r", 1))
        if r > cap:
            return _Resp(status_code=403)
        ordered = sorted(universe, reverse=params.get("o") == "-ticker")
        return _Resp(_page_html(ordered[r - 1 : r - 1 + 20], len(universe), r - 1))

    return fetch


def test_parse_page_rows_total_and_cap():
    rows, total = fs.parse_screener_page(_page_html(["AMN", "ANF"], 24))
    assert total == 24
    assert [r["Ticker"] for r in rows] == ["AMN", "ANF"]
    # Market Cap is in millions on the page; main.parse_number gives USD.
    assert main.parse_number(rows[0]["Market Cap"]) == pytest.approx(3_050_500_000)


def test_page_without_rows_block_is_an_error_not_empty():
    with pytest.raises(fs.FinvizLayoutError):
        fs.parse_screener_page("<html><table><tr><td>AAPL</td></tr></table></html>")


def test_screener_pages_through_all_rows(monkeypatch):
    calls: list[dict] = []
    universe = [f"T{i:03d}" for i in range(45)]
    monkeypatch.setattr(fs, "_fetch_page", _fake_site(universe, calls))
    s = fs.Screener(filters=["sh_price_o20", "ta_sma50_pa"], signal="ta_topgainers")
    assert [r["Ticker"] for r in s.data] == universe
    assert s._total_rows == 45
    assert [c.get("r") for c in calls] == [None, 21, 41]
    assert calls[0]["f"] == "sh_price_o20,ta_sma50_pa"
    assert calls[0]["s"] == "ta_topgainers"
    assert calls[0]["v"] == "111"


def test_screener_stops_at_page_cap(monkeypatch):
    calls: list[dict] = []
    universe = [f"T{i:04d}" for i in range(1500)]
    monkeypatch.setattr(fs, "_fetch_page", _fake_site(universe, calls))
    s = fs.Screener(filters=["f1"])
    assert len(s.data) == 1000
    assert s._total_rows == 1500


def test_run_screener_unions_tail_past_page_cap(monkeypatch):
    """End to end with run_screener's descending-order tail query."""
    calls: list[dict] = []
    universe = [f"T{i:04d}" for i in range(1500)]
    monkeypatch.setattr(fs, "_fetch_page", _fake_site(universe, calls))
    got = main.run_screener(["f1"])
    assert sorted(got) == universe


def test_zero_rows_raise_noresults(monkeypatch):
    monkeypatch.setattr(fs, "_fetch_page", lambda params: _Resp(_page_html([], 0)))
    with pytest.raises(NoResults):
        fs.Screener(filters=["f1"])
    assert main.run_screener(["f1"]) == []


def test_first_page_403_raises(monkeypatch):
    monkeypatch.setattr(fs, "_fetch_page", lambda params: _Resp(status_code=403))
    with pytest.raises(requests.exceptions.HTTPError):
        fs.Screener(filters=["f1"])


def test_main_uses_the_redesign_screener():
    assert main.Screener is fs.Screener


def test_unknown_filter_code_raises(monkeypatch):
    monkeypatch.setattr(
        fs, "_fetch_page", lambda params: _Resp(_page_html(["A"], 5607, defs=_FILTER_DEFS))
    )
    with pytest.raises(fs.FinvizFilterError) as exc:
        fs.Screener(filters=["ind_stocksonly", "sh_price_o20", "ta_perf_52w150o"])
    assert exc.value.codes == ["ta_perf_52w150o"]


def test_known_and_implied_filter_codes_pass():
    html = _page_html(["A"], 1, defs=_FILTER_DEFS)
    codes = ["ind_stocksonly", "sh_price_o20", "ta_perf_52w100o", "ta_perf_dup", "ta_sma50_pa"]
    assert fs.unknown_filters(html, codes) == []
    # "" is the "Any" option, not a value a code can select.
    assert fs.unknown_filters(html, ["sh_price_"]) == ["sh_price_"]


def test_page_without_filter_definitions_skips_the_check():
    assert fs.unknown_filters(_page_html(["A"], 1), ["ta_perf_52w150o"]) == []


# --- broken-scraper guard (main._alert_if_finviz_broken / notify_finviz_broken) ---

_CFG = {"notify": {"enabled": True, "ntfy_topic": "t"}}
_TODAY = "2026_10_20"


def _tally(screens: int, rows: int, errors: int = 0, unknown: set[str] = frozenset()) -> None:
    main._finviz_tally.reset()
    main._finviz_tally.screens = screens
    main._finviz_tally.rows = rows
    main._finviz_tally.errors = errors
    main._finviz_tally.unknown_filters = set(unknown)


def _alert(tmp_path: Path):
    with patch.object(main, "notify_finviz_broken") as notify:
        main._alert_if_finviz_broken("us-eod", _CFG, tmp_path, _TODAY)
    return notify


def test_all_screens_empty_alerts(tmp_path: Path):
    _tally(13, 0, 2)
    notify = _alert(tmp_path)
    notify.assert_called_once()
    assert "all 13 Finviz screens returned 0 rows (2 raised)" in notify.call_args.args[1]


def test_unknown_filter_alerts_even_with_rows(tmp_path: Path):
    _tally(13, 120, 1, {"ta_perf_52w150o"})
    notify = _alert(tmp_path)
    notify.assert_called_once()
    detail = notify.call_args.args[1]
    assert "ta_perf_52w150o" in detail and "0 rows" not in detail


def test_some_rows_do_not_alert(tmp_path: Path):
    _tally(13, 1)
    _alert(tmp_path).assert_not_called()


def test_too_few_screens_do_not_alert(tmp_path: Path):
    _tally(main._FINVIZ_EMPTY_MIN_SCREENS - 1, 0)
    _alert(tmp_path).assert_not_called()


def test_run_screener_counts_screens_rows_errors_and_unknown_codes(monkeypatch):
    main._finviz_tally.reset()
    monkeypatch.setattr(fs, "_fetch_page", lambda params: _Resp(_page_html(["A", "B"], 2)))
    main.run_screener(["f1"])
    monkeypatch.setattr(fs, "_fetch_page", lambda params: _Resp("<html></html>"))
    with pytest.raises(fs.FinvizLayoutError):
        main.run_screener(["f1"])
    monkeypatch.setattr(
        fs, "_fetch_page", lambda params: _Resp(_page_html(["A"], 9, defs=_FILTER_DEFS))
    )
    with pytest.raises(fs.FinvizFilterError):
        main.run_screener(["ta_perf_52w150o"])
    t = main._finviz_tally
    assert (t.screens, t.rows, t.errors) == (3, 2, 2)
    assert t.unknown_filters == {"ta_perf_52w150o"}


def test_notify_once_per_day(tmp_path: Path):
    with patch("notify._ntfy_post", return_value=True) as post:
        notify_finviz_broken("us-eod", "boom", _CFG, tmp_path, _TODAY)
        notify_finviz_broken("us-eod", "boom", _CFG, tmp_path, _TODAY)
    assert post.call_count == 1
    assert post.call_args.kwargs["priority"] == "high"
    assert (tmp_path / "state" / f"finviz_broken_alerted_{_TODAY}.txt").exists()


def test_failed_post_leaves_no_marker(tmp_path: Path):
    with patch("notify._ntfy_post", return_value=False):
        notify_finviz_broken("us-eod", "boom", _CFG, tmp_path, _TODAY)
    assert not (tmp_path / "state" / f"finviz_broken_alerted_{_TODAY}.txt").exists()


def test_marker_ages_out(tmp_path: Path):
    from datetime import date

    marker = tmp_path / "state" / "finviz_broken_alerted_2026_10_10.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("us-eod\n")
    cleanup_old_outputs(tmp_path, date(2026, 10, 20))
    assert not marker.exists()
