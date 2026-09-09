import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from report import analyst
from report.llm import _extract_text


@pytest.fixture
def fake_data():
    return {
        "ticker": "AAPL",
        "group": "EarningsGap",
        "exchange": "NASDAQ",
        "company_name": "Apple Inc.",
        "market_cap": 3_000_000_000_000,
        "last_price": 200.0,
        "prev_close": 198.0,
        "gap_pct": 1.01,
        "pe_ratio": 30.0,
        "roe": 150.0,
        "institutional_holdings_pct": 60.0,
        "eps_latest_q": 1.1,
        "eps_latest_q_yoy_pct": 10.0,
        "revenue_latest_q": 1.1e9,
        "revenue_latest_q_yoy_pct": 10.0,
        "annual_eps_yoy_5y": [10.0, 12.0, 14.29, 16.67, 16.67],
        "annual_revenue_yoy_5y": [10.0, 12.0, 14.29, 16.67, 16.67],
        "quarterly_eps_yoy_4q": [10.0, 12.0, 14.0, 16.0],
        "quarterly_eps_yoy_4q_labels": ["Jun'24", "Sep'24", "Dec'24", "Mar'25"],
        "quarterly_revenue_yoy_4q": [10.0, 12.0, 14.0, 16.0],
        "quarterly_revenue_yoy_4q_labels": ["Jun'24", "Sep'24", "Dec'24", "Mar'25"],
        "latest_earnings_date": "2026-03-31",
        "rs_percentile": 95,
    }


def test_build_user_message_includes_ticker_and_data(fake_data):
    msg = analyst.build_user_message(fake_data)
    assert "AAPL" in msg
    assert "EarningsGap" in msg
    assert "Apple Inc." in msg
    # YoY arrays serialized
    assert "16.67" in msg or "16.7" in msg


def _fake_backend(analyze_mock: AsyncMock) -> MagicMock:
    """Build a minimal LLMBackend stand-in. Only `analyze` is exercised by the
    retry path — `name`/`aclose` are inert."""
    backend = MagicMock()
    backend.name = "fake"
    backend.analyze = analyze_mock
    return backend


async def test_analyze_ticker_success(fake_data):
    backend = _fake_backend(AsyncMock(return_value="## AAPL\n\n**Snapshot**..."))
    section = await analyst.analyze_ticker(
        backend=backend,
        system_prompt="<system prompt>",
        data=fake_data,
        semaphore=asyncio.Semaphore(1),
    )
    assert "## AAPL" in section
    backend.analyze.assert_awaited_once()


async def test_analyze_ticker_retries_on_5xx(fake_data):
    import anthropic
    failure = anthropic.APIStatusError(
        message="server error",
        response=MagicMock(status_code=503),
        body=None,
    )
    failure.status_code = 503
    backend = _fake_backend(
        AsyncMock(side_effect=[failure, "## AAPL\nfine"])
    )
    section = await analyst.analyze_ticker(
        backend=backend,
        system_prompt="<sp>",
        data=fake_data,
        semaphore=asyncio.Semaphore(1),
    )
    assert "## AAPL" in section
    assert backend.analyze.await_count == 2


async def test_analyze_ticker_returns_failure_section_after_retry_exhausted(fake_data):
    import anthropic
    failure = anthropic.APIConnectionError(request=MagicMock())
    backend = _fake_backend(AsyncMock(side_effect=[failure, failure]))
    section = await analyst.analyze_ticker(
        backend=backend,
        system_prompt="<sp>",
        data=fake_data,
        semaphore=asyncio.Semaphore(1),
    )
    assert "AAPL" in section
    assert "分析失败" in section


async def test_analyze_ticker_does_not_retry_on_4xx(fake_data):
    """4xx (e.g. 401 bad API key) must fail fast — single attempt, distinct placeholder."""
    import anthropic
    failure = anthropic.APIStatusError(
        message="invalid x-api-key",
        response=MagicMock(status_code=401),
        body=None,
    )
    failure.status_code = 401
    backend = _fake_backend(AsyncMock(side_effect=failure))
    section = await analyst.analyze_ticker(
        backend=backend,
        system_prompt="<sp>",
        data=fake_data,
        semaphore=asyncio.Semaphore(1),
    )
    assert "AAPL" in section
    assert "配置错误" in section
    assert "401" in section
    # Must NOT have retried
    assert backend.analyze.await_count == 1


async def test_analyze_ticker_retries_on_429_rate_limit(fake_data):
    """429 IS retriable (transient rate limit). Must attempt twice."""
    import anthropic
    failure = anthropic.APIStatusError(
        message="rate limited",
        response=MagicMock(status_code=429),
        body=None,
    )
    failure.status_code = 429
    backend = _fake_backend(
        AsyncMock(side_effect=[failure, "## AAPL\nfine"])
    )
    section = await analyst.analyze_ticker(
        backend=backend,
        system_prompt="<sp>",
        data=fake_data,
        semaphore=asyncio.Semaphore(1),
    )
    assert "## AAPL" in section
    assert backend.analyze.await_count == 2


def test_extract_text_handles_mixed_blocks():
    """Web-search responses include server-tool-use blocks; we want concatenated text only."""
    text_block = MagicMock(type="text", text="Hello.")
    tool_block = MagicMock(type="server_tool_use")
    other_text = MagicMock(type="text", text=" World.")
    response = MagicMock()
    response.content = [text_block, tool_block, other_text]
    # No "## " heading anywhere → return concatenated text as-is.
    assert _extract_text(response) == "Hello. World."


def test_extract_text_strips_preamble_before_first_h3():
    """LLM occasionally emits 'Let me research...' before the actual prose.
    _extract_text must drop that and start at the first H3 (the prose template
    now starts with ### 公司速览, not a ticker H2)."""
    block = MagicMock(
        type="text",
        text=(
            "I'll research ENTG's qualitative aspects with targeted searches.\n"
            "I have sufficient information. Let me generate the report.\n"
            "### 公司速览\n\n"
            "Entegris is...\n"
        ),
    )
    response = MagicMock()
    response.content = [block]
    out = _extract_text(response)
    assert out.startswith("### 公司速览")
    assert "I'll research" not in out
    assert "Let me generate" not in out


def test_extract_text_keeps_text_when_h3_is_first_line():
    block = MagicMock(type="text", text="### 公司速览\n\nbody")
    response = MagicMock()
    response.content = [block]
    assert _extract_text(response).startswith("### 公司速览")


def test_extract_text_back_compat_strips_preamble_before_h2():
    """Older LLM outputs may still emit a ticker H2 first; strip preamble before that too."""
    block = MagicMock(
        type="text",
        text=(
            "Let me think.\n"
            "## AAPL — Apple Inc.\n\n"
            "### 公司速览\nbody\n"
        ),
    )
    response = MagicMock()
    response.content = [block]
    out = _extract_text(response)
    assert out.startswith("## AAPL")


def test_build_user_message_is_compact_json(fake_data):
    msg = analyst.build_user_message(fake_data)
    assert '"ticker":"AAPL"' in msg          # no spaces after separators
    assert "Use the web_search tool sparingly" in msg
    assert "Pre-fetched evidence" not in msg


def test_build_user_message_with_evidence_block(fake_data):
    ev = {
        "as_of": "2026-09-09",
        "news": [{"date": "2026-09-08", "title": "Apple beats", "source": "Reuters", "summary": "s", "url": "u"}],
        "analyst": {"price_target": {"mean": 250.0}},
        "calendar": None, "filings": [], "form4_count": 0,
        "news_count": 1, "filings_count": 0, "errors": [],
    }
    msg = analyst.build_user_message(fake_data, evidence=ev)
    assert "Pre-fetched evidence" in msg
    assert '"title":"Apple beats"' in msg
    assert "Ground every qualitative section in the evidence above" in msg
    assert "Only call `web_search` if the tool is offered" in msg
    assert "Use the web_search tool sparingly" not in msg
    # evidence block comes after the structured block
    assert msg.index('"ticker":"AAPL"') < msg.index("Pre-fetched evidence")


async def test_analyze_ticker_passes_evidence_and_budget(fake_data):
    analyze = AsyncMock(return_value="### 公司速览\n\nok")
    backend = _fake_backend(analyze)
    ev = {"news": [], "news_count": 0, "filings": [], "filings_count": 0,
          "analyst": None, "calendar": None, "form4_count": 0, "errors": [], "as_of": "2026-09-09"}
    await analyst.analyze_ticker(
        backend=backend, system_prompt="sys", data=fake_data,
        semaphore=asyncio.Semaphore(1), evidence=ev, max_search_calls=0,
    )
    args, kwargs = analyze.await_args
    assert kwargs["max_search_calls"] == 0
    assert "Pre-fetched evidence" in args[1]


async def test_analyze_ticker_default_passes_none_budget(fake_data):
    analyze = AsyncMock(return_value="### 公司速览\n\nok")
    backend = _fake_backend(analyze)
    await analyst.analyze_ticker(
        backend=backend, system_prompt="sys", data=fake_data, semaphore=asyncio.Semaphore(1),
    )
    assert analyze.await_args.kwargs["max_search_calls"] is None
