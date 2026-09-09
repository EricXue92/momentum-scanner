# CANSLIM / 盘前催化报告:证据包预取 + 单次 LLM 调用 — 设计

日期:2026-09-09
状态:已批准,待实施(阶段 1 → 观察几天 → 阶段 2)

## 背景与问题

现状两条报告路径都是"模型驱动检索":LLM 拿到结构化数据后,通过一个手写的
tool loop 自己决定发几次 `web_search`(Tavily),每次搜索一轮往返,最后再出一轮
生成。实测(2026-09-09 US EOD,6 只票):

- 每票最多 3 次 LLM 往返,总耗时 3.5 分钟;出现过 `empty response` 重试。
- Tavily basic 只返回摘要片段,模型最多 2 条查询,证据面窄。AXTI 一节写
  "缺乏公开的具体目标价共识信息",而 yfinance 免费就有 5 家分析师、均值目标价。
- 现金成本里 Tavily 约占 2/3(月约 1300 credits,超出免费额),LLM token 本身
  每票只有 $0.01 左右。

## 目标

1. 每票只调 **1 次** LLM(无 tool),证据由程序从免费/已接入的结构化源预取。
2. 仅在证据不足时放开少量 Tavily 兜底搜索(条件兜底)。
3. 覆盖两条路径:阶段 1 = EOD CANSLIM 报告;阶段 2 = 盘前 catalyst 报告。
4. 模型保持 `deepseek-v4-pro`,只换检索方式,便于前后对比报告质量。

## 非目标

- Finnhub / HKEX 公告抓取 / Perplexity 等新供应商。
- 证据包落盘 sidecar(可复现用途,以后再说)。
- 盘中(MorningGap5)和 HK morning-gap:本来就没有 catalyst 报告,不新增。
- 改动 renderer 的整体版式。

## 总体架构

```
enrich (yfinance 财务) ──┐
                         ├─> analyst.build_user_message ──> backend.analyze(max_search_calls=budget)
evidence.fetch_evidence ─┘         (structured JSON + evidence JSON)          budget = 0 → 单次无 tool
   news / analyst / calendar / filings                                        budget > 0 → 现有 tool loop
   + evidence.search_budget(evidence, cfg)
```

所有后端(Anthropic 原生 web_search、DeepSeek 等 Anthropic-compat tool loop)
都吃同一份 evidence 块,差别只在"预算 > 0 时怎么搜"。

## 组件 1:`report/evidence.py`(新)

### 公共接口

```python
def fetch_evidence(yf_symbol: str, market: str, cfg: EvidenceConfig) -> dict
def search_budget(evidence: dict, cfg: EvidenceConfig) -> int
def summarize_for_log(evidence: dict) -> str   # "news=10 filings=6 analyst=yes calendar=yes"
```

`EvidenceConfig` 是一个 frozen dataclass,由 `[report.evidence]`(EOD)或
`[morning_gap_catalyst]`(盘前)两个 config 段各自构造,字段:

| 字段 | EOD 默认 | 盘前默认 | 说明 |
|---|---|---|---|
| `enabled` | true | true | false = 不预取、不加 evidence 块、预算走后端默认(完全回到旧行为) |
| `news_max_items` | 10 | 10 | |
| `news_max_age_hours` | 720 (30 天) | 36 | 超龄新闻丢弃 |
| `news_summary_chars` | 300 | 300 | summary 截断 |
| `filings_days` | 60 | 3 | 仅 US |
| `filings_max_items` | 8 | 8 | |
| `analyst_grades_days` | 90 | 7 | 升降评级窗口 |
| `search_fallback` | true | true | false = 预算恒为 0 |
| `min_items_for_no_search` | 3 | 1 | 见 search_budget |
| `fallback_search_calls` | 1 | 2 | |
| `timeout_seconds` | 20 | 20 | 单票预取总超时 |

### 输出结构

```json
{
  "as_of": "2026-09-09",
  "news": [ {"date": "2026-08-27", "title": "...", "source": "Zacks", "summary": "...", "url": "..."} ],
  "analyst": {
    "price_target": {"current": 69.56, "mean": 91.6, "high": 125.0, "low": 55.0},
    "ratings": {"strongBuy": 1, "buy": 3, "hold": 1, "sell": 0, "strongSell": 0},
    "recent_grades": [ {"date": "...", "firm": "...", "action": "up", "from": "Hold", "to": "Buy"} ]
  },
  "calendar": {"next_earnings_date": "2026-10-30", "eps_estimate": 0.308, "revenue_estimate": 66004800},
  "filings": [ {"form": "8-K", "date": "2026-08-13", "items": "2.02,9.01", "description": "...", "url": "..."} ],
  "form4_count": 2,
  "news_count": 10,
  "filings_count": 6,
  "errors": ["filings: HTTP 503"]
}
```

- 某一源失败 → 该字段为 `null`(列表源为 `[]`),错误文本进 `errors`,
  **不影响其它源**。四个源都失败也返回一个合法 dict(计数全 0)。
- `news_count` / `filings_count` 是过滤后的条数,供 `search_budget` 用。
- HK:`filings` 恒为 `null`,`form4_count` 为 0;其余三源照常(yfinance 对
  `XXXX.HK` 有新闻和分析师数据)。

### 各源实现

**news** — `yf.Ticker(sym).news`。每条取 `content.title` / `content.summary` /
`content.pubDate` / `content.provider.displayName` / `content.canonicalUrl.url`
(缺则 `clickThroughUrl.url`)。字段缺失按 `null` 处理,不抛;`pubDate` 解析失败的
条目丢弃。按时间倒序,过滤超龄,截 `news_max_items`。

**analyst** — `analyst_price_targets`(dict)、`recommendations_summary`(取
period `0m` 那一行)、`upgrades_downgrades`(DataFrame,按 GradeDate 过滤
`analyst_grades_days`,最多 5 条,倒序)。三个属性各自 try/except,任一失败该子键
为 `null`;全失败则 `analyst` 为 `null`。

**calendar** — `Ticker.calendar`:`Earnings Date` 列表第一个元素、
`Earnings Average`、`Revenue Average`。

**filings**(仅 `market == "us"`)— 复用 `report/edgar.py` 的 `_get_cik` /
`_http_get_json` / JSON 缓存,新增 `fetch_recent_filings(ticker, days, max_items)`:
拉 `https://data.sec.gov/submissions/CIK{cik:010d}.json`,缓存到
`output/state/edgar_cache/submissions_CIK{cik}.json`,TTL 1 天(盘前模式 TTL
6 小时,因为当天新挂的 8-K 就是催化剂)。从 `filings.recent` 并行数组取
`form / filingDate / items / primaryDocDescription / accessionNumber /
primaryDocument`,保留表单集合
`{8-K, 6-K, 10-Q, 10-K, 10-K/A, 10-Q/A, S-1, S-3, 424B*, SC 13D*, SC 13G*}`,
按日期过滤 `filings_days`,截 `filings_max_items`;另外统计窗口内 `4` 表单的
数量为 `form4_count`(内部人交易活跃度,不列明细)。URL 拼成
`https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession 去横线}/{primaryDocument}`。
`report/cleanup.py` 不需要改:`edgar_cache/` 本来就在不动名单里,缓存靠 TTL 覆盖。

### 超时

`fetch_evidence` 本身是同步函数。调用方用
`asyncio.wait_for(asyncio.to_thread(fetch_evidence, ...), timeout_seconds)`;
超时 → 视为四源全失败(空 evidence,`errors=["timeout"]`),日志 WARNING,
兜底搜索按空证据计算。这样盘前子进程不会被 yfinance 卡死。

### `search_budget`

```
if not cfg.enabled:            return None      # None = 让后端用自己的默认值(旧行为)
if not cfg.search_fallback:    return 0
items = news_count + filings_count
return 0 if items >= cfg.min_items_for_no_search else cfg.fallback_search_calls
```

## 组件 2:LLM 后端接口(`report/llm.py`)

- `LLMBackend.analyze(system_prompt, user_message, *, max_search_calls: int | None = None)`。
  - `None` → 用构造函数里的 `max_search_calls` / `web_search_max_uses`(现有行为,
    盘前路径阶段 1 期间不改一行也能跑)。
  - `0` → **ToolLoopBackend** 发一次请求,不传 `tools`,直接返回文本;
    **AnthropicBackend** 不挂 `web_search` 工具。
  - `n > 0` → 现有 loop,上限改为 `n`。
- 每次 `messages.create` 返回后,后端以 INFO 记一行
  `[llm] <model> in=<input_tokens> cache_hit=<cache_read_input_tokens|-> out=<output_tokens>`;
  Anthropic 后端另记 `searches=<web_search_requests>`。这是用户衡量 token 成本的
  唯一手段,必须做。
- `_TAVILY_SEARCH_TOOL.description` 改为"仅当预取证据不足时使用,最多 N 次"由
  system prompt 表述;工具定义本身不变。

## 组件 3:EOD 路径(阶段 1)

### `report/__main__.py`

enrich 循环内每票追加:

```python
ev = await asyncio.wait_for(asyncio.to_thread(evidence.fetch_evidence, yf_sym, market, ev_cfg), ev_cfg.timeout_seconds)
budget = evidence.search_budget(ev, ev_cfg)
logger.info(f"[evidence] {yf_sym}: {evidence.summarize_for_log(ev)} search={budget}")
```

(`_run_async` 已是 async,enrich 本身仍顺序执行以避开 yfinance 限速。)
`enabled = false` 时跳过预取,`ev = None`,`budget = None`。

### `report/analyst.py`

- `build_user_message(data, evidence=None)`:两个 JSON 块都改为紧凑序列化
  (`separators=(",", ":")`,不 indent)。evidence 非 None 时在结构化块之后追加:

  ````
  Pre-fetched evidence (news / analyst consensus / earnings calendar / SEC filings; use these as
  primary sources for the qualitative sections and cite as (source, date)):

  ```json
  {...evidence...}
  ```
  ````

  最后一段的提示改为:evidence 存在时写"Ground every qualitative section in the
  evidence above. Only call `web_search` if the tool is offered in this request.";
  不存在时保留现在的"Use the web_search tool sparingly (≤2 calls)"。
- `analyze_ticker(backend, system_prompt, data, semaphore, *, evidence=None, max_search_calls=None)`
  → 透传给 `backend.analyze`。重试逻辑不变。

### `prompts/canslim_system.md`

- 规则 7 改为:定性各节以 JSON 里的 evidence 为主要依据,引用格式
  `(来源, 日期)`,不得引用 evidence 之外的具体新闻,除非来自本次请求允许的
  `web_search` 结果;**只有当请求提供了 `web_search` 工具时才可以搜,且最多 1 次**。
- 新增规则:`市场情绪 / 共识` 一节在 `analyst` 非 null 时必须使用其目标价与评级
  分布;`新产品 / 催化剂` 一节在 `calendar.next_earnings_date` 非 null 时必须提及。
- 规则 8(语种)保留,适用于兜底搜索。

### `report/renderer.py`

每只票的结构化表格下方加一行小字(muted):
`证据 · 新闻 {news_count} · 公告 {filings_count} · 搜索 {budget}`。
evidence 为 None(功能关闭)时不渲染这一行。渲染函数签名增加可选
`evidence_meta: list[dict | None]`,与 `enriched` 等长。

### `config.toml`

```toml
[report.evidence]
enabled = true
news_max_items = 10
news_max_age_hours = 720
news_summary_chars = 300
filings_days = 60
filings_max_items = 8
analyst_grades_days = 90
search_fallback = true
min_items_for_no_search = 3
fallback_search_calls = 1
timeout_seconds = 20
```

`[report.deepseek].max_search_calls` 保留,含义变为"调用方不传预算时的默认值"
(阶段 1 期间即盘前路径)。

## 组件 4:盘前路径(阶段 2)

前提:阶段 1 上线并观察至少 3 个交易日的 EOD 报告后再做。

### 约束调整

旧约束"catalyst 子进程 MUST NOT call Futu / yfinance"改为:
**MUST NOT call Futu;yfinance 只允许在 `evidence.fetch_evidence` 内使用,
且受 `timeout_seconds` 约束、soft-fail。** 理由:子进程在扫描完成后才启动,
不与扫描抢 yfinance 配额;超时保护保证子进程不会挂住。
需同步更新 CLAUDE.md 的 Catalyst report 条目,并在
`2026-06-03-morning-gap-catalyst-report-design.md` 的 Open invariants 处加一行
指向本 spec。

### `report/morning.py`

- `_run_async` 里,在 `asyncio.gather` 之前顺序预取每票 evidence(≤10 票,
  每票 ≤20s 超时),模式用盘前默认(36 小时新闻、3 天 filings、7 天评级)。
- `build_user_message(entry, evidence=None)`:追加 evidence 块,格式同 EOD。
- `analyze_catalyst(...)` 透传 `max_search_calls=budget`。
- 预算规则:`news_count + filings_count == 0` → `fallback_search_calls`(2),
  否则 0。

### `prompts/morning_gap_catalyst_system.md`

- 规则 5 改为:优先从 evidence 里找催化剂;`证据` 一节的链接直接用 evidence
  里的 `url`;只有当请求提供 `web_search` 工具时才搜,最多 2 次。
- 规则 6(不得编造)不变。

### `config.toml`

`[morning_gap_catalyst]` 新增同名字段(`evidence_enabled` 以及上表"盘前默认"
一列),`max_search_calls = 3` 保留为 `evidence_enabled = false` 时的旧行为。

### `report/morning_renderer.py`

每票加同样的证据小字行。

## 错误处理总表

| 情况 | 行为 |
|---|---|
| 某一源异常 | 该字段 null / [],记 errors,继续 |
| 预取超时 | 空 evidence,WARNING,按空证据算预算 |
| EDGAR 找不到 CIK | filings null,不算错误 |
| 预算 0 但模型仍输出 tool_use | 无 tools 时 API 不会返回 tool_use;若返回,`_extract_text` 已会丢弃 tool_use 块 |
| `enabled = false` | 与改动前逐字节一致的请求(旧 prompt 提示语、indent JSON 除外——紧凑 JSON 对两种模式都生效) |

## 测试

- `tests/test_report_evidence.py`(新):mock `yf.Ticker`(属性用 MagicMock /
  DataFrame fixture)和 `edgar._http_get_json`:
  - 新闻过滤超龄、截断、缺字段容错、pubDate 非法丢弃;
  - analyst 三属性各自失败的组合;
  - filings 表单集合过滤、Form 4 计数、URL 拼接、HK 为 null;
  - 单源异常不影响其它源;`search_budget` 四种分支;`summarize_for_log`。
- `tests/test_report_analyst.py`:带/不带 evidence 的消息体;紧凑 JSON;
  `max_search_calls` 透传。
- `tests/test_report_llm.py`:预算 0 时 ToolLoopBackend 只发 1 次且请求无
  `tools`;AnthropicBackend 预算 0 时无 web_search 工具;预算 1 时 loop 上限为 1;
  `None` 时沿用构造默认。
- `tests/test_report_main.py`:evidence 关闭时不调用预取;超时路径。
- `tests/test_report_renderer.py`:证据行渲染/不渲染。
- 阶段 2 对应 `test_report_morning.py` / `test_report_morning_renderer.py`。
- 手工验证:`uv run main.py --mode report --market us --date <最近交易日>`,
  对比新旧 HTML 的 `信息不足` 出现次数、`[llm]` 日志的 token 数、总耗时。

## 预期效果

| 指标 | 现状 | 预期 |
|---|---|---|
| 每票 LLM 往返 | 最多 3 | 1(兜底时 2) |
| 每票输入 token | ~12k | ~4k |
| 每票 LLM 成本(v4-pro) | ~$0.01 | ~$0.005 |
| Tavily 月用量 | ~1300 credits | 只在冷门票触发,预计 < 200 |
| 每票耗时 | 30–90 s | 10–20 s |

## 实施顺序

1. `evidence.py` + 测试(纯新增,不影响现网)。
2. `llm.py` 接口扩展 + 测试(默认值保证零行为变化)。
3. `analyst.py` / `__main__.py` / prompt / config / renderer 接线 + 测试。
4. 手工跑一次 EOD 报告对比;CLAUDE.md 更新 Report 条目。
5. 观察 ≥3 个交易日。
6. 阶段 2:`morning.py` / 盘前 prompt / config / CLAUDE.md 约束调整 + 测试。
