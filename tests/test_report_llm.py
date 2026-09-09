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
    text instead of looping forever."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dsk")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly")
    backend = llm.build_backend({"backend": "deepseek", "deepseek": {"max_search_calls": 1}})

    backend._client.messages.create = AsyncMock(
        side_effect=[
            _tool_use_response("q1"),       # iter 0 — search budget=1, used 1
            _tool_use_response("q2"),       # iter 1 — would use 2 but loop hits cap
            _final_text_response("### 公司速览\n\nfinal"),  # forced no-tool turn
        ]
    )
    backend._tavily.search = AsyncMock(return_value="ctx")
    backend._tavily.__aenter__ = AsyncMock(return_value=backend._tavily)
    backend._tavily.__aexit__ = AsyncMock(return_value=None)

    out = await backend.analyze("<sys>", "<user>")
    assert out.startswith("### 公司速览")
    # Third (forced) call must NOT include a `tools` kwarg.
    third_call_kwargs = backend._client.messages.create.await_args_list[2].kwargs
    assert "tools" not in third_call_kwargs
