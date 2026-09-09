"""Tests for the LLM backend abstraction (report/llm.py).

Covers the factory's branching logic and the DeepSeek tool-call loop —
the latter is the only non-trivial piece of orchestration in this module
(AnthropicBackend is a single SDK call so its tests live in test_report_analyst
via the shared retry path)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from report import llm


def test_build_backend_defaults_to_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    backend = llm.build_backend({})
    assert backend.name == "anthropic"
    assert isinstance(backend, llm.AnthropicBackend)


def test_build_backend_anthropic_explicit(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    backend = llm.build_backend({"backend": "anthropic", "anthropic": {"model": "x"}})
    assert backend.name == "anthropic"


def test_build_backend_anthropic_missing_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        llm.build_backend({"backend": "anthropic"})


def test_build_backend_deepseek(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dsk-test")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    backend = llm.build_backend({"backend": "deepseek"})
    assert backend.name == "deepseek"
    assert isinstance(backend, llm.DeepSeekBackend)


def test_build_backend_deepseek_missing_keys_raises(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dsk-test")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TAVILY_API_KEY"):
        llm.build_backend({"backend": "deepseek"})


def test_build_backend_unknown_raises():
    with pytest.raises(ValueError, match="unknown report backend"):
        llm.build_backend({"backend": "gpt-future"})


def test_build_backend_kimi_compat_provider(monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi-test")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    backend = llm.build_backend({"backend": "kimi"})
    assert isinstance(backend, llm.ToolLoopBackend)
    assert backend.name == "kimi"
    assert backend.model_label() == "kimi-k2-turbo-preview (Moonshot)"


def test_build_backend_compat_provider_config_overrides(monkeypatch):
    monkeypatch.setenv("ZHIPUAI_API_KEY", "sk-glm-test")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    backend = llm.build_backend(
        {"backend": "GLM", "glm": {"model": "glm-4.6-air", "max_search_calls": 3}}
    )
    assert backend.model_label() == "glm-4.6-air (Zhipu)"
    assert backend._max_search_calls == 3


def test_build_backend_compat_provider_missing_vendor_key_raises(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    with pytest.raises(RuntimeError, match="MINIMAX_API_KEY"):
        llm.build_backend({"backend": "minimax"})


def test_anthropic_backend_model_label(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    backend = llm.build_backend({"anthropic": {"model": "claude-sonnet-4-6"}})
    assert backend.model_label() == "claude-sonnet-4-6 (Anthropic)"


def test_deepseek_backend_model_label(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dsk-test")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    backend = llm.build_backend({"backend": "deepseek", "deepseek": {"model": "deepseek-v4-flash"}})
    assert backend.model_label() == "deepseek-v4-flash (DeepSeek)"


def _tool_use_response(query: str, tool_use_id: str = "tu_1") -> MagicMock:
    """Minimal SDK response shape with one tool_use block."""
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_use_id
    block.input = {"query": query}
    response = MagicMock()
    response.content = [block]
    response.stop_reason = "tool_use"
    return response


def _final_text_response(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    response.stop_reason = "end_turn"
    return response


async def test_deepseek_runs_tool_loop_and_returns_final_text(monkeypatch):
    """When the model emits a tool_use, the backend must call Tavily, append a
    tool_result, and continue. After the model returns end_turn, the final
    text is returned."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dsk")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly")
    backend = llm.build_backend({"backend": "deepseek"})

    # Stub the SDK: first call -> tool_use, second call -> final text.
    backend._client.messages.create = AsyncMock(
        side_effect=[
            _tool_use_response("AAPL latest catalyst 2026"),
            _final_text_response("### 公司速览\n\nApple..."),
        ]
    )
    # Stub Tavily so we don't hit the network.
    backend._tavily.search = AsyncMock(return_value="(stub search context)")
    backend._tavily.__aenter__ = AsyncMock(return_value=backend._tavily)
    backend._tavily.__aexit__ = AsyncMock(return_value=None)

    out = await backend.analyze("<sys>", "<user>")

    assert out.startswith("### 公司速览")
    # Second SDK call must include the assistant tool_use turn + a user
    # tool_result turn — verify by inspecting the messages kwarg.
    second_call_kwargs = backend._client.messages.create.await_args_list[1].kwargs
    msgs = second_call_kwargs["messages"]
    assert msgs[-2]["role"] == "assistant"
    assert msgs[-1]["role"] == "user"
    tool_results = msgs[-1]["content"]
    assert tool_results[0]["type"] == "tool_result"
    assert tool_results[0]["tool_use_id"] == "tu_1"
    assert "stub search context" in tool_results[0]["content"]
    backend._tavily.search.assert_awaited_once_with("AAPL latest catalyst 2026")


async def test_deepseek_caps_tool_calls_then_forces_final_turn(monkeypatch):
    """Once the search budget (max_search_calls) is exhausted, the loop must
    issue one final no-tool messages.create so the model is forced to emit
    text instead of looping forever. With budget=1, only 2 calls are made
    total (no discarded round trip)."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dsk")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly")
    backend = llm.build_backend({"backend": "deepseek", "deepseek": {"max_search_calls": 1}})

    backend._client.messages.create = AsyncMock(
        side_effect=[
            _tool_use_response("q1"),
            _final_text_response("### 公司速览\n\nfinal"),
        ]
    )
    backend._tavily.search = AsyncMock(return_value="ctx")
    backend._tavily.__aenter__ = AsyncMock(return_value=backend._tavily)
    backend._tavily.__aexit__ = AsyncMock(return_value=None)

    out = await backend.analyze("<sys>", "<user>")
    assert out.startswith("### 公司速览")
    assert backend._client.messages.create.await_count == 2
    # Second (forced final) call must NOT include a `tools` kwarg.
    second_call_kwargs = backend._client.messages.create.await_args_list[1].kwargs
    assert "tools" not in second_call_kwargs
    backend._tavily.search.assert_awaited_once_with("q1")


async def test_toolloop_budget_two_allows_two_searches(monkeypatch):
    """budget=2: the model may search twice before being forced to a final
    no-tool turn. Net calls = budget + 1 at most, no discarded turn."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dsk")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly")
    backend = llm.build_backend({"backend": "deepseek", "deepseek": {"max_search_calls": 2}})

    backend._client.messages.create = AsyncMock(
        side_effect=[
            _tool_use_response("q1"),
            _tool_use_response("q2"),
            _final_text_response("### 公司速览\n\nfinal"),
        ]
    )
    backend._tavily.search = AsyncMock(return_value="ctx")
    backend._tavily.__aenter__ = AsyncMock(return_value=backend._tavily)
    backend._tavily.__aexit__ = AsyncMock(return_value=None)

    out = await backend.analyze("<sys>", "<user>")
    assert out.startswith("### 公司速览")
    assert backend._client.messages.create.await_count == 3
    assert backend._tavily.search.await_count == 2
    third_call_kwargs = backend._client.messages.create.await_args_list[2].kwargs
    assert "tools" not in third_call_kwargs


async def test_toolloop_budget_zero_makes_single_call_without_tools(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dsk")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly")
    backend = llm.build_backend({"backend": "deepseek"})
    backend._client.messages.create = AsyncMock(
        return_value=_final_text_response("### 公司速览\n\nno search")
    )
    backend._tavily.search = AsyncMock()
    backend._tavily.__aenter__ = AsyncMock(return_value=backend._tavily)
    backend._tavily.__aexit__ = AsyncMock(return_value=None)

    out = await backend.analyze("<sys>", "<user>", max_search_calls=0)

    assert out.startswith("### 公司速览")
    assert backend._client.messages.create.await_count == 1
    kwargs = backend._client.messages.create.await_args.kwargs
    assert "tools" not in kwargs
    backend._tavily.search.assert_not_awaited()


async def test_toolloop_explicit_budget_overrides_constructor_default(monkeypatch):
    """Constructor default is 2; passing 1 must cap the loop at 1 search,
    with no discarded round trip (net calls = budget + 1 = 2)."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dsk")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly")
    backend = llm.build_backend({"backend": "deepseek", "deepseek": {"max_search_calls": 2}})
    backend._client.messages.create = AsyncMock(
        side_effect=[
            _tool_use_response("q1"),
            _final_text_response("### 公司速览\n\nfinal"),
        ]
    )
    backend._tavily.search = AsyncMock(return_value="ctx")
    backend._tavily.__aenter__ = AsyncMock(return_value=backend._tavily)
    backend._tavily.__aexit__ = AsyncMock(return_value=None)

    out = await backend.analyze("<sys>", "<user>", max_search_calls=1)
    assert out.startswith("### 公司速览")
    assert backend._client.messages.create.await_count == 2
    assert "tools" not in backend._client.messages.create.await_args_list[1].kwargs
    # Only the first tool_use was actually searched (budget 1).
    assert backend._tavily.search.await_count == 1


async def test_toolloop_disables_thinking_by_default(monkeypatch):
    """v4-pro has thinking mode on by default on the Anthropic-compat endpoint,
    and reasoning tokens eat into max_tokens — leaving little/no room for the
    actual report text. Disable it unless a vendor config opts in."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dsk")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly")
    backend = llm.build_backend({"backend": "deepseek"})
    backend._client.messages.create = AsyncMock(
        return_value=_final_text_response("### 公司速览\n\nx")
    )
    await backend.analyze("<sys>", "<user>", max_search_calls=0)
    kwargs = backend._client.messages.create.await_args.kwargs
    assert kwargs["thinking"] == {"type": "disabled"}


async def test_toolloop_thinking_true_omits_param(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dsk")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly")
    backend = llm.build_backend({"backend": "deepseek", "deepseek": {"thinking": True}})
    backend._client.messages.create = AsyncMock(
        return_value=_final_text_response("### 公司速览\n\nx")
    )
    await backend.analyze("<sys>", "<user>", max_search_calls=0)
    kwargs = backend._client.messages.create.await_args.kwargs
    assert "thinking" not in kwargs


async def test_toolloop_thinking_disabled_on_forced_final_turn(monkeypatch):
    """Every messages.create call the loop makes — including the tool-use
    iterations and the forced final no-tool turn — must carry the disabled
    thinking param, not just the simple no-search path."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dsk")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly")
    backend = llm.build_backend({"backend": "deepseek", "deepseek": {"max_search_calls": 1}})

    backend._client.messages.create = AsyncMock(
        side_effect=[
            _tool_use_response("q1"),
            _tool_use_response("q2"),
            _final_text_response("### 公司速览\n\nfinal"),
        ]
    )
    backend._tavily.search = AsyncMock(return_value="ctx")
    backend._tavily.__aenter__ = AsyncMock(return_value=backend._tavily)
    backend._tavily.__aexit__ = AsyncMock(return_value=None)

    await backend.analyze("<sys>", "<user>")
    for call in backend._client.messages.create.await_args_list:
        assert call.kwargs["thinking"] == {"type": "disabled"}


async def test_toolloop_none_budget_uses_constructor_default(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dsk")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly")
    backend = llm.build_backend({"backend": "deepseek", "deepseek": {"max_search_calls": 0}})
    backend._client.messages.create = AsyncMock(
        return_value=_final_text_response("### 公司速览\n\nx")
    )
    backend._tavily.__aenter__ = AsyncMock(return_value=backend._tavily)
    backend._tavily.__aexit__ = AsyncMock(return_value=None)
    await backend.analyze("<sys>", "<user>")
    assert "tools" not in backend._client.messages.create.await_args.kwargs


async def test_anthropic_budget_zero_omits_web_search_tool(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    backend = llm.build_backend({"backend": "anthropic"})
    backend._client.messages.create = AsyncMock(
        return_value=_final_text_response("### 公司速览\n\nx")
    )
    await backend.analyze("<sys>", "<user>", max_search_calls=0)
    assert "tools" not in backend._client.messages.create.await_args.kwargs


async def test_anthropic_budget_positive_sets_max_uses(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    backend = llm.build_backend({"backend": "anthropic", "anthropic": {"web_search_max_uses": 2}})
    backend._client.messages.create = AsyncMock(
        return_value=_final_text_response("### 公司速览\n\nx")
    )
    await backend.analyze("<sys>", "<user>", max_search_calls=1)
    tools = backend._client.messages.create.await_args.kwargs["tools"]
    assert tools[0]["type"].startswith("web_search") and tools[0]["max_uses"] == 1
    await backend.analyze("<sys>", "<user>")
    tools = backend._client.messages.create.await_args.kwargs["tools"]
    assert tools[0]["max_uses"] == 2


def test_log_usage_formats_tokens(caplog):
    usage = MagicMock()
    usage.input_tokens = 4000
    usage.output_tokens = 3000
    usage.cache_read_input_tokens = 1200
    usage.server_tool_use = None
    resp = MagicMock()
    resp.usage = usage
    with caplog.at_level("INFO", logger="report.llm"):
        llm._log_usage("deepseek-v4-pro", resp)
    assert "[llm] deepseek-v4-pro in=4000 cache_hit=1200 out=3000" in caplog.text


def test_log_usage_tolerates_missing_usage(caplog):
    resp = MagicMock()
    resp.usage = None
    with caplog.at_level("INFO", logger="report.llm"):
        llm._log_usage("m", resp)  # must not raise
