from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from report import renderer


HKT = ZoneInfo("Asia/Hong_Kong")


def _fake_data(ticker: str, group: str = "Leaders") -> dict:
    return {
        "ticker": ticker,
        "group": group,
        "exchange": "NASDAQ",
        "company_name": f"{ticker} Inc.",
        "sector": "Technology",
        "industry": "Software",
        "market_cap": 50_000_000_000,
        "last_price": 200.0,
        "prev_close": 195.0,
        "gap_pct": 2.56,
        "institutional_holdings_pct": 65.4,
        "eps_latest_q": 1.25,
        "eps_latest_q_yoy_pct": 18.5,
        "revenue_latest_q": 1_200_000_000,
        "revenue_latest_q_yoy_pct": 22.0,
        "annual_eps_yoy_5y": [10.0, 14.0, 18.0, 25.0, 30.0],
        "annual_revenue_yoy_5y": [12.0, 18.0, 20.0, 25.0, 28.0],
        "quarterly_eps_yoy_4q": [22.0, 24.0, 28.0, 31.0],
        "quarterly_eps_yoy_4q_labels": ["Jun'24", "Sep'24", "Dec'24", "Mar'25"],
        "quarterly_revenue_yoy_4q": [10.0, 13.0, 16.0, 18.0],
        "quarterly_revenue_yoy_4q_labels": ["Jun'24", "Sep'24", "Dec'24", "Mar'25"],
        "latest_earnings_date": "2026-04-30",
        "rs_percentile": 92,
        "yahoo_revenue_growth_yoy_pct": None,
        "yahoo_earnings_growth_yoy_pct": None,
    }


# --- HTML document tests -----------------------------------------------------

def test_render_html_document_is_self_contained():
    enriched = [_fake_data("AAPL")]
    html = renderer.render_html_document(
        market="us",
        date_iso="2026-05-07",
        enriched=enriched,
        prose_sections=["### 公司速览\n\nApple makes iPhones."],
        truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert html.startswith("<!doctype html>")
    assert "<style>" in html
    assert "AAPL" in html
    assert "Apple makes iPhones" in html or "Apple" in html
    # No external resources (`http(s)://` only appears in SVG namespace
    # `http://www.w3.org/2000/svg`, which is namespace-only and not a fetch).
    assert 'src="' not in html
    assert "<link" not in html
    assert "@import" not in html
    assert "url(http" not in html


def test_render_html_renders_per_ticker_anchors_and_index():
    enriched = [_fake_data("AAPL"), _fake_data("NVDA", "EarningsGap")]
    html = renderer.render_html_document(
        market="us",
        date_iso="2026-05-07",
        enriched=enriched,
        prose_sections=["### 公司速览\nx\n", "### 公司速览\ny\n"],
        truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert 'id="t-AAPL"' in html
    assert 'id="t-NVDA"' in html
    assert 'href="#t-AAPL"' in html
    assert 'href="#t-NVDA"' in html
    assert "EarningsGap" in html


def test_render_html_includes_svg_bar_chart():
    enriched = [_fake_data("AAPL")]
    html = renderer.render_html_document(
        market="us",
        date_iso="2026-05-07",
        enriched=enriched,
        prose_sections=["### 公司速览\nbody"],
        truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    # Both YoY rows present as inline SVG.
    assert html.count("<svg") >= 2
    assert "EPS YoY" in html
    assert "Rev. YoY" in html


def test_render_html_marks_positive_negative_yoy():
    """Positive YoY gets `positive` class; negative gets `negative` class
    on the latest-quarter YoY badge. Use values below the CANSLIM "hot"
    thresholds (Rev>20% / EPS>25%) so the `hot` modifier doesn't trigger
    and we can observe the base positive/negative class in isolation."""
    d = _fake_data("XYZ")
    d["eps_latest_q_yoy_pct"] = -25.0
    d["revenue_latest_q_yoy_pct"] = 15.0  # under 20% → positive without hot
    html = renderer.render_html_document(
        market="us",
        date_iso="2026-05-07",
        enriched=[d],
        prose_sections=["### 公司速览\nbody"],
        truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert 'class="yoy negative"' in html
    assert 'class="yoy positive"' in html


def test_render_html_failure_prose_renders_as_failure_block():
    html = renderer.render_html_document(
        market="us",
        date_iso="2026-05-07",
        enriched=[_fake_data("XYZ")],
        prose_sections=["## XYZ — ?  (NASDAQ · Leaders)\n\n[配置错误: HTTP 401: bad key]\n"],
        truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert 'class="failure"' in html
    assert "配置错误" in html


def test_render_html_truncated_section_appears():
    html = renderer.render_html_document(
        market="us",
        date_iso="2026-05-07",
        enriched=[_fake_data("AAPL")],
        prose_sections=["### 公司速览\nbody"],
        truncated=[("WMT", "RS"), ("PLTR", "TopGainers")],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert "Truncated" in html
    assert "WMT" in html
    assert "PLTR" in html


# --- Markdown document tests -------------------------------------------------

def test_render_markdown_document_includes_data_tables():
    md = renderer.render_markdown_document(
        market="us",
        date_iso="2026-05-07",
        enriched=[_fake_data("AAPL")],
        prose_sections=["### 公司速览\nApple"],
        truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert "Daily Scan — 2026-05-07 (US)" in md
    assert "AAPL" in md
    assert "Market Cap" in md
    assert "Latest Quarter" in md
    assert "FY−1" in md
    assert "公司速览" in md


# --- write_report_files (new API) -------------------------------------------

def test_write_report_files_writes_html_only(tmp_path: Path):
    html_path = renderer.write_report_files(
        out_dir=tmp_path,
        date_stem="2026_05_07",
        market="us",
        enriched=[_fake_data("AAPL")],
        prose_sections=["### 公司速览\nbody"],
        truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
        date_iso="2026-05-07",
    )
    assert html_path == tmp_path / "2026_05_07_us.html"
    assert html_path.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert not (tmp_path / "2026_05_07_us.md").exists()
    assert sorted(f.name for f in tmp_path.iterdir()) == ["2026_05_07_us.html"]


def test_write_report_files_handles_chinese(tmp_path: Path):
    d = _fake_data("0700.HK", "Leaders")
    d["company_name"] = "腾讯控股"
    html_path = renderer.write_report_files(
        out_dir=tmp_path,
        date_stem="2026_05_07",
        market="hk",
        enriched=[d],
        prose_sections=["### 公司速览\n\n中国互联网龙头。\n\n### 综合判断\n\n看涨。"],
        truncated=[],
        generated_at=datetime(2026, 5, 7, 20, 5, 0, tzinfo=HKT),
        date_iso="2026-05-07",
    )
    html_text = html_path.read_text(encoding="utf-8")
    assert "腾讯控股" in html_text
    assert "公司速览" in html_text


# --- Bar chart helper --------------------------------------------------------

def test_bar_chart_handles_all_null():
    svg = renderer._bar_chart_svg([None] * 5, ["FY-5", "FY-4", "FY-3", "FY-2", "FY-1"])
    assert svg.startswith("<svg")
    assert svg.count("<text") >= 5  # period labels still rendered


def test_line_chart_handles_mix_of_signs():
    """Line chart: black ink on white, red emphasis only on negative labels."""
    svg = renderer._line_chart_svg([10.0, -5.0, None, 25.0, -15.0],
                                    ["FY-5", "FY-4", "FY-3", "FY-2", "FY-1"])
    assert "<circle" in svg                    # at least one filled dot
    assert "#0A0A0A" in svg                    # ink color (positive label)
    assert "#A02828" in svg                    # negative-label color
    assert svg.count("<line") >= 2             # baseline + connecting segments


# --- Fresh-IPO no-data banner ------------------------------------------------

def _fake_ipo_no_data() -> dict:
    d = _fake_data("REA", group="IPO")
    # Strip every fundamental field — fresh IPO with no EDGAR + no yfinance.
    for k in ("eps_latest_q", "revenue_latest_q",
              "eps_latest_q_yoy_pct", "revenue_latest_q_yoy_pct",
              "yahoo_revenue_growth_yoy_pct", "yahoo_earnings_growth_yoy_pct"):
        d[k] = None
    d["annual_eps_yoy_5y"] = [None] * 5
    d["annual_revenue_yoy_5y"] = [None] * 5
    d["quarterly_eps_yoy_4q"] = [None] * 4
    d["quarterly_revenue_yoy_4q"] = [None] * 4
    d["ipo_date"] = "2026-05-05"
    return d


def test_has_no_fundamentals_true_when_all_empty():
    assert renderer._has_no_fundamentals(_fake_ipo_no_data()) is True


def test_has_no_fundamentals_false_when_any_field_present():
    d = _fake_ipo_no_data()
    d["revenue_latest_q"] = 1_000_000
    assert renderer._has_no_fundamentals(d) is False


def test_html_ipo_block_renders_banner_and_skips_fundamentals_tables():
    html = renderer.render_html_document(
        market="us",
        date_iso="2026-05-08",
        enriched=[_fake_ipo_no_data()],
        prose_sections=["### 公司速览\n\nNew IPO."],
        truncated=[],
        generated_at=datetime(2026, 5, 8, 10, 5, 0, tzinfo=HKT),
    )
    assert 'class="ipo-no-data"' in html
    assert "2026 年" in html
    assert "首日交易 2026-05-05" in html
    # Fundamentals sections must be suppressed
    assert "5-Year Annual Earnings Increases" not in html
    assert "Past 4 Quarters" not in html


def test_md_ipo_block_renders_banner_and_skips_fundamentals_tables():
    md = renderer.render_markdown_document(
        market="us",
        date_iso="2026-05-08",
        enriched=[_fake_ipo_no_data()],
        prose_sections=["### 公司速览\n\nNew IPO."],
        truncated=[],
        generated_at=datetime(2026, 5, 8, 10, 5, 0, tzinfo=HKT),
    )
    assert "**2026 年**上市的 IPO 公司" in md
    assert "首日交易 2026-05-05" in md
    # No 5y annual table or Latest Quarter line
    assert "FY−5" not in md
    assert "Latest Quarter" not in md


# --- CANSLIM enrichments (ROE, hot YoY, model-attribution footer) ------------

def test_html_quarterly_marks_hot_when_revenue_yoy_above_20():
    """Rev YoY > 20% earns the `hot` modifier on both the value and pill."""
    d = _fake_data("HOT1")
    d["revenue_latest_q_yoy_pct"] = 35.0
    d["eps_latest_q_yoy_pct"] = 5.0  # under 25% threshold — not hot
    html = renderer.render_html_document(
        market="us", date_iso="2026-05-07", enriched=[d],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    # Revenue side: pill carries `positive hot`; value carries `metric-value hot`.
    assert 'class="yoy positive hot"' in html
    assert 'class="metric-value hot"' in html
    # EPS side stays cold — the pill class is just `positive`, no hot.
    # Two pills exist; verify exactly one is hot.
    assert html.count(" hot") >= 2  # one pill + one metric-value
    assert html.count("positive hot") == 1


def test_html_quarterly_no_hot_at_threshold_boundary():
    """Threshold is strict `>` — a value exactly at the limit must not be hot."""
    d = _fake_data("EDGE")
    d["revenue_latest_q_yoy_pct"] = 20.0   # exactly at threshold, not above
    d["eps_latest_q_yoy_pct"] = 25.0       # exactly at threshold, not above
    html = renderer.render_html_document(
        market="us", date_iso="2026-05-07", enriched=[d],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert "positive hot" not in html


def test_html_snapshot_marks_roe_hot_above_17():
    d = _fake_data("ROE1")
    d["roe_pct"] = 25.0
    html = renderer.render_html_document(
        market="us", date_iso="2026-05-07", enriched=[d],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert "ROE</td>" in html
    assert ">25.0%</td>" in html
    assert "snap-value hot" in html


def test_html_snapshot_no_hot_when_roe_low_or_missing():
    d = _fake_data("ROE2")
    d["roe_pct"] = 10.0  # below threshold
    html = renderer.render_html_document(
        market="us", date_iso="2026-05-07", enriched=[d],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert "snap-value hot" not in html

    d2 = _fake_data("ROE3")
    d2["roe_pct"] = None
    html2 = renderer.render_html_document(
        market="us", date_iso="2026-05-07", enriched=[d2],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert "ROE</td>" in html2
    assert ">—</td>" in html2  # missing ROE shown as em dash


def test_md_latest_quarter_bolds_when_above_thresholds():
    d = _fake_data("MD1")
    d["revenue_latest_q_yoy_pct"] = 28.0   # > 20%
    d["eps_latest_q_yoy_pct"] = 30.0       # > 25%
    md = renderer.render_markdown_document(
        market="us", date_iso="2026-05-07", enriched=[d],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    # Both segments wrapped in **...**
    assert "**EPS $1.25 (YoY +30.0%)**" in md
    assert "**Revenue $1.20B (YoY +28.0%)**" in md


def test_md_latest_quarter_does_not_bold_when_under_threshold():
    d = _fake_data("MD2")
    d["revenue_latest_q_yoy_pct"] = 10.0
    d["eps_latest_q_yoy_pct"] = 5.0
    md = renderer.render_markdown_document(
        market="us", date_iso="2026-05-07", enriched=[d],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    # The Latest Quarter prefix is bold but the value segments are not.
    assert "**EPS $1.25" not in md
    assert "**Revenue $1.2B" not in md


def test_md_snapshot_includes_roe_column_and_bolds_above_17():
    d = _fake_data("ROE_MD")
    d["roe_pct"] = 22.5
    md = renderer.render_markdown_document(
        market="us", date_iso="2026-05-07", enriched=[d],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert "| ROE |" in md
    assert "**22.5%**" in md  # bold because > 17%


def test_md_quarterly_trend_bolds_only_rightmost_when_hot():
    """4Q YoY table: only the LAST (most-recent) cell gets bolded, even if
    older cells exceed the threshold. Older quarters are historical context,
    not the CANSLIM 'C' signal."""
    d = _fake_data("QTRD")
    # Older 3 quarters all above EPS hot threshold; only the latest matters.
    d["quarterly_eps_yoy_4q"] = [40.0, 35.0, 30.0, 28.0]
    d["quarterly_revenue_yoy_4q"] = [10.0, 12.0, 15.0, 25.0]
    md = renderer.render_markdown_document(
        market="us", date_iso="2026-05-07", enriched=[d],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    # EPS 28.0 (latest) > 25% → bolded
    assert "**+28.0%**" in md
    # Revenue 25.0 (latest) > 20% → bolded
    assert "**+25.0%**" in md
    # Older EPS values like +40.0% must NOT be bolded
    assert "**+40.0%**" not in md
    assert "**+35.0%**" not in md


def test_md_footer_shows_model_label():
    d = _fake_data("FOOT")
    md = renderer.render_markdown_document(
        market="us", date_iso="2026-05-07", enriched=[d],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
        model_label="claude-sonnet-4-6 (Anthropic)",
    )
    assert "*Generated by claude-sonnet-4-6 (Anthropic) · 2026-05-07*" in md


def test_html_footer_shows_model_label():
    d = _fake_data("FOOT")
    html = renderer.render_html_document(
        market="us", date_iso="2026-05-07", enriched=[d],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
        model_label="deepseek-v4-flash (DeepSeek)",
    )
    assert 'class="report-footer"' in html
    assert "deepseek-v4-flash (DeepSeek)" in html


def test_footer_omitted_when_model_label_absent():
    """Backward-compat: callers that don't pass model_label get no footer."""
    d = _fake_data("FOOT")
    md = renderer.render_markdown_document(
        market="us", date_iso="2026-05-07", enriched=[d],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert "Generated by" not in md
    html = renderer.render_html_document(
        market="us", date_iso="2026-05-07", enriched=[d],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    # CSS rule `.report-footer` is in INLINE_CSS unconditionally; we want
    # to confirm the <footer class="report-footer"> element is NOT emitted.
    assert '<footer class="report-footer"' not in html


# --- Legacy shims (keep older callers working) -------------------------------

def test_legacy_render_markdown_still_works():
    md = renderer.render_markdown(
        market="us",
        date_iso="2026-05-07",
        analyzed_count=1,
        truncated=[("WMT", "RS")],
        sections=["## AAPL\nbody"],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert "Scan Report" in md
    assert "AAPL" in md
    assert "WMT (RS)" in md


def test_legacy_markdown_to_html_still_works():
    html = renderer.markdown_to_html("# x", page_title="Test")
    assert html.startswith("<!doctype html>")
    assert "<h1>x</h1>" in html


def test_format_eps_dual_shows_both_when_materially_different():
    val_str, yoy_str = renderer._format_eps_dual(
        gaap=0.71, gaap_yoy=-13.41, adj=1.61, adj_yoy=-5.29,
    )
    assert val_str == "$0.71 GAAP / $1.61 Adj"
    assert yoy_str == "YoY GAAP -13.4% / Adj -5.3%"


def test_format_eps_dual_collapses_when_close():
    val_str, yoy_str = renderer._format_eps_dual(
        gaap=0.42, gaap_yoy=90.91, adj=0.42, adj_yoy=90.91,
    )
    assert val_str == "$0.42"
    assert yoy_str == "YoY +90.9%"


def test_format_eps_dual_only_gaap():
    val_str, yoy_str = renderer._format_eps_dual(
        gaap=0.42, gaap_yoy=10.0, adj=None, adj_yoy=None,
    )
    assert val_str == "$0.42"
    assert yoy_str == "YoY +10.0%"


def test_format_eps_dual_only_adj():
    val_str, yoy_str = renderer._format_eps_dual(
        gaap=None, gaap_yoy=None, adj=1.61, adj_yoy=-5.29,
    )
    assert val_str == "$1.61 Adj"
    assert yoy_str == "YoY -5.3% (Adj)"


def test_format_eps_dual_neither_returns_em_dash():
    val_str, yoy_str = renderer._format_eps_dual(
        gaap=None, gaap_yoy=None, adj=None, adj_yoy=None,
    )
    assert val_str == "—"
    assert yoy_str == "YoY —"


def test_format_eps_dual_near_zero_gaap_does_not_blow_up():
    val_str, yoy_str = renderer._format_eps_dual(
        gaap=-0.07, gaap_yoy=56.3, adj=0.27, adj_yoy=22.73,
    )
    assert val_str == "$-0.07 GAAP / $0.27 Adj"
    assert yoy_str == "YoY GAAP +56.3% / Adj +22.7%"


def test_format_eps_dual_dual_with_one_yoy_missing():
    val_str, yoy_str = renderer._format_eps_dual(
        gaap=0.71, gaap_yoy=-13.41, adj=1.61, adj_yoy=None,
    )
    assert val_str == "$0.71 GAAP / $1.61 Adj"
    assert yoy_str == "YoY GAAP -13.4% / Adj —"


def test_html_quarterly_shows_dual_eps_when_materially_different():
    d = _fake_data("AKAM")
    d["eps_latest_q"] = 0.71
    d["eps_latest_q_yoy_pct"] = -13.41
    d["eps_latest_q_adj"] = 1.61
    d["eps_latest_q_adj_yoy_pct"] = -5.29
    html = renderer.render_html_document(
        market="us", date_iso="2026-05-07", enriched=[d],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert "$0.71 GAAP / $1.61 Adj" in html
    assert "YoY GAAP -13.4% / Adj -5.3%" in html


def test_html_quarterly_falls_back_to_single_eps_when_close():
    d = _fake_data("INOD")
    d["eps_latest_q"] = 0.42
    d["eps_latest_q_yoy_pct"] = 90.91
    d["eps_latest_q_adj"] = 0.42
    d["eps_latest_q_adj_yoy_pct"] = 90.91
    html = renderer.render_html_document(
        market="us", date_iso="2026-05-07", enriched=[d],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert ">$0.42<" in html
    # Single-value mode: no GAAP/Adj suffix in the quarterly section.
    assert "GAAP /" not in html
    assert "/ $0.42 Adj" not in html


def test_md_latest_quarter_shows_dual_eps_when_materially_different():
    d = _fake_data("AKAM")
    d["eps_latest_q"] = 0.71
    d["eps_latest_q_yoy_pct"] = -13.41
    d["eps_latest_q_adj"] = 1.61
    d["eps_latest_q_adj_yoy_pct"] = -5.29
    md = renderer.render_markdown_document(
        market="us", date_iso="2026-05-07", enriched=[d],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert "EPS $0.71 GAAP / $1.61 Adj (YoY GAAP -13.4% / Adj -5.3%)" in md


def test_md_latest_quarter_falls_back_to_single_eps_when_close():
    d = _fake_data("INOD")
    d["eps_latest_q"] = 0.42
    d["eps_latest_q_yoy_pct"] = 90.91
    d["eps_latest_q_adj"] = 0.42
    d["eps_latest_q_adj_yoy_pct"] = 90.91
    md = renderer.render_markdown_document(
        market="us", date_iso="2026-05-07", enriched=[d],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert "EPS $0.42 (YoY +90.9%)" in md


def test_html_footnote_appears_when_any_ticker_shows_dual_eps():
    """Footnote appears once when at least one ticker uses the dual form."""
    d_dual = _fake_data("AKAM")
    d_dual["eps_latest_q"] = 0.71
    d_dual["eps_latest_q_yoy_pct"] = -13.41
    d_dual["eps_latest_q_adj"] = 1.61
    d_dual["eps_latest_q_adj_yoy_pct"] = -5.29
    d_single = _fake_data("INOD")
    d_single["eps_latest_q"] = 0.42
    d_single["eps_latest_q_yoy_pct"] = 90.91
    html = renderer.render_html_document(
        market="us", date_iso="2026-05-07", enriched=[d_dual, d_single],
        prose_sections=["### 公司速览\nbody", "### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert "EPS shows GAAP / Adjusted when they differ" in html
    assert html.count("EPS shows GAAP / Adjusted when they differ") == 1


def test_html_footnote_absent_when_no_ticker_shows_dual_eps():
    d = _fake_data("INOD")
    d["eps_latest_q"] = 0.42
    d["eps_latest_q_yoy_pct"] = 90.91
    html = renderer.render_html_document(
        market="us", date_iso="2026-05-07", enriched=[d],
        prose_sections=["### 公司速览\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 7, 10, 5, 0, tzinfo=HKT),
    )
    assert "EPS shows GAAP / Adjusted when they differ" not in html


# --- Cover page --------------------------------------------------------------

def test_html_cover_hero_leads_with_methodology_not_date():
    """Hero is the methodology statement (single line); the count was
    removed because it read as visually clumsy. Date is demoted to the
    eyebrow strip and colophon timestamp. Person names and CANSLIM stay
    English; framework copy is Chinese."""
    enriched = [
        _fake_data("AKAM", "HighVolume"),
        _fake_data("FLEX", "HighVolume"),
        _fake_data("DDOG", "Leaders"),
    ]
    html = renderer.render_html_document(
        market="us", date_iso="2026-05-09",
        enriched=enriched,
        prose_sections=["### x\nbody"] * 3,
        truncated=[("WMT", "RS")],
        generated_at=datetime(2026, 5, 9, 16, 30, 0, tzinfo=HKT),
        model_label="Anthropic Claude Sonnet 4.6",
    )
    # Eyebrow: label · 美股 + No.N + dotted-date — no bottom border
    # ("页面上面多了一条横线" was removed at user request).
    assert 'class="cover"' in html
    assert "每日股票精选 · 美股" in html
    assert "第 129 期" in html              # day-of-year for May 9 in 2026
    assert "2026.05.09 · 周六" in html

    # Hero is the methodology statement — count removed entirely.
    assert "新晋强势榜" in html
    assert "3 支股票" not in html  # large count headline removed

    # Strap names both methodology authors with their respective
    # descriptors (Kullamägi: 1-6 个月强势 + 抛物线), the model, and CANSLIM.
    assert "Oliver Kell" in html
    assert "Kristjan Kullamägi" in html
    assert "1-6 个月强势表现股票" in html
    assert "抛物线做空策略" in html
    assert "Anthropic Claude Sonnet 4.6" in html
    assert "CANSLIM" in html

    # Ticker plate
    assert 'class="cover-plate-ticker"' in html
    assert 'href="#t-AKAM"' in html and 'href="#t-DDOG"' in html

    # Colophon
    assert "数据来源" in html
    assert "选股方法" in html
    assert "SEC EDGAR" in html
    assert "William O" in html  # third methodology row
    # Updated Kullamägi colophon line includes the new descriptor.
    assert "1-6 个月的强势增长股票 Leaders" in html

    # Legal disclaimer — small print bottom-right
    assert 'class="cover-disclaimer"' in html
    assert "不构成任何投资建议" in html
    assert "股市有风险" in html


def test_html_cover_groups_tickers_by_category_in_first_seen_order():
    """Per-category breakdown must follow ranker's first-seen order, not
    alphabetical or Python dict insertion-order quirks."""
    enriched = [
        _fake_data("CORZ", "EarningsGap"),
        _fake_data("AKAM", "HighVolume"),
        _fake_data("FLEX", "HighVolume"),
        _fake_data("DDOG", "Leaders"),
        _fake_data("PENG", "NewHigh52W"),
    ]
    html = renderer.render_html_document(
        market="us", date_iso="2026-05-09",
        enriched=enriched,
        prose_sections=["### x\nbody"] * 5,
        truncated=[],
        generated_at=datetime(2026, 5, 9, 16, 30, 0, tzinfo=HKT),
        model_label="Anthropic Claude Sonnet 4.6",
    )
    cover_start = html.find('class="cover-breakdown"')
    cover_end = html.find('class="cover-colophon"', cover_start)
    breakdown = html[cover_start:cover_end]
    pos_eg = breakdown.find("Earnings Gap")
    pos_hv = breakdown.find("High Volume")
    pos_ld = breakdown.find("Leaders")
    pos_nh = breakdown.find("52-Week High")
    assert pos_eg != -1 and pos_hv != -1 and pos_ld != -1 and pos_nh != -1
    assert pos_eg < pos_hv < pos_ld < pos_nh
    # Tickers within a category list separated by ' · ' (middle dot)
    assert "AKAM · FLEX" in breakdown
    # Category counts use Chinese measure word, no zero-padding
    assert "2 个" in breakdown


def test_html_cover_handles_empty_enriched():
    """Cover still renders structurally with the no-stocks fallback copy.
    Headline is methodology-statement (always shown); plate area carries
    the empty messaging."""
    html = renderer.render_html_document(
        market="us", date_iso="2026-05-09",
        enriched=[], prose_sections=[], truncated=[],
        generated_at=datetime(2026, 5, 9, 16, 30, 0, tzinfo=HKT),
        model_label="Anthropic Claude Sonnet 4.6",
    )
    assert 'class="cover"' in html
    assert "新晋强势榜" in html
    assert "本期暂无符合条件的股票" in html


def test_html_cover_market_label_handles_hk():
    html = renderer.render_html_document(
        market="hk", date_iso="2026-05-09",
        enriched=[_fake_data("0700", "HKLeaders")],
        prose_sections=["### x\nbody"], truncated=[],
        generated_at=datetime(2026, 5, 9, 20, 30, 0, tzinfo=HKT),
        model_label="DeepSeek V4 Pro",
    )
    assert "每日股票精选 · 港股" in html
    # HK group label still reads as "Leaders" in English
    assert "Leaders" in html
