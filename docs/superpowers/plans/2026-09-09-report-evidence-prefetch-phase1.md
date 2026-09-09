# 证据包预取(阶段 1:EOD CANSLIM 报告)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** EOD CANSLIM 报告每只票只调一次 LLM,证据(新闻 / 分析师共识 / 财报日历 / SEC 公告)由程序从 yfinance + EDGAR 预取;证据不足时才放开 1 次 Tavily 搜索。

**Architecture:** 新增 `report/evidence.py`(纯函数,四个数据源各自 soft-fail,输出一个 JSON 友好的 dict + 搜索预算);`report/llm.py` 的 `analyze()` 增加 `max_search_calls` 参数(0 = 无 tool 单次调用,None = 旧行为);`report/analyst.py` 把 evidence 块拼进 user message;`report/__main__.py` 在 enrich 循环里逐票预取;renderer 加一行证据小字;system prompt 改为"以 evidence 为准"。

**Tech Stack:** Python 3.12, yfinance, httpx, anthropic SDK, pytest + pytest-asyncio(`asyncio_mode = "auto"`,async 测试函数不需要装饰器)。

**Spec:** `docs/superpowers/specs/2026-09-09-report-evidence-prefetch-design.md`

## Global Constraints

- 阶段 1 只改 EOD 路径。`report/morning.py` 一行不改;它调用 `backend.analyze(system, user)` 不传预算,必须继续按构造函数里的 `max_search_calls` 跑。
- `[report.evidence].enabled = false` 时:不预取、不加 evidence 块、预算为 `None`(后端默认)、user message 提示语用旧文案。唯一允许的差异是 JSON 改为紧凑序列化。
- 每个数据源独立 try/except;任何一源失败只让该字段为 `null`(列表源为 `[]`),错误文本进 `errors`;`fetch_evidence` 永不抛异常。
- 预取有超时:调用方用 `asyncio.wait_for(asyncio.to_thread(...), timeout_seconds)`;超时视为空证据。
- 所有 `messages.create` 之后记一行 INFO:`[llm] <model> in=<n> cache_hit=<n|-> out=<n>`。
- 所有命令用 `uv run ...`。测试跑 `uv run pytest tests/ -q`。
- 提交信息结尾加:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01WNEAh2FKWZC9U5gJ1UKtTb
  ```
- 文档/注释用中文或英文都可,和所在文件保持一致(现有 report/ 代码注释是英文)。

## 文件结构

| 文件                            | 动作 | 职责                                                                                                       |
| ------------------------------- | ---- | ---------------------------------------------------------------------------------------------------------- |
| `report/evidence.py`            | 新增 | `EvidenceConfig`、四源抓取、`fetch_evidence`、`search_budget`、`summarize_for_log`                         |
| `report/edgar.py`               | 修改 | 新增 `fetch_recent_filings(ticker, days, max_items, ttl_seconds)`,复用现有 CIK/HTTP/缓存 helper            |
| `report/llm.py`                 | 修改 | `analyze(..., max_search_calls=None)`;预算 0 无 tool;token 用量日志                                        |
| `report/analyst.py`             | 修改 | `build_user_message(data, evidence=None)`;`analyze_ticker(..., evidence=None, max_search_calls=None)`      |
| `report/__main__.py`            | 修改 | 读 `[report.evidence]`,逐票预取 + 预算 + 日志,把 evidence meta 传给 renderer                               |
| `report/renderer.py`            | 修改 | `_render_ticker_block` / `render_html_document` / `write_report_files` 接受 `evidence_meta`,渲染证据小字行 |
| `prompts/canslim_system.md`     | 修改 | 规则 7 改为 evidence-first;新增规则 10                                                                     |
| `config.toml`                   | 修改 | 新增 `[report.evidence]`                                                                                   |
| `CLAUDE.md`                     | 修改 | Report 条目补一句                                                                                          |
| `tests/test_report_evidence.py` | 新增 | evidence 单元测试                                                                                          |
| `tests/test_report_edgar.py`    | 修改 | `fetch_recent_filings` 测试                                                                                |
| `tests/test_report_llm.py`      | 修改 | 预算 0 / 1 / None 行为                                                                                     |
| `tests/test_report_analyst.py`  | 修改 | evidence 块、透传                                                                                          |
| `tests/test_report_renderer.py` | 修改 | 证据行                                                                                                     |
| `tests/test_report_main.py`     | 修改 | `_load_evidence_config`、`_prefetch_evidence` 超时                                                         |

---

### Task 1: `EvidenceConfig` + `search_budget` + `summarize_for_log`

**Files:**

- Create: `report/evidence.py`
- Test: `tests/test_report_evidence.py`

**Interfaces:**

- Produces:

  ```python
  @dataclass(frozen=True)
  class EvidenceConfig:
      enabled: bool = True
      news_max_items: int = 10
      news_max_age_hours: int = 720
      news_summary_chars: int = 300
      filings_days: int = 60
      filings_max_items: int = 8
      analyst_grades_days: int = 90
      search_fallback: bool = True
      min_items_for_no_search: int = 3
      fallback_search_calls: int = 1
      timeout_seconds: float = 20.0

      @classmethod
      def from_dict(cls, raw: dict | None) -> "EvidenceConfig": ...

  def empty_evidence(as_of: date, errors: list[str] | None = None) -> dict
  def search_budget(evidence: dict | None, cfg: EvidenceConfig) -> int | None
  def summarize_for_log(evidence: dict | None) -> str
  ```

- [ ] **Step 1: 写失败测试**

```python
# tests/test_report_evidence.py
"""Tests for report/evidence.py — pre-fetched evidence bundle."""
from __future__ import annotations

from datetime import date

import pytest

from report import evidence


def test_config_from_dict_defaults():
    cfg = evidence.EvidenceConfig.from_dict(None)
    assert cfg.enabled is True
    assert cfg.news_max_items == 10
    assert cfg.news_max_age_hours == 720
    assert cfg.min_items_for_no_search == 3
    assert cfg.fallback_search_calls == 1
    assert cfg.timeout_seconds == 20.0


def test_config_from_dict_overrides_and_ignores_unknown_keys():
    cfg = evidence.EvidenceConfig.from_dict(
        {"enabled": False, "news_max_items": 3, "timeout_seconds": 5, "bogus": 1}
    )
    assert cfg.enabled is False
    assert cfg.news_max_items == 3
    assert cfg.timeout_seconds == 5.0


def test_empty_evidence_shape():
    ev = evidence.empty_evidence(date(2026, 9, 9), errors=["timeout"])
    assert ev["as_of"] == "2026-09-09"
    assert ev["news"] == [] and ev["filings"] == []
    assert ev["analyst"] is None and ev["calendar"] is None
    assert ev["news_count"] == 0 and ev["filings_count"] == 0
    assert ev["form4_count"] == 0
    assert ev["errors"] == ["timeout"]


def _ev(news: int, filings: int) -> dict:
    ev = evidence.empty_evidence(date(2026, 9, 9))
    ev["news_count"] = news
    ev["filings_count"] = filings
    return ev


def test_search_budget_disabled_returns_none():
    cfg = evidence.EvidenceConfig(enabled=False)
    assert evidence.search_budget(_ev(0, 0), cfg) is None
    assert evidence.search_budget(None, cfg) is None


def test_search_budget_no_fallback_returns_zero():
    cfg = evidence.EvidenceConfig(search_fallback=False)
    assert evidence.search_budget(_ev(0, 0), cfg) == 0


def test_search_budget_enough_items_returns_zero():
    cfg = evidence.EvidenceConfig(min_items_for_no_search=3)
    assert evidence.search_budget(_ev(2, 1), cfg) == 0
    assert evidence.search_budget(_ev(10, 0), cfg) == 0


def test_search_budget_too_few_items_returns_fallback():
    cfg = evidence.EvidenceConfig(min_items_for_no_search=3, fallback_search_calls=1)
    assert evidence.search_budget(_ev(2, 0), cfg) == 1
    # None evidence (prefetch failed entirely) counts as zero items.
    assert evidence.search_budget(None, cfg) == 1


def test_summarize_for_log():
    ev = _ev(10, 6)
    ev["analyst"] = {"price_target": {"mean": 1.0}}
    assert evidence.summarize_for_log(ev) == "news=10 filings=6 analyst=yes calendar=no"
    assert evidence.summarize_for_log(None) == "news=0 filings=0 analyst=no calendar=no"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_report_evidence.py -q`
Expected: FAIL,`ModuleNotFoundError: No module named 'report.evidence'`

- [ ] **Step 3: 最小实现**

```python
# report/evidence.py
"""Pre-fetched evidence bundle for the CANSLIM report.

Replaces model-driven web search as the *primary* source of qualitative
context. Four independent sources — yfinance news, yfinance analyst
consensus, yfinance earnings calendar, SEC EDGAR recent filings — are
fetched by code, each soft-failing on its own, and handed to the LLM as
one JSON block. Tavily search stays available only as a conditional
fallback (see `search_budget`).

Spec: docs/superpowers/specs/2026-09-09-report-evidence-prefetch-design.md
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvidenceConfig:
    """Tunables from `[report.evidence]` (EOD defaults). Phase 2 builds a
    second instance from `[morning_gap_catalyst]` with tighter windows."""

    enabled: bool = True
    news_max_items: int = 10
    news_max_age_hours: int = 720  # 30 days
    news_summary_chars: int = 300
    filings_days: int = 60
    filings_max_items: int = 8
    analyst_grades_days: int = 90
    search_fallback: bool = True
    min_items_for_no_search: int = 3
    fallback_search_calls: int = 1
    timeout_seconds: float = 20.0

    @classmethod
    def from_dict(cls, raw: dict | None) -> "EvidenceConfig":
        """Build from a TOML table; unknown keys ignored, values coerced to
        the field's declared type so `timeout_seconds = 5` works."""
        raw = raw or {}
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            if f.name not in raw:
                continue
            value = raw[f.name]
            if f.type == "bool" or f.type is bool:
                kwargs[f.name] = bool(value)
            elif f.type == "float" or f.type is float:
                kwargs[f.name] = float(value)
            else:
                kwargs[f.name] = int(value)
        return cls(**kwargs)


def empty_evidence(as_of: date, errors: list[str] | None = None) -> dict:
    """The all-sources-failed (or not-yet-fetched) bundle. Every consumer
    can rely on these keys existing."""
    return {
        "as_of": as_of.isoformat(),
        "news": [],
        "analyst": None,
        "calendar": None,
        "filings": [],
        "form4_count": 0,
        "news_count": 0,
        "filings_count": 0,
        "errors": list(errors or []),
    }


def search_budget(evidence: dict | None, cfg: EvidenceConfig) -> int | None:
    """How many web_search calls the LLM may issue for this ticker.
    None = feature disabled, let the backend use its own default (legacy).
    0 = evidence sufficient, single no-tool call.
    >0 = fallback budget."""
    if not cfg.enabled:
        return None
    if not cfg.search_fallback:
        return 0
    items = 0
    if evidence:
        items = int(evidence.get("news_count") or 0) + int(evidence.get("filings_count") or 0)
    return 0 if items >= cfg.min_items_for_no_search else cfg.fallback_search_calls


def summarize_for_log(evidence: dict | None) -> str:
    if not evidence:
        return "news=0 filings=0 analyst=no calendar=no"
    yn = lambda v: "yes" if v else "no"  # noqa: E731
    return (
        f"news={evidence.get('news_count', 0)} filings={evidence.get('filings_count', 0)} "
        f"analyst={yn(evidence.get('analyst'))} calendar={yn(evidence.get('calendar'))}"
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_report_evidence.py -q`
Expected: 8 passed

- [ ] **Step 5: 提交**

```bash
git add report/evidence.py tests/test_report_evidence.py
git commit -m "feat(report): evidence config, empty bundle, search budget"
```

---

### Task 2: EDGAR 近期公告 `fetch_recent_filings`

**Files:**

- Modify: `report/edgar.py`(在文件末尾 `fetch_edgar_fundamentals` 之后追加)
- Test: `tests/test_report_edgar.py`(追加)

**Interfaces:**

- Consumes: `edgar._get_cik`, `edgar._http_get_json`, `edgar._is_fresh`, `edgar._load_json_cache`, `edgar._save_json_cache`, `edgar.CACHE_DIR`(均已存在)
- Produces:

  ```python
  SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
  SUBMISSIONS_TTL = 86_400
  FILING_FORMS_OF_INTEREST: frozenset[str]
  def fetch_recent_filings(
      ticker: str, *, days: int, max_items: int, as_of: date | None = None,
      ttl_seconds: int = SUBMISSIONS_TTL,
  ) -> tuple[list[dict], int] | None
  # -> (filings, form4_count); None when CIK unknown or both cache+network fail
  # filing dict: {"form", "date", "items", "description", "url"}
  ```

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_report_edgar.py` 末尾:

```python
# --- fetch_recent_filings ----------------------------------------------------

from datetime import date as _date  # noqa: E402


def _submissions_payload() -> dict:
    return {
        "cik": 1051627,
        "filings": {
            "recent": {
                "form": ["4", "4", "144", "10-Q", "8-K", "SC 13G/A", "8-K", "4"],
                "filingDate": [
                    "2026-08-19", "2026-08-19", "2026-08-17", "2026-08-13",
                    "2026-07-30", "2026-08-12", "2026-05-01", "2026-05-02",
                ],
                "items": ["", "", "", "", "2.02,9.01", "", "5.02", ""],
                "primaryDocDescription": ["FORM 4", "FORM 4", "", "FORM 10-Q", "8-K", "", "8-K", "FORM 4"],
                "accessionNumber": [
                    "0001051627-26-000010", "0001051627-26-000011", "0001051627-26-000012",
                    "0001051627-26-000013", "0001051627-26-000009", "0001051627-26-000014",
                    "0001051627-26-000005", "0001051627-26-000006",
                ],
                "primaryDocument": [
                    "f4.xml", "f4.xml", "f144.pdf", "axti-10q.htm",
                    "axti-8k.htm", "sc13g.htm", "axti-8k-may.htm", "f4.xml",
                ],
            }
        },
    }


def test_fetch_recent_filings_filters_forms_window_and_counts_form4(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(edgar, "_get_cik", lambda t: "0001051627")
    monkeypatch.setattr(edgar, "_http_get_json", lambda url: _submissions_payload())

    result = edgar.fetch_recent_filings(
        "AXTI", days=60, max_items=8, as_of=_date(2026, 9, 9)
    )
    assert result is not None
    filings, form4_count = result
    # Window = 2026-07-11..2026-09-09: drops the two May filings.
    forms = [f["form"] for f in filings]
    assert forms == ["10-Q", "SC 13G/A", "8-K"]          # newest first, Form 4 / 144 excluded
    assert form4_count == 2                              # only the two August Form 4s
    eightk = filings[-1]
    assert eightk["date"] == "2026-07-30"
    assert eightk["items"] == "2.02,9.01"
    assert eightk["description"] == "8-K"
    assert eightk["url"] == (
        "https://www.sec.gov/Archives/edgar/data/1051627/000105162726000009/axti-8k.htm"
    )
    # Cache written for reuse.
    assert (tmp_path / "submissions_CIK0001051627.json").is_file()


def test_fetch_recent_filings_respects_max_items(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(edgar, "_get_cik", lambda t: "0001051627")
    monkeypatch.setattr(edgar, "_http_get_json", lambda url: _submissions_payload())
    filings, _ = edgar.fetch_recent_filings("AXTI", days=60, max_items=2, as_of=_date(2026, 9, 9))
    assert [f["form"] for f in filings] == ["10-Q", "SC 13G/A"]


def test_fetch_recent_filings_uses_fresh_cache_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(edgar, "_get_cik", lambda t: "0001051627")
    edgar._save_json_cache(tmp_path / "submissions_CIK0001051627.json", _submissions_payload())
    calls = []
    monkeypatch.setattr(edgar, "_http_get_json", lambda url: calls.append(url) or None)
    result = edgar.fetch_recent_filings("AXTI", days=60, max_items=8, as_of=_date(2026, 9, 9))
    assert result is not None and calls == []


def test_fetch_recent_filings_returns_none_when_cik_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(edgar, "_get_cik", lambda t: None)
    assert edgar.fetch_recent_filings("ZZZZ", days=60, max_items=8) is None


def test_fetch_recent_filings_returns_none_when_network_and_cache_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(edgar, "_get_cik", lambda t: "0001051627")
    monkeypatch.setattr(edgar, "_http_get_json", lambda url: None)
    assert edgar.fetch_recent_filings("AXTI", days=60, max_items=8) is None


def test_fetch_recent_filings_tolerates_ragged_arrays(tmp_path, monkeypatch):
    """SEC occasionally ships arrays of unequal length; zip must not blow up
    and missing `items` must render as empty string."""
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(edgar, "_get_cik", lambda t: "0000000001")
    payload = {"filings": {"recent": {
        "form": ["8-K", "10-K"], "filingDate": ["2026-09-01", "2026-08-01"],
        "accessionNumber": ["0000000001-26-000001", "0000000001-26-000002"],
        "primaryDocument": ["a.htm", "b.htm"],
        # no `items`, and description shorter than the others
        "primaryDocDescription": ["8-K"],
    }}}
    monkeypatch.setattr(edgar, "_http_get_json", lambda url: payload)
    filings, form4 = edgar.fetch_recent_filings("X", days=60, max_items=8, as_of=_date(2026, 9, 9))
    assert [f["form"] for f in filings] == ["8-K", "10-K"]
    assert filings[0]["items"] == "" and filings[1]["description"] == ""
    assert form4 == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_report_edgar.py -q -k recent_filings`
Expected: FAIL,`AttributeError: module 'report.edgar' has no attribute 'fetch_recent_filings'`

- [ ] **Step 3: 实现**

追加到 `report/edgar.py` 末尾:

```python
# --- Recent filings (evidence bundle) ---------------------------------------

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SUBMISSIONS_TTL = 86_400  # 1 day; the pre-market path (phase 2) passes 6h

# Material-event / periodic / offering / ownership forms. Form 4 and 144
# (insider trades) are counted, not listed — too noisy for the LLM.
FILING_FORMS_OF_INTEREST: frozenset[str] = frozenset({
    "8-K", "8-K/A", "6-K", "10-Q", "10-Q/A", "10-K", "10-K/A",
    "S-1", "S-1/A", "S-3", "S-3/A",
})
_FILING_FORM_PREFIXES = ("424B", "SC 13D", "SC 13G")
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"


def _form_of_interest(form: str) -> bool:
    return form in FILING_FORMS_OF_INTEREST or form.startswith(_FILING_FORM_PREFIXES)


def _fetch_submissions(cik: str, ttl_seconds: int) -> dict | None:
    cache_path = CACHE_DIR / f"submissions_CIK{cik}.json"
    if _is_fresh(cache_path, ttl_seconds):
        cached = _load_json_cache(cache_path)
        if cached:
            return cached
    fresh = _http_get_json(SUBMISSIONS_URL.format(cik=cik))
    if fresh:
        _save_json_cache(cache_path, fresh)
        return fresh
    cached = _load_json_cache(cache_path)
    if cached:
        logger.info(f"[edgar] using stale submissions cache for CIK{cik} (network failed)")
        return cached
    return None


def fetch_recent_filings(
    ticker: str,
    *,
    days: int,
    max_items: int,
    as_of: _date | None = None,
    ttl_seconds: int = SUBMISSIONS_TTL,
) -> tuple[list[dict], int] | None:
    """Recent filings of interest for `ticker` within the last `days` days
    (newest first, at most `max_items`) plus the count of Form 4 filings in
    the same window. Returns None when the ticker has no CIK or the
    submissions feed is unavailable. Never raises."""
    cik = _get_cik(ticker)
    if not cik:
        return None
    raw = _fetch_submissions(cik, ttl_seconds)
    if not raw:
        return None
    recent = ((raw.get("filings") or {}).get("recent")) or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    items = recent.get("items") or []
    descs = recent.get("primaryDocDescription") or []
    accs = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []

    today = as_of or _date.today()
    cutoff = today.fromordinal(today.toordinal() - days)
    cik_int = int(cik)

    def at(seq: list, i: int) -> str:
        return str(seq[i]) if i < len(seq) and seq[i] is not None else ""

    filings: list[dict] = []
    form4_count = 0
    for i in range(min(len(forms), len(dates))):
        form = at(forms, i).strip()
        filed = _parse_iso_date(at(dates, i))
        if filed is None or filed < cutoff or filed > today:
            continue
        if form == "4":
            form4_count += 1
            continue
        if not _form_of_interest(form):
            continue
        acc = at(accs, i).replace("-", "")
        doc = at(docs, i)
        url = ARCHIVE_URL.format(cik_int=cik_int, acc_nodash=acc, doc=doc) if acc and doc else ""
        filings.append({
            "form": form,
            "date": filed.isoformat(),
            "items": at(items, i),
            "description": at(descs, i),
            "url": url,
        })
    filings.sort(key=lambda f: f["date"], reverse=True)
    return filings[:max_items], form4_count
```

`_parse_iso_date` 已在文件里(第 271 行附近),签名 `(s: str) -> date | None`,直接复用。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_report_edgar.py -q`
Expected: 全部通过(含原有测试)

- [ ] **Step 5: 提交**

```bash
git add report/edgar.py tests/test_report_edgar.py
git commit -m "feat(edgar): fetch_recent_filings from SEC submissions feed"
```

---

### Task 3: yfinance 三源 + `fetch_evidence`

**Files:**

- Modify: `report/evidence.py`
- Test: `tests/test_report_evidence.py`(追加)

**Interfaces:**

- Consumes: `edgar.fetch_recent_filings(ticker, days=, max_items=, as_of=)`(Task 2);`EvidenceConfig`、`empty_evidence`(Task 1)
- Produces:

  ```python
  def fetch_evidence(yf_symbol: str, market: str, cfg: EvidenceConfig, *, as_of: date | None = None) -> dict
  # 内部可测单元:
  def _extract_news(raw_news: list, *, now: datetime, cfg: EvidenceConfig) -> list[dict]
  def _extract_analyst(t, *, now: datetime, cfg: EvidenceConfig) -> dict | None
  def _extract_calendar(t) -> dict | None
  ```

  `market == "us"` 时 filings 走 EDGAR(ticker 用 `yf_symbol` 原样,US 就是裸 ticker);其它 market 时 `filings = None`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_report_evidence.py`:

```python
from datetime import datetime, timezone  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pandas as pd  # noqa: E402

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _news_item(title: str, pub: str, summary: str = "s", provider: str = "Reuters", url: str = "https://x/y"):
    return {
        "id": title,
        "content": {
            "title": title,
            "summary": summary,
            "pubDate": pub,
            "provider": {"displayName": provider},
            "canonicalUrl": {"url": url},
        },
    }


def test_extract_news_filters_age_sorts_and_truncates():
    cfg = evidence.EvidenceConfig(news_max_items=2, news_max_age_hours=24 * 30, news_summary_chars=5)
    raw = [
        _news_item("old", "2026-07-01T00:00:00Z"),                       # > 30 days → dropped
        _news_item("mid", "2026-09-01T10:00:00Z", summary="abcdefgh"),
        _news_item("new", "2026-09-08T10:00:00Z"),
        _news_item("newest", "2026-09-09T01:00:00Z"),
    ]
    out = evidence._extract_news(raw, now=NOW, cfg=cfg)
    assert [n["title"] for n in out] == ["newest", "new"]           # newest first, capped at 2
    assert out[0] == {
        "date": "2026-09-09", "title": "newest", "source": "Reuters",
        "summary": "s", "url": "https://x/y",
    }


def test_extract_news_truncates_summary_and_tolerates_missing_fields():
    cfg = evidence.EvidenceConfig(news_summary_chars=5)
    raw = [
        {"content": {"title": "t", "pubDate": "2026-09-08T10:00:00Z", "summary": "abcdefgh"}},
        {"content": {"title": "bad date", "pubDate": "not-a-date"}},   # dropped
        {"content": {"pubDate": "2026-09-08T10:00:00Z"}},               # no title → dropped
        {"no_content": True},                                            # dropped
    ]
    out = evidence._extract_news(raw, now=NOW, cfg=cfg)
    assert len(out) == 1
    assert out[0]["summary"] == "abcde"
    assert out[0]["source"] is None and out[0]["url"] is None


def _ticker_mock(*, targets=None, rec=None, grades=None, calendar=None) -> MagicMock:
    t = MagicMock()
    t.analyst_price_targets = targets if targets is not None else {
        "current": 69.56, "high": 125.0, "low": 55.0, "mean": 91.6, "median": 93.0}
    t.recommendations_summary = rec if rec is not None else pd.DataFrame(
        [{"period": "0m", "strongBuy": 1, "buy": 3, "hold": 1, "sell": 0, "strongSell": 0},
         {"period": "-1m", "strongBuy": 1, "buy": 3, "hold": 1, "sell": 0, "strongSell": 0}])
    t.upgrades_downgrades = grades if grades is not None else pd.DataFrame(
        {"Firm": ["Needham", "B. Riley"], "ToGrade": ["Buy", "Neutral"],
         "FromGrade": ["Hold", "Buy"], "Action": ["up", "down"]},
        index=pd.to_datetime(["2026-08-20 13:00:00", "2025-01-01 10:00:00"]))
    t.calendar = calendar if calendar is not None else {
        "Earnings Date": [date(2026, 10, 30)], "Earnings Average": 0.308,
        "Revenue Average": 66004800}
    return t


def test_extract_analyst_full():
    cfg = evidence.EvidenceConfig(analyst_grades_days=90)
    out = evidence._extract_analyst(_ticker_mock(), now=NOW, cfg=cfg)
    assert out["price_target"] == {"current": 69.56, "mean": 91.6, "high": 125.0, "low": 55.0}
    assert out["ratings"] == {"strongBuy": 1, "buy": 3, "hold": 1, "sell": 0, "strongSell": 0}
    assert out["recent_grades"] == [
        {"date": "2026-08-20", "firm": "Needham", "action": "up", "from": "Hold", "to": "Buy"}
    ]  # the 2025 downgrade is outside the 90-day window


def test_extract_analyst_partial_failure_keeps_other_keys():
    t = _ticker_mock()
    type(t).recommendations_summary = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    out = evidence._extract_analyst(t, now=NOW, cfg=evidence.EvidenceConfig())
    assert out["ratings"] is None
    assert out["price_target"]["mean"] == 91.6


def test_extract_analyst_all_fail_returns_none():
    t = MagicMock()
    # MagicMock gives every instance its own subclass, so properties set on
    # type(t) affect only this mock.
    for attr in ("analyst_price_targets", "recommendations_summary", "upgrades_downgrades"):
        setattr(type(t), attr, property(lambda self: (_ for _ in ()).throw(RuntimeError("x"))))
    assert evidence._extract_analyst(t, now=NOW, cfg=evidence.EvidenceConfig()) is None


def test_extract_calendar():
    out = evidence._extract_calendar(_ticker_mock())
    assert out == {"next_earnings_date": "2026-10-30", "eps_estimate": 0.308, "revenue_estimate": 66004800}
    assert evidence._extract_calendar(_ticker_mock(calendar={})) is None


def test_fetch_evidence_us_assembles_all_sources(monkeypatch):
    t = _ticker_mock()
    t.news = [_news_item("n1", "2026-09-08T10:00:00Z"), _news_item("n2", "2026-09-07T10:00:00Z")]
    monkeypatch.setattr(evidence.yf, "Ticker", lambda sym: t)
    monkeypatch.setattr(
        evidence.edgar, "fetch_recent_filings",
        lambda ticker, **kw: ([{"form": "8-K", "date": "2026-07-30", "items": "2.02", "description": "8-K", "url": "u"}], 2),
    )
    ev = evidence.fetch_evidence("AXTI", "us", evidence.EvidenceConfig(), as_of=date(2026, 9, 9))
    assert ev["as_of"] == "2026-09-09"
    assert ev["news_count"] == 2 and len(ev["news"]) == 2
    assert ev["filings_count"] == 1 and ev["form4_count"] == 2
    assert ev["analyst"]["price_target"]["mean"] == 91.6
    assert ev["calendar"]["next_earnings_date"] == "2026-10-30"
    assert ev["errors"] == []


def test_fetch_evidence_hk_skips_filings(monkeypatch):
    t = _ticker_mock()
    t.news = []
    monkeypatch.setattr(evidence.yf, "Ticker", lambda sym: t)
    called = []
    monkeypatch.setattr(evidence.edgar, "fetch_recent_filings", lambda *a, **kw: called.append(1))
    ev = evidence.fetch_evidence("0700.HK", "hk", evidence.EvidenceConfig(), as_of=date(2026, 9, 9))
    assert ev["filings"] is None and ev["filings_count"] == 0 and called == []


def test_fetch_evidence_single_source_failure_is_isolated(monkeypatch):
    t = _ticker_mock()
    type(t).news = property(lambda self: (_ for _ in ()).throw(RuntimeError("yf down")))
    monkeypatch.setattr(evidence.yf, "Ticker", lambda sym: t)
    monkeypatch.setattr(evidence.edgar, "fetch_recent_filings", lambda ticker, **kw: None)
    ev = evidence.fetch_evidence("AXTI", "us", evidence.EvidenceConfig(), as_of=date(2026, 9, 9))
    assert ev["news"] == [] and ev["news_count"] == 0
    assert any(e.startswith("news:") for e in ev["errors"])
    assert ev["filings"] == [] and ev["filings_count"] == 0     # None from edgar → [] + no error
    assert ev["analyst"] is not None


def test_fetch_evidence_never_raises_when_ticker_ctor_fails(monkeypatch):
    def boom(sym):
        raise RuntimeError("ctor")
    monkeypatch.setattr(evidence.yf, "Ticker", boom)
    ev = evidence.fetch_evidence("AXTI", "us", evidence.EvidenceConfig(), as_of=date(2026, 9, 9))
    assert ev["news_count"] == 0 and ev["analyst"] is None
    assert any("ticker:" in e for e in ev["errors"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_report_evidence.py -q`
Expected: 新增测试 FAIL(`AttributeError: ... has no attribute '_extract_news'` 等)

- [ ] **Step 3: 实现**

在 `report/evidence.py` 顶部 import 区追加:

```python
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from report import edgar
```

在 `summarize_for_log` 之后追加:

```python
# --- Source extractors -------------------------------------------------------

def _parse_pubdate(s: Any) -> datetime | None:
    """yfinance pubDate is ISO-8601 with trailing Z; return aware UTC or None."""
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _extract_news(raw_news: list, *, now: datetime, cfg: EvidenceConfig) -> list[dict]:
    """Flatten `Ticker.news` (list of {content: {...}}) into compact rows,
    drop items older than `news_max_age_hours` or without title/date, newest
    first, capped at `news_max_items`."""
    cutoff_seconds = cfg.news_max_age_hours * 3600
    rows: list[tuple[datetime, dict]] = []
    for item in raw_news or []:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, dict):
            continue
        title = (content.get("title") or "").strip()
        published = _parse_pubdate(content.get("pubDate"))
        if not title or published is None:
            continue
        if (now - published).total_seconds() > cutoff_seconds:
            continue
        provider = content.get("provider") or {}
        source = provider.get("displayName") if isinstance(provider, dict) else None
        url = None
        for key in ("canonicalUrl", "clickThroughUrl"):
            u = content.get(key)
            if isinstance(u, dict) and u.get("url"):
                url = u["url"]
                break
        summary = (content.get("summary") or "").strip() or None
        if summary and len(summary) > cfg.news_summary_chars:
            summary = summary[: cfg.news_summary_chars]
        rows.append((published, {
            "date": published.date().isoformat(),
            "title": title,
            "source": source or None,
            "summary": summary,
            "url": url,
        }))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [r[1] for r in rows[: cfg.news_max_items]]


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN → None


def _extract_analyst(t: Any, *, now: datetime, cfg: EvidenceConfig) -> dict | None:
    """Price targets + rating distribution + recent grade changes. Each of
    the three yfinance properties fails independently; None only when all
    three fail."""
    out: dict[str, Any] = {"price_target": None, "ratings": None, "recent_grades": None}
    failures = 0

    try:
        pt = t.analyst_price_targets or {}
        out["price_target"] = {k: _num(pt.get(k)) for k in ("current", "mean", "high", "low")}
    except Exception:
        failures += 1

    try:
        rec = t.recommendations_summary
        row = None
        if isinstance(rec, pd.DataFrame) and not rec.empty:
            cur = rec[rec.get("period") == "0m"] if "period" in rec.columns else rec
            row = (cur if not cur.empty else rec).iloc[0]
        if row is not None:
            out["ratings"] = {k: int(_num(row.get(k)) or 0)
                              for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
    except Exception:
        failures += 1

    try:
        ud = t.upgrades_downgrades
        grades: list[dict] = []
        if isinstance(ud, pd.DataFrame) and not ud.empty:
            cutoff = now - pd.Timedelta(days=cfg.analyst_grades_days)
            for ts, row in ud.sort_index(ascending=False).iterrows():
                ts = pd.Timestamp(ts)
                if ts.tzinfo is None:
                    ts = ts.tz_localize("UTC")
                if ts < cutoff:
                    continue
                grades.append({
                    "date": ts.date().isoformat(),
                    "firm": str(row.get("Firm") or ""),
                    "action": str(row.get("Action") or ""),
                    "from": str(row.get("FromGrade") or ""),
                    "to": str(row.get("ToGrade") or ""),
                })
                if len(grades) >= 5:
                    break
        out["recent_grades"] = grades
    except Exception:
        failures += 1

    return None if failures == 3 else out


def _extract_calendar(t: Any) -> dict | None:
    cal = t.calendar
    if not isinstance(cal, dict) or not cal:
        return None
    dates = cal.get("Earnings Date") or []
    first = dates[0] if isinstance(dates, (list, tuple)) and dates else None
    next_date = first.isoformat() if isinstance(first, date) else None
    out = {
        "next_earnings_date": next_date,
        "eps_estimate": _num(cal.get("Earnings Average")),
        "revenue_estimate": _num(cal.get("Revenue Average")),
    }
    return out if any(v is not None for v in out.values()) else None


# --- Entry point -------------------------------------------------------------

def fetch_evidence(
    yf_symbol: str,
    market: str,
    cfg: EvidenceConfig,
    *,
    as_of: date | None = None,
) -> dict:
    """Build the evidence bundle for one ticker. Synchronous (yfinance is
    sync); callers wrap it in `asyncio.to_thread` + `wait_for`. Never raises."""
    today = as_of or date.today()
    now = datetime.now(timezone.utc)
    ev = empty_evidence(today)

    try:
        t = yf.Ticker(yf_symbol)
    except Exception as e:  # pragma: no cover — yfinance ctor is lazy, but be safe
        ev["errors"].append(f"ticker: {type(e).__name__}: {e}")
        if market.lower() != "us":
            ev["filings"] = None
        return ev

    try:
        ev["news"] = _extract_news(t.news, now=now, cfg=cfg)
    except Exception as e:
        ev["errors"].append(f"news: {type(e).__name__}: {e}")
    ev["news_count"] = len(ev["news"])

    try:
        ev["analyst"] = _extract_analyst(t, now=now, cfg=cfg)
    except Exception as e:
        ev["errors"].append(f"analyst: {type(e).__name__}: {e}")

    try:
        ev["calendar"] = _extract_calendar(t)
    except Exception as e:
        ev["errors"].append(f"calendar: {type(e).__name__}: {e}")

    if market.lower() == "us":
        try:
            res = edgar.fetch_recent_filings(
                yf_symbol, days=cfg.filings_days, max_items=cfg.filings_max_items, as_of=today,
            )
            if res is not None:
                ev["filings"], ev["form4_count"] = res
        except Exception as e:
            ev["errors"].append(f"filings: {type(e).__name__}: {e}")
        ev["filings_count"] = len(ev["filings"])
    else:
        ev["filings"] = None
        ev["filings_count"] = 0
    return ev
```

注意 `test_fetch_evidence_never_raises_when_ticker_ctor_fails` 要求 ctor 异常被捕获并记 `ticker:` 错误——上面的 `except Exception` 分支覆盖了它,去掉 `pragma: no cover` 也行。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_report_evidence.py -q`
Expected: 全部通过

- [ ] **Step 5: 真实冒烟(不入库)**

Run:

```bash
uv run python -c "
from datetime import date
from report import evidence
import json
ev = evidence.fetch_evidence('AXTI', 'us', evidence.EvidenceConfig(), as_of=date.today())
print(evidence.summarize_for_log(ev), ev['errors'])
print(json.dumps(ev, ensure_ascii=False)[:600])
"
```

Expected: `news=<1..10> filings=<0..8> analyst=yes calendar=yes []`,JSON 里能看到 title/date/url。

- [ ] **Step 6: 提交**

```bash
git add report/evidence.py tests/test_report_evidence.py
git commit -m "feat(report): fetch_evidence — yfinance news/analyst/calendar + EDGAR filings"
```

---

### Task 4: LLM 后端 `max_search_calls` 参数 + token 日志

**Files:**

- Modify: `report/llm.py`(`LLMBackend.analyze`、`AnthropicBackend.analyze`、`ToolLoopBackend.analyze`)
- Test: `tests/test_report_llm.py`(追加)

**Interfaces:**

- Produces:

  ```python
  class LLMBackend(Protocol):
      async def analyze(self, system_prompt: str, user_message: str, *, max_search_calls: int | None = None) -> str: ...
  def _log_usage(model: str, response: Any) -> None
  ```

  语义:`None` → 构造函数默认;`0` → 无 tool 单次调用;`n>0` → loop 上限 n。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_report_llm.py`:

```python
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
    """Constructor default is 2; passing 1 must cap the loop at 1 search."""
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

    out = await backend.analyze("<sys>", "<user>", max_search_calls=1)
    assert out.startswith("### 公司速览")
    assert backend._client.messages.create.await_count == 3
    assert "tools" not in backend._client.messages.create.await_args_list[2].kwargs
    # Only the first tool_use was actually searched (budget 1).
    assert backend._tavily.search.await_count == 1


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_report_llm.py -q`
Expected: 新增测试 FAIL(`TypeError: analyze() got an unexpected keyword argument 'max_search_calls'`、`AttributeError: _log_usage`)

- [ ] **Step 3: 实现**

在 `report/llm.py` 中:

(a) `_extract_text` 之前加:

```python
def _log_usage(model: str, response: Any) -> None:
    """One INFO line per API call so the operator can track token spend
    from the launchd log. Tolerates SDK objects without usage."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    inp = getattr(usage, "input_tokens", None)
    out = getattr(usage, "output_tokens", None)
    cache_hit = getattr(usage, "cache_read_input_tokens", None)
    line = f"[llm] {model} in={inp} cache_hit={cache_hit if cache_hit is not None else '-'} out={out}"
    server = getattr(usage, "server_tool_use", None)
    searches = getattr(server, "web_search_requests", None) if server is not None else None
    if searches is not None:
        line += f" searches={searches}"
    logger.info(line)
```

(b) `LLMBackend.analyze` 签名改为:

```python
    async def analyze(
        self, system_prompt: str, user_message: str, *, max_search_calls: int | None = None
    ) -> str: ...
```

(c) `AnthropicBackend.analyze` 改为:

```python
    async def analyze(
        self, system_prompt: str, user_message: str, *, max_search_calls: int | None = None
    ) -> str:
        budget = self._web_search_max_uses if max_search_calls is None else max_search_calls
        kwargs: dict[str, Any] = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )
        if budget > 0:
            kwargs["tools"] = [
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": budget,
                }
            ]
        response = await self._client.messages.create(**kwargs)
        _log_usage(self._model, response)
        return _extract_text(response)
```

(d) `ToolLoopBackend.analyze` 改为(整体替换原方法):

```python
    async def analyze(
        self, system_prompt: str, user_message: str, *, max_search_calls: int | None = None
    ) -> str:
        budget = self._max_search_calls if max_search_calls is None else max_search_calls
        system = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_message}
        ]
        if budget <= 0:
            # Evidence-sufficient path: one no-tool round trip.
            response = await self._client.messages.create(
                model=self._model, max_tokens=self._max_tokens, system=system, messages=messages,
            )
            _log_usage(self._model, response)
            return _extract_text(response)

        await self._ensure_tavily()
        # Cap iterations at search budget + 1 (the +1 lets the model emit the
        # final assistant turn after its last search).
        for iteration in range(budget + 1):
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                tools=[_TAVILY_SEARCH_TOOL],
                messages=messages,
            )
            _log_usage(self._model, response)
            if getattr(response, "stop_reason", None) != "tool_use":
                return _extract_text(response)
            if iteration >= budget:
                # Model wants another search but the budget is spent — fall
                # through to the forced no-tool turn without searching.
                break
            messages.append({"role": "assistant", "content": response.content})
            tool_results: list[dict[str, Any]] = []
            for block in response.content or []:
                if getattr(block, "type", None) != "tool_use":
                    continue
                query = (block.input or {}).get("query", "") if isinstance(block.input, dict) else ""
                result_text = await self._tavily.search(query) if query else "(empty query)"
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text or "(no results)",
                    }
                )
            if not tool_results:
                return _extract_text(response)
            messages.append({"role": "user", "content": tool_results})
        # Budget exhausted — force one final no-tool turn so the model emits
        # text. `messages` always ends with a user turn here (we break before
        # appending the over-budget tool_use), so the sequence stays valid.
        final = await self._client.messages.create(
            model=self._model, max_tokens=self._max_tokens, system=system, messages=messages,
        )
        _log_usage(self._model, final)
        return _extract_text(final)
```

**行为核对:**旧代码在 budget 用完后会把最后一次 tool_use 也执行搜索(第 `iteration = budget` 轮仍进 Tavily),然后 forced turn。新代码在 `iteration >= budget` 时不再搜索——`test_toolloop_explicit_budget_overrides_constructor_default` 断言 Tavily 只被调 1 次,原有 `test_deepseek_caps_tool_calls_then_forces_final_turn` 只断言第三次调用无 `tools`,两者都能过。这是有意的收紧:预算就是预算。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_report_llm.py tests/test_report_morning.py -q`
Expected: 全部通过(morning 测试证明不传预算的旧路径没变)

- [ ] **Step 5: 提交**

```bash
git add report/llm.py tests/test_report_llm.py
git commit -m "feat(llm): max_search_calls override (0 = no-tool single call) + usage logging"
```

---

### Task 5: `analyst.build_user_message` 加 evidence 块 + 透传预算

**Files:**

- Modify: `report/analyst.py`
- Test: `tests/test_report_analyst.py`(追加/修改)

**Interfaces:**

- Consumes: `backend.analyze(system, user, max_search_calls=...)`(Task 4)
- Produces:

  ```python
  def build_user_message(data: dict, evidence: dict | None = None) -> str
  async def analyze_ticker(backend, system_prompt, data, semaphore, *, evidence=None, max_search_calls=None) -> str
  ```

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_report_analyst.py`:

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_report_analyst.py -q`
Expected: 新增 4 个 FAIL

- [ ] **Step 3: 实现**

`report/analyst.py` 中替换 `build_user_message` 和 `analyze_ticker` 的签名/调用:

````python
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
````

并把函数体里的 `backend.analyze(system_prompt, user_msg)` 改为
`backend.analyze(system_prompt, user_msg, max_search_calls=max_search_calls)`。其余重试逻辑不动。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_report_analyst.py -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add report/analyst.py tests/test_report_analyst.py
git commit -m "feat(report): evidence block in user message; pass search budget through"
```

---

### Task 6: renderer 证据小字行

**Files:**

- Modify: `report/renderer.py`(`_render_ticker_block`、`render_html_document`、`write_report_files`、`INLINE_CSS`)
- Test: `tests/test_report_renderer.py`(追加)

**Interfaces:**

- Produces:

  ```python
  def _render_evidence_meta(meta: dict | None) -> str
  def _render_ticker_block(idx, data, prose_md, evidence_meta: dict | None = None) -> str
  def render_html_document(..., model_label=None, evidence_meta: list[dict | None] | None = None) -> str
  def write_report_files(..., model_label=None, evidence_meta: list[dict | None] | None = None) -> Path
  ```

  `evidence_meta[i]` 形如 `{"news_count": 10, "filings_count": 6, "search_budget": 0}`;`None` 元素或整个参数为 `None` 时不渲染。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_report_renderer.py`:

```python
def test_html_renders_evidence_meta_line_when_given():
    html = renderer.render_html_document(
        market="us", date_iso="2026-09-09",
        enriched=[_fake_data("AXTI")], prose_sections=["### 公司速览\n\nx"],
        truncated=[], generated_at=datetime(2026, 9, 9, 10, 0, tzinfo=HKT),
        evidence_meta=[{"news_count": 10, "filings_count": 6, "search_budget": 0}],
    )
    assert 'class="evidence-meta"' in html
    assert "证据 · 新闻 10 · 公告 6 · 搜索 0" in html


def test_html_omits_evidence_meta_when_absent_or_none():
    common = dict(
        market="us", date_iso="2026-09-09",
        enriched=[_fake_data("AXTI")], prose_sections=["### 公司速览\n\nx"],
        truncated=[], generated_at=datetime(2026, 9, 9, 10, 0, tzinfo=HKT),
    )
    assert "evidence-meta" not in renderer.render_html_document(**common)
    assert "evidence-meta" not in renderer.render_html_document(**common, evidence_meta=[None])


def test_html_evidence_meta_hk_shows_dash_for_filings():
    html = renderer.render_html_document(
        market="hk", date_iso="2026-09-09",
        enriched=[_fake_data("0700.HK")], prose_sections=["### 公司速览\n\nx"],
        truncated=[], generated_at=datetime(2026, 9, 9, 20, 0, tzinfo=HKT),
        evidence_meta=[{"news_count": 4, "filings_count": None, "search_budget": 0}],
    )
    assert "证据 · 新闻 4 · 公告 — · 搜索 0" in html
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_report_renderer.py -q -k evidence_meta`
Expected: FAIL,`TypeError: render_html_document() got an unexpected keyword argument 'evidence_meta'`

- [ ] **Step 3: 实现**

(a) `INLINE_CSS` 里 `.snapshot { margin-bottom: 22px; }` 之后加一条:

```css
.evidence-meta {
  font-size: 11px;
  color: var(--muted);
  margin: -14px 0 18px;
  letter-spacing: 0.01em;
}
```

(b) `_render_ticker_block` 之前加:

```python
def _render_evidence_meta(meta: dict[str, Any] | None) -> str:
    """One muted line under the snapshot: how much pre-fetched evidence the
    LLM had and whether a fallback web search was allowed. Lets the operator
    judge coverage day by day. Empty string when the feature is off."""
    if not meta:
        return ""
    def cell(v: Any) -> str:
        return "—" if v is None else str(v)
    return (
        f'<p class="evidence-meta">证据 · 新闻 {cell(meta.get("news_count"))} · '
        f'公告 {cell(meta.get("filings_count"))} · 搜索 {cell(meta.get("search_budget"))}</p>'
    )
```

(c) `_render_ticker_block(idx, data, prose_md, evidence_meta=None)`:在 `{_render_snapshot(data)}` 之后插入 `{_render_evidence_meta(evidence_meta)}`。

(d) `render_html_document` 增加参数 `evidence_meta: list[dict[str, Any] | None] | None = None`,blocks 改为:

```python
    metas = evidence_meta or [None] * len(enriched)
    blocks = [
        _render_ticker_block(i + 1, d, p, metas[i] if i < len(metas) else None)
        for i, (d, p) in enumerate(zip(enriched, prose_sections))
    ]
```

(e) `write_report_files` 增加同名参数并透传给 `render_html_document(..., evidence_meta=evidence_meta)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_report_renderer.py -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add report/renderer.py tests/test_report_renderer.py
git commit -m "feat(report): evidence coverage line per ticker in HTML"
```

---

### Task 7: `__main__` 接线 + config + prompt

**Files:**

- Modify: `report/__main__.py`
- Modify: `config.toml`(`[report.deepseek]` 段之后)
- Modify: `prompts/canslim_system.md`
- Test: `tests/test_report_main.py`(追加)

**Interfaces:**

- Consumes: `evidence.EvidenceConfig.from_dict`, `evidence.fetch_evidence`, `evidence.search_budget`, `evidence.summarize_for_log`, `evidence.empty_evidence`(Task 1/3);`analyst.analyze_ticker(..., evidence=, max_search_calls=)`(Task 5);`renderer.write_report_files(..., evidence_meta=)`(Task 6)
- Produces(`report/__main__.py`):

  ```python
  def _load_evidence_config(report_cfg: dict) -> evidence.EvidenceConfig
  async def _prefetch_evidence(yf_sym: str, market: str, cfg: evidence.EvidenceConfig, as_of: date) -> dict
  ```

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_report_main.py`:

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_report_main.py -q`
Expected: 新增 4 个 FAIL(`AttributeError: ... '_load_evidence_config'`)

- [ ] **Step 3: 实现 `report/__main__.py`**

(a) import 区:`from report import analyst, enrich, evidence, ranker, renderer`

(b) `_empty_data` 之后加:

```python
def _load_evidence_config(report_cfg: dict) -> evidence.EvidenceConfig:
    return evidence.EvidenceConfig.from_dict((report_cfg or {}).get("evidence"))


async def _prefetch_evidence(
    yf_sym: str, market: str, cfg: evidence.EvidenceConfig, as_of: date
) -> dict:
    """Run the sync yfinance/EDGAR fetch off-loop with a hard timeout. A
    timeout is treated as all-sources-failed so the search fallback can
    kick in; it must never abort the report."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(evidence.fetch_evidence, yf_sym, market, cfg, as_of=as_of),
            timeout=cfg.timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning(f"[evidence] {yf_sym}: prefetch timed out after {cfg.timeout_seconds}s")
        return evidence.empty_evidence(as_of, errors=["timeout"])
    except Exception as e:  # fetch_evidence never raises, but to_thread plumbing might
        logger.warning(f"[evidence] {yf_sym}: prefetch failed: {type(e).__name__}: {e}")
        return evidence.empty_evidence(as_of, errors=[f"prefetch: {type(e).__name__}"])
```

(c) `_run_async` 里,`rs_lookup = ...` 之后加 `ev_cfg = _load_evidence_config(report_cfg)`;把 enrich 循环改为:

```python
    enriched: list[dict] = []
    evidences: list[dict | None] = []
    budgets: list[int | None] = []
    for qualified, group in analyzed_entries:
        exchange, symbol = _split_exchange_ticker(qualified)
        yf_sym = _yf_ticker(symbol, market)
        try:
            data = enrich.fetch_ticker_data(yf_sym, group, exchange, rs_lookup, as_of_date=as_of)
        except Exception as e:
            logger.warning(f"[report] enrich failed for {qualified}: {e}")
            data = _empty_data(yf_sym, group, exchange)
        enriched.append(data)
        if ev_cfg.enabled:
            ev = await _prefetch_evidence(yf_sym, market, ev_cfg, as_of)
            budget = evidence.search_budget(ev, ev_cfg)
            logger.info(
                f"[evidence] {yf_sym}: {evidence.summarize_for_log(ev)} search={budget}"
                + (f" errors={ev['errors']}" if ev.get("errors") else "")
            )
        else:
            ev, budget = None, None
        evidences.append(ev)
        budgets.append(budget)
```

(d) `coroutines` 改为:

```python
        coroutines = [
            analyst.analyze_ticker(
                backend, system_prompt, data, semaphore,
                evidence=ev, max_search_calls=budget,
            )
            for data, ev, budget in zip(enriched, evidences, budgets)
        ]
```

(e) `write_report_files(...)` 增加:

```python
        evidence_meta=[
            None if ev is None else {
                "news_count": ev.get("news_count", 0),
                "filings_count": None if ev.get("filings") is None else ev.get("filings_count", 0),
                "search_budget": budget,
            }
            for ev, budget in zip(evidences, budgets)
        ],
```

(f) 文件顶部 docstring 里 "fan out async Claude calls" 前补一句 "prefetch the evidence bundle per ticker (report/evidence.py),"。

- [ ] **Step 4: `config.toml`**

在 `[report.deepseek]` 段之后、被注释的 `# [report.kimi]` 之前插入:

```toml
[report.evidence]
# 证据包预取(spec: docs/superpowers/specs/2026-09-09-report-evidence-prefetch-design.md)。
# 程序先从 yfinance(新闻 / 分析师 / 财报日历)+ EDGAR(近期公告)拉证据,LLM 只调一次;
# 证据条数(news + filings)低于 min_items_for_no_search 时才允许 fallback_search_calls 次 Tavily。
# enabled = false 回到旧的 tool-loop 行为(上面各后端的 max_search_calls 生效)。
enabled = true
news_max_items = 10
news_max_age_hours = 720        # 30 天
news_summary_chars = 300
filings_days = 60               # 仅 US;HK 无公告源
filings_max_items = 8
analyst_grades_days = 90
search_fallback = true
min_items_for_no_search = 3
fallback_search_calls = 1
timeout_seconds = 20
```

并把 `[report.deepseek]` 段里 `max_search_calls = 2` 那行的注释改为
`max_search_calls = 2   # 仅在 [report.evidence].enabled = false 或盘前 catalyst 路径时生效`。

- [ ] **Step 5: `prompts/canslim_system.md`**

把规则 7 整段替换为:

```markdown
7. **Evidence first.** The user message carries a `Pre-fetched evidence`
   JSON block (recent news with source/date/url, analyst price targets and
   rating counts, recent upgrades/downgrades, next earnings date with
   consensus, and — for US tickers — recent SEC filings with 8-K item codes
   plus a Form 4 count). Ground 竞争力 / 政策 / 新产品 / 风险点 / 市场情绪 in
   that block and cite inline as `(来源, YYYY-MM-DD)`. Do not assert specific
   recent events that appear in neither the evidence nor a web_search result
   from this request. A `web_search` tool is offered **only when the evidence
   was thin**; if it is offered, use it at most once, for the single most
   important gap. If no tool is offered, do not mention searching.
```

规则 9 之后追加:

```markdown
10. **Analyst block is mandatory input for 市场情绪 / 共识.** When
    `evidence.analyst` is non-null, state the mean target vs. current price
    and the rating distribution in that section (numbers in English). When
    `evidence.calendar.next_earnings_date` is non-null, mention it in
    新产品 / 催化剂 as the next dated event. When `evidence.form4_count` ≥ 3,
    mention insider-filing activity under 风险点 (without inventing
    direction — the count alone is not buy/sell).
```

- [ ] **Step 6: 跑全部测试**

Run: `uv run pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 7: 提交**

```bash
git add report/__main__.py config.toml prompts/canslim_system.md tests/test_report_main.py
git commit -m "feat(report): wire evidence prefetch into EOD report; evidence-first prompt"
```

---

### Task 8: 真实运行对比 + 文档

**Files:**

- Modify: `CLAUDE.md`(Report 条目)
- Modify: `README.md`(如有 report 段落,补一句;先 `grep -n "Tavily\|report" README.md` 看有没有)

- [ ] **Step 1: 跑一次真实 EOD 报告到临时目录对比**

先把今天的报告备份,再重跑(报告是幂等覆盖,`--date` 用最近有 `.txt` 的交易日):

```bash
cp output/Reports/PostMarket/2026_09_09_us.html /tmp/claude-501/before_us.html 2>/dev/null || true
time uv run main.py --mode report --market us --date 2026-09-09 2>&1 | grep -E "\[evidence\]|\[llm\]|\[analyst\]|\[report\]"
```

Expected:

- 每票一行 `[evidence] ... search=0`(冷门票可能 `search=1`);
- 每票一行 `[llm] deepseek-v4-pro in=... out=...`,`in` 大约 3k–6k;
- 总耗时明显低于之前的 3.5 分钟(6 票);
- 无 `[analyst] ... failed`。

- [ ] **Step 2: 人工核对 HTML**

```bash
open output/Reports/PostMarket/2026_09_09_us.html
python3 -c "
import re,html
for p in ['/tmp/claude-501/before_us.html','output/Reports/PostMarket/2026_09_09_us.html']:
    try: t=open(p,encoding='utf-8').read()
    except FileNotFoundError: continue
    print(p, '信息不足 x', t.count('信息不足'), '| evidence lines', t.count('evidence-meta'))
"
```

Expected:每票下方出现"证据 · 新闻 N · 公告 M · 搜索 K";"市场情绪"一节引用了目标价与评级分布;`信息不足` 次数不高于改动前。若某票 `search=1` 且 Tavily 被调用,`[llm]` 应出现 2–3 行。

- [ ] **Step 3: 更新 CLAUDE.md**

在 `## Invariants` 的 **Report** 条目末尾追加一句:

```
  **Evidence prefetch** (`report/evidence.py`, `[report.evidence]`): the EOD
  report pre-fetches yfinance news / analyst consensus / earnings calendar +
  EDGAR recent filings per ticker and makes ONE no-tool LLM call; Tavily is
  only offered when `news + filings < min_items_for_no_search`. Each source
  soft-fails independently; prefetch has a hard timeout (empty bundle →
  fallback search). `enabled = false` restores the tool-loop behavior. The
  pre-market catalyst path is NOT on this yet (phase 2 — spec
  `docs/superpowers/specs/2026-09-09-report-evidence-prefetch-design.md`).
```

- [ ] **Step 4: 提交**

```bash
git add CLAUDE.md README.md
git commit -m "docs: evidence prefetch for EOD report (phase 1)"
```

- [ ] **Step 5: 观察期**

阶段 2(盘前路径)在至少 3 个交易日的 EOD 报告确认质量没有下降后再另起计划。观察项:`[evidence]` 行里 `search=1` 的比例、`[llm]` 的 in/out token、报告里 `信息不足` 次数、Tavily 控制台的 credit 消耗。
