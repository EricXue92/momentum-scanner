"""Per-ticker LLM call with retry, timeout, and placeholder-on-failure.

Backend-agnostic: the actual model + search wiring lives in `report.llm`.
This file owns the retry policy and the contract that callers always get a
non-empty markdown section back (real result on success, distinct
`[配置错误]` / `[分析失败]` placeholders on failure)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import anthropic

from report.llm import LLMBackend

logger = logging.getLogger(__name__)

RETRY_BACKOFF_SECONDS = 5.0
# Sonnet 4.6 + web_search (≤2 calls) + 8-section Chinese output runs 30–90s
# typical, with a long tail when Anthropic queues a 5xx auto-retry inside the
# SDK (which is inside our wait_for). 180s gives the tail room without making
# the wrapper feel hung. DeepSeek tool-loop has similar latency once Tavily
# round-trips are factored in.
PER_CALL_TIMEOUT_SECONDS = 180.0

_COMPACT = {"separators": (",", ":"), "ensure_ascii": False, "default": str}


def build_user_message(data: dict[str, Any], evidence: dict[str, Any] | None = None) -> str:
    """Serialize the enrichment dict (and, when present, the pre-fetched
    evidence bundle) as a structured prompt for the model. Compact JSON —
    indentation was ~30% of the input tokens for zero benefit."""
    payload = json.dumps(data, **_COMPACT)
    parts = [
        f"Ticker: {data['ticker']}  |  Group: {data['group']}  |  Exchange: {data['exchange']}\n\n"
        f"Structured data (use these numbers verbatim in the Snapshot block; for any field that is "
        f"null, write '信息不足' in the qualitative analysis):\n\n```json\n{payload}\n```\n\n"
    ]
    if evidence is not None:
        ev_payload = json.dumps(evidence, **_COMPACT)
        parts.append(
            "Pre-fetched evidence (news / analyst consensus / earnings calendar / SEC filings; "
            "use these as primary sources for the qualitative sections and cite as (source, date)):"
            f"\n\n```json\n{ev_payload}\n```\n\n"
            "Generate the Markdown section per the template in the system prompt. Ground every "
            "qualitative section in the evidence above. Only call `web_search` if the tool is "
            "offered in this request, and at most once."
        )
    else:
        parts.append(
            "Generate the Markdown section per the template in the system prompt. Use the web_search "
            "tool sparingly (≤2 calls) for the qualitative legs."
        )
    return "".join(parts)


async def analyze_ticker(
    backend: LLMBackend,
    system_prompt: str,
    data: dict[str, Any],
    semaphore: asyncio.Semaphore,
    *,
    evidence: dict[str, Any] | None = None,
    max_search_calls: int | None = None,
) -> str:
    """Call the backend for one ticker. On retry exhaustion, return a
    placeholder Markdown section so the renderer never sees a missing entry."""
    user_msg = build_user_message(data, evidence)
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            async with semaphore:
                text = await asyncio.wait_for(
                    backend.analyze(system_prompt, user_msg, max_search_calls=max_search_calls),
                    timeout=PER_CALL_TIMEOUT_SECONDS,
                )
            if not text:
                raise RuntimeError("empty response")
            return text
        except anthropic.APIStatusError as e:
            # Retry only transient HTTP failures: 5xx server errors, 408 timeout,
            # 429 rate limit. 4xx (401 bad key, 400 malformed, 404 wrong model) are
            # configuration bugs — fail loudly with a distinct placeholder rather
            # than burn a retry and produce N misleading "[分析失败]" sections.
            status = getattr(e, "status_code", None)
            retriable = status is None or status >= 500 or status in (408, 429)
            if not retriable:
                logger.error(
                    f"[analyst] {data['ticker']}: non-retriable HTTP {status}: {e}"
                )
                return (
                    f"## {data['ticker']} — {data.get('company_name') or '?'} "
                    f"({data['exchange']} · {data['group']})\n\n"
                    f"[配置错误: HTTP {status}: {e}]\n"
                )
            last_error = e
            logger.warning(
                f"[analyst] {data['ticker']}: attempt {attempt} failed: "
                f"HTTP {status}: {e}"
            )
            if attempt == 1:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS)
        except (
            anthropic.APIConnectionError,
            asyncio.TimeoutError,
            RuntimeError,
        ) as e:
            last_error = e
            logger.warning(
                f"[analyst] {data['ticker']}: attempt {attempt} failed: "
                f"{type(e).__name__}: {e}"
            )
            if attempt == 1:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS)
    return (
        f"## {data['ticker']} — {data.get('company_name') or '?'} "
        f"({data['exchange']} · {data['group']})\n\n"
        f"[分析失败: {type(last_error).__name__}: {last_error}]\n"
    )
