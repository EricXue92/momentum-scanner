# ETF 成分股 1M 相对强度前 5 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `<date>_ETF_rs_3m.txt` / `_ETF_rs_1m.txt` 每只 ETF 的行下面追加一行
`   1M强: A > B > C > D > E`，列出该 ETF 美股成分股中 1M 相对强度最强的 5 只。

**Architecture:** 新模块 `etf_holdings_rs.py` 负责"成分股来源链 (今日缓存 →
stockanalysis → 旧缓存走回 → yfinance 前十大) + 美股过滤 + 1M 打分 + 续行字符串"，
对外一个入口 `compute_extras`。`etf_rs.run_etf_rs` 在写文件前以软步骤调用它，
`write_ranking(extras=)` 原样追加续行；`read_previous_ranks` 跳过缩进行。云端
Pages 构建传 `holdings_rs=False` 关掉这一步。

**Tech Stack:** Python 3.12 / pandas / yfinance / `urllib.request` (不加新依赖) / pytest。

**Spec:** `docs/superpowers/specs/2026-10-31-etf-holdings-1m-rs-design.md`

## Global Constraints

- 软步骤：任何异常只导致两个 `.txt` 不带续行，EOD 退出码不变。
- 不新增第三方依赖；HTML 解析用 `re` 或 `html.parser`。
- stockanalysis：每只 ETF 每天最多 1 个 GET，间隔 `request_delay_sec` (0.8s)，
  timeout 15s，浏览器 UA；同日重跑读缓存不上网。
- 续行格式固定：3 空格缩进 + `1M强: ` + ` > ` 连接，最多 `top_n` (5) 只；
  兜底来源加后缀 ` (前十大)`；空列表不写续行。
- 美股 symbol 正则 `^[A-Z]{1,5}(\.[A-Z])?$`；yfinance 请求时 `.` → `-`，显示保留 `.`。
- 1M 阶梯 = `us_rs_3m.WEIGHTS_1M_BARS`，分数 = 股票分 − SPY 分；无 SPY 用绝对分。
- 缓存 `output/state/etf_holdings_<YYYY_MM_DD>.json`，只存 stockanalysis 来源；
  走回上限 `cache_max_age_days` (7)；清理窗口 8 天。
- 测试不上网：网络层函数全部可注入 / monkeypatch。

## Review Focus

1. stockanalysis 返回 200 但表格 0 行 (改版 / Pro 墙) → 视为失败且**不写入缓存**，不能让空列表把前十大兜底挡掉。(Task 1 `test_parse_table_without_rows_is_none`, Task 2 `test_empty_parse_result_not_cached`)
2. 今日缓存文件损坏 (非 JSON / 结构不对) → 当作不存在继续来源链，不抛。(Task 2 `test_corrupt_cache_is_ignored`)
3. 兜底 `yf_top10` 本身抛异常 (DXYZ 这类非标准 ETF) → 该 ETF 缺席，其余 ETF 不受影响。(Task 2 `test_yf_top10_exception_only_drops_that_etf`)
4. 成分股 K 线批次整体为空 (Yahoo 节流) → `compute_extras` 返回 `{}` 并 WARNING，两个 `.txt` 照常写出。(Task 3 `test_compute_extras_empty_klines_returns_empty`, Task 4 `test_run_writes_files_without_extras_when_step_raises`)
5. 带续行的旧文件被 `read_previous_ranks` 读取时，`1M强:` 不能被当成 ticker 计入名次。(Task 4 `test_read_previous_ranks_ignores_indented_continuation_lines`)

---

### Task 1: 解析与美股过滤 (`etf_holdings_rs.py` 纯函数)

**Files:**
- Create: `etf_holdings_rs.py`
- Create: `tests/fixtures/stockanalysis_xlf_holdings.html` (从
  `/private/tmp/claude-501/-Users-xue-momentum-scanner/f71e4529-6e03-491a-8365-240b47976bd4/scratchpad/sa/xlf_table_fixture.html`
  复制；它是 2026-10-31 真实 XLF 页面裁剪后只剩 `<table id="main-table">` 的版本，
  第 1 行 `BRK.B`、第 2 行 `JPM`，共 25 行)
- Create: `tests/test_etf_holdings_rs.py`

**Interfaces:**
- Produces:
  - `US_SYMBOL_RE: re.Pattern` = `^[A-Z]{1,5}(\.[A-Z])?$`
  - `parse_holdings_html(html: str) -> list[str] | None` — Symbol 列按页面顺序；无表格 / 无数据行 → `None`
  - `is_us_symbol(sym: str) -> bool`
  - `to_yf_symbol(sym: str) -> str` (`BRK.B` → `BRK-B`)，`from_yf_symbol(sym: str) -> str` (反向)
  - `Constituents = NamedTuple("Constituents", [("symbols", list[str]), ("source", str)])`

- [ ] **Step 1: 写失败测试**

```python
FIX = Path(__file__).parent / "fixtures" / "stockanalysis_xlf_holdings.html"

def test_parse_real_page_gives_25_symbols_in_page_order():
    syms = etf_holdings_rs.parse_holdings_html(FIX.read_text())
    assert len(syms) == 25
    assert syms[:2] == ["BRK.B", "JPM"]
    assert all(s == s.strip() and "<" not in s for s in syms)

def test_parse_table_without_rows_is_none():
    html = '<table id="main-table"><thead><tr><th>No.</th><th>Symbol</th></tr></thead><tbody></tbody></table>'
    assert etf_holdings_rs.parse_holdings_html(html) is None

def test_parse_page_without_table_is_none():
    assert etf_holdings_rs.parse_holdings_html("<html><body>Subscribe</body></html>") is None

def test_parse_unescapes_and_strips_anchor():
    html = ('<table id="main-table"><tr><th>No.</th><th>Symbol</th></tr>'
            '<tr><td>1</td><td><a href="/stocks/brk.b/"> BRK.B </a></td><td>Berkshire &amp; Co</td></tr></table>')
    assert etf_holdings_rs.parse_holdings_html(html) == ["BRK.B"]

@pytest.mark.parametrize("sym,ok", [
    ("NVDA", True), ("BRK.B", True), ("A", True),
    ("TLV:NXSN", False), ("688836.SZ", False), ("SHE:002472", False),
    ("278A.T", False), ("-", False), ("", False), ("brk.b", False),
])
def test_is_us_symbol(sym, ok):
    assert etf_holdings_rs.is_us_symbol(sym) is ok

def test_yf_symbol_round_trip():
    assert etf_holdings_rs.to_yf_symbol("BRK.B") == "BRK-B"
    assert etf_holdings_rs.from_yf_symbol("BRK-B") == "BRK.B"
    assert etf_holdings_rs.to_yf_symbol("NVDA") == "NVDA"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_etf_holdings_rs.py -q`
Expected: FAIL，`ModuleNotFoundError: etf_holdings_rs`

- [ ] **Step 3: 实现**

`etf_holdings_rs.py` 模块 docstring 按 `etf_rs.py` 风格写清目的 / 来源链 / 软失败。
`parse_holdings_html`：定位第一个 `<table`…`</table>`，表头里找 `Symbol` 列序号
(找不到时取第 2 列)，对每个 `<tr>` 取该列 `<td>`，`re.sub(r"<[^>]+>", "", …)` 去标签、
`html.unescape`、strip；跳过空值；0 行 → `None`。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_etf_holdings_rs.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add etf_holdings_rs.py tests/test_etf_holdings_rs.py tests/fixtures/stockanalysis_xlf_holdings.html
git commit -m "feat(etf-holdings-rs): parse stockanalysis holdings table, US symbol filter"
```

---

### Task 2: 成分股来源链与缓存

**Files:**
- Modify: `etf_holdings_rs.py`
- Test: `tests/test_etf_holdings_rs.py`

**Interfaces:**
- Consumes: Task 1 的 `parse_holdings_html`、`Constituents`。
- Produces:
  - `HOLDINGS_URL = "https://stockanalysis.com/etf/{slug}/holdings/"`
  - `cache_path(output_dir: Path, day: date) -> Path` → `output_dir/"state"/f"etf_holdings_{day:%Y_%m_%d}.json"`
  - `load_cache(output_dir: Path, day: date) -> dict[str, Constituents]`（文件缺失 / 损坏 → `{}`）
  - `save_cache(output_dir: Path, day: date, data: dict[str, Constituents]) -> None`（只写 `source == "stockanalysis"` 的条目；写失败 WARNING）
  - `_fetch_page(url: str, timeout: float = 15.0) -> str | None`（`urllib.request`，UA 头，非 200 / 异常 → `None` + WARNING）
  - `_yf_top_holdings(etf: str) -> list[str] | None`（`yf.Ticker(etf).funds_data.top_holdings.index`；异常 → `None`）
  - `fetch_constituents(etfs: list[str], today: date, output_dir: Path, cfg: dict, *, fetch_page=None, yf_top10=None, sleep=time.sleep) -> dict[str, Constituents]`
    — `fetch_page` / `yf_top10` 为 `None` 时用上面两个默认实现；`source` 取值
    `"stockanalysis"` / `f"stockanalysis-cache:{YYYY-MM-DD}"` / `"yf_top10"`。
  - JSON 格式：`{"XLK": {"symbols": [...], "source": "stockanalysis"}}`

- [ ] **Step 1: 写失败测试**

```python
def _cfg(**o):
    base = {"enabled": True, "top_n": 5, "cache_max_age_days": 7, "request_delay_sec": 0}
    base.update(o); return base

TODAY = date(2026, 10, 31)
PAGE = '<table id="main-table"><tr><th>No.</th><th>Symbol</th></tr><tr><td>1</td><td>AAA</td></tr><tr><td>2</td><td>TLV:ZZZ</td></tr></table>'

def test_today_cache_hit_skips_network(tmp_path):
    etf_holdings_rs.save_cache(tmp_path, TODAY, {"XLK": etf_holdings_rs.Constituents(["AAA"], "stockanalysis")})
    calls = []
    out = etf_holdings_rs.fetch_constituents(["XLK"], TODAY, tmp_path, _cfg(),
                                             fetch_page=lambda u: calls.append(u) or PAGE, yf_top10=lambda e: None)
    assert calls == [] and out["XLK"].symbols == ["AAA"] and out["XLK"].source == "stockanalysis"

def test_live_fetch_is_cached_with_raw_symbols(tmp_path):
    out = etf_holdings_rs.fetch_constituents(["XLK"], TODAY, tmp_path, _cfg(),
                                             fetch_page=lambda u: PAGE, yf_top10=lambda e: None)
    assert out["XLK"].symbols == ["AAA", "TLV:ZZZ"]      # 海外保留在缓存里, 过滤在排名时做
    assert etf_holdings_rs.load_cache(tmp_path, TODAY)["XLK"].symbols == ["AAA", "TLV:ZZZ"]

def test_fetch_url_uses_lowercase_slug(tmp_path):
    seen = []
    etf_holdings_rs.fetch_constituents(["BRK.B"], TODAY, tmp_path, _cfg(),
                                       fetch_page=lambda u: seen.append(u) or PAGE, yf_top10=lambda e: None)
    assert seen == ["https://stockanalysis.com/etf/brk.b/holdings/"]

def test_fetch_failure_walks_back_to_recent_cache(tmp_path):
    etf_holdings_rs.save_cache(tmp_path, date(2026, 10, 27), {"XLK": etf_holdings_rs.Constituents(["OLD"], "stockanalysis")})
    out = etf_holdings_rs.fetch_constituents(["XLK"], TODAY, tmp_path, _cfg(),
                                             fetch_page=lambda u: None, yf_top10=lambda e: ["T1"])
    assert out["XLK"].symbols == ["OLD"]
    assert out["XLK"].source == "stockanalysis-cache:2026-10-27"

def test_cache_older_than_max_age_is_not_used(tmp_path):
    etf_holdings_rs.save_cache(tmp_path, date(2026, 10, 23), {"XLK": etf_holdings_rs.Constituents(["OLD"], "stockanalysis")})
    out = etf_holdings_rs.fetch_constituents(["XLK"], TODAY, tmp_path, _cfg(cache_max_age_days=7),
                                             fetch_page=lambda u: None, yf_top10=lambda e: ["T1"])
    assert out["XLK"].symbols == ["T1"] and out["XLK"].source == "yf_top10"

def test_yf_top10_is_not_cached(tmp_path):
    etf_holdings_rs.fetch_constituents(["XLK"], TODAY, tmp_path, _cfg(),
                                       fetch_page=lambda u: None, yf_top10=lambda e: ["T1"])
    assert "XLK" not in etf_holdings_rs.load_cache(tmp_path, TODAY)

def test_all_sources_fail_means_etf_absent(tmp_path):
    out = etf_holdings_rs.fetch_constituents(["XLK"], TODAY, tmp_path, _cfg(),
                                             fetch_page=lambda u: None, yf_top10=lambda e: None)
    assert out == {}

def test_empty_parse_result_not_cached(tmp_path):
    empty = '<table id="main-table"><tr><th>No.</th><th>Symbol</th></tr></table>'
    out = etf_holdings_rs.fetch_constituents(["XLK"], TODAY, tmp_path, _cfg(),
                                             fetch_page=lambda u: empty, yf_top10=lambda e: ["T1"])
    assert out["XLK"].source == "yf_top10"
    assert not etf_holdings_rs.cache_path(tmp_path, TODAY).exists() or "XLK" not in etf_holdings_rs.load_cache(tmp_path, TODAY)

def test_corrupt_cache_is_ignored(tmp_path):
    p = etf_holdings_rs.cache_path(tmp_path, TODAY); p.parent.mkdir(parents=True); p.write_text("{not json")
    out = etf_holdings_rs.fetch_constituents(["XLK"], TODAY, tmp_path, _cfg(),
                                             fetch_page=lambda u: PAGE, yf_top10=lambda e: None)
    assert out["XLK"].source == "stockanalysis"

def test_yf_top10_exception_only_drops_that_etf(tmp_path):
    def boom(e):
        if e == "DXYZ": raise RuntimeError("no funds_data")
        return ["T1"]
    out = etf_holdings_rs.fetch_constituents(["DXYZ", "XLK"], TODAY, tmp_path, _cfg(),
                                             fetch_page=lambda u: None, yf_top10=boom)
    assert set(out) == {"XLK"}

def test_sleep_between_live_requests_only(tmp_path):
    slept = []
    etf_holdings_rs.fetch_constituents(["A", "B"], TODAY, tmp_path, _cfg(request_delay_sec=0.8),
                                       fetch_page=lambda u: PAGE, yf_top10=lambda e: None, sleep=slept.append)
    assert slept == [0.8]          # 两次在线请求之间 1 次, 第一次前不睡
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_etf_holdings_rs.py -q`
Expected: FAIL，`AttributeError: ... has no attribute 'fetch_constituents'`

- [ ] **Step 3: 实现**

`fetch_constituents` 逻辑：`today_cache = load_cache(today)`；对每只 ETF 依次
(1) 今日缓存 (2) `fetch_page(HOLDINGS_URL.format(slug=etf.lower()))` → `parse_holdings_html`，
非 `None` 则记为 `stockanalysis` 并加入待写缓存；在线请求之间 `sleep(delay)`
(3) `for k in 1..cache_max_age_days: load_cache(today - k)`，命中且 `source == "stockanalysis"`
则 `source = f"stockanalysis-cache:{day.isoformat()}"` (4) `yf_top10(etf)`，异常用
try/except 包住 → WARNING。循环结束后 `save_cache(today, merged)`：今日缓存原有
条目 + 本次新抓的，只含 `stockanalysis` 来源。每只 ETF 一条 INFO
`[ETF 持仓RS] XLK: 25 symbols (stockanalysis)`。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_etf_holdings_rs.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add etf_holdings_rs.py tests/test_etf_holdings_rs.py
git commit -m "feat(etf-holdings-rs): constituent source chain with daily cache and top-10 fallback"
```

---

### Task 3: 1M 打分、排名与续行字符串

**Files:**
- Modify: `etf_holdings_rs.py`
- Test: `tests/test_etf_holdings_rs.py`

**Interfaces:**
- Consumes: Task 1/2；`us_rs_3m._score_from_kline_bars(df, us_rs_3m.WEIGHTS_1M_BARS) -> (score|None, reason)`；
  `us_rs_3m.fetch_us_klines_yf(tickers, period=, fill_last_close=)`。
- Produces:
  - `_fetch_klines(symbols: list[str]) -> dict[str, pd.DataFrame]` = `us_rs_3m.fetch_us_klines_yf(symbols, period="3mo", fill_last_close=True)`（测试 monkeypatch 点）
  - `rank_holdings(constituents: dict[str, Constituents], klines: dict[str, pd.DataFrame], benchmark_kline: pd.DataFrame | None, top_n: int) -> dict[str, list[str]]`
    — `klines` 以**显示代码**为键 (调用方已 `from_yf_symbol`)；每 ETF 只保留 `is_us_symbol` 的、打分 `reason == "ok"` 的，按 (股票分 − SPY 分) 降序取前 `top_n`；空列表的 ETF 不出现在结果里
  - `format_extra(top: list[str], source: str) -> str` → `"1M强: A > B"`，`source == "yf_top10"` 时追加 `" (前十大)"`
  - `compute_extras(cfg: dict, etfs: list[str], benchmark_kline: pd.DataFrame | None, today: date, output_dir: Path) -> dict[str, str]`
    — 编排：`enabled` 为 False → `{}`；`fetch_constituents` → 去重美股 symbol → `_fetch_klines(to_yf_symbol…)` → 键映射回显示代码 → `rank_holdings` → `{etf: format_extra(...)}`；K 线为空 → WARNING + `{}`

- [ ] **Step 1: 写失败测试**

```python
def _kline(ret_pct: float, n: int = 40) -> pd.DataFrame:   # 同 tests/test_etf_rs.py 的平坦+末根跳涨
    closes = [100.0] * (n - 1) + [100.0 * (1 + ret_pct / 100)]
    return pd.DataFrame({"time_key": pd.bdate_range(end="2026-10-30", periods=n), "close": closes})

C = etf_holdings_rs.Constituents

def test_rank_holdings_strongest_first_relative_to_benchmark_top_n():
    cons = {"XLK": C(["A", "B", "C", "D", "E", "F", "TLV:X"], "stockanalysis")}
    kl = {s: _kline(r) for s, r in zip("ABCDEF", (1, 6, 3, 5, 2, 4))}
    kl["TLV:X"] = _kline(99)
    out = etf_holdings_rs.rank_holdings(cons, kl, _kline(0), top_n=5)
    assert out == {"XLK": ["B", "D", "F", "C", "E"]}      # 海外的 TLV:X 不参与; A 第 6 被截掉

def test_rank_holdings_fewer_than_top_n_lists_what_exists():
    cons = {"XLK": C(["A", "B"], "stockanalysis")}
    out = etf_holdings_rs.rank_holdings(cons, {"A": _kline(1), "B": _kline(2)}, _kline(0), 5)
    assert out == {"XLK": ["B", "A"]}

def test_rank_holdings_short_history_and_missing_klines_excluded():
    cons = {"XLK": C(["A", "B", "Z"], "stockanalysis")}
    out = etf_holdings_rs.rank_holdings(cons, {"A": _kline(1), "B": _kline(9, n=15)}, _kline(0), 5)
    assert out == {"XLK": ["A"]}

def test_rank_holdings_no_scorable_names_omits_etf():
    assert etf_holdings_rs.rank_holdings({"XLK": C(["TLV:X"], "stockanalysis")}, {}, _kline(0), 5) == {}

def test_rank_holdings_without_benchmark_uses_absolute_scores():
    cons = {"XLK": C(["A", "B"], "stockanalysis")}
    assert etf_holdings_rs.rank_holdings(cons, {"A": _kline(1), "B": _kline(2)}, None, 5) == {"XLK": ["B", "A"]}

def test_format_extra():
    assert etf_holdings_rs.format_extra(["A", "B"], "stockanalysis") == "1M强: A > B"
    assert etf_holdings_rs.format_extra(["A"], "stockanalysis-cache:2026-10-27") == "1M强: A"
    assert etf_holdings_rs.format_extra(["A", "B"], "yf_top10") == "1M强: A > B (前十大)"

def test_compute_extras_end_to_end_maps_dot_symbols(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_holdings_rs, "fetch_constituents",
                        lambda *a, **k: {"XLF": C(["BRK.B", "JPM"], "stockanalysis")})
    asked = []
    monkeypatch.setattr(etf_holdings_rs, "_fetch_klines",
                        lambda syms: asked.extend(syms) or {"BRK-B": _kline(5), "JPM": _kline(1)})
    out = etf_holdings_rs.compute_extras(_cfg(), ["XLF"], _kline(0), TODAY, tmp_path)
    assert sorted(asked) == ["BRK-B", "JPM"]
    assert out == {"XLF": "1M强: BRK.B > JPM"}

def test_compute_extras_empty_klines_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_holdings_rs, "fetch_constituents", lambda *a, **k: {"XLF": C(["JPM"], "stockanalysis")})
    monkeypatch.setattr(etf_holdings_rs, "_fetch_klines", lambda syms: {})
    assert etf_holdings_rs.compute_extras(_cfg(), ["XLF"], _kline(0), TODAY, tmp_path) == {}

def test_compute_extras_disabled_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_holdings_rs, "fetch_constituents", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    assert etf_holdings_rs.compute_extras(_cfg(enabled=False), ["XLF"], None, TODAY, tmp_path) == {}
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_etf_holdings_rs.py -q`
Expected: FAIL，`AttributeError: ... 'rank_holdings'`

- [ ] **Step 3: 实现**

`rank_holdings`：SPY 分 = `_score_from_kline_bars(benchmark_kline, WEIGHTS_1M_BARS)[0]`
(None 时记 0 并 WARNING 一次)。`compute_extras` 日志：每 ETF 一行
`成分 N (来源) / 美股 M / 打分 K / 前{top_n}: …`，末尾汇总 `K 线覆盖 x/y`。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_etf_holdings_rs.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add etf_holdings_rs.py tests/test_etf_holdings_rs.py
git commit -m "feat(etf-holdings-rs): 1M ranking within each ETF and continuation-line formatting"
```

---

### Task 4: 接入 `etf_rs` 与云端构建

**Files:**
- Modify: `etf_rs.py` (`read_previous_ranks` ~L194-233, `write_ranking` ~L256-284, `_write_1m_ranking` ~L297, `run_etf_rs` ~L330-399)
- Modify: `scripts/build_etf_rs_page.py:46`
- Test: `tests/test_etf_rs.py`, `tests/test_build_etf_rs_page.py`

**Interfaces:**
- Consumes: `etf_holdings_rs.compute_extras(cfg, etfs, benchmark_kline, today, output_dir) -> dict[str, str]`
- Produces:
  - `write_ranking(table, names, holdings, output_dir, today, prev_ranks=None, stem=_STEM, extras: dict[str, str] | None = None)` — `extras.get(t)` 非空时在该行后追加 `"   " + extras[t]`
  - `_write_1m_ranking(..., today, extras: dict[str, str] | None = None)`
  - `run_etf_rs(cfg, output_dir, today, holdings_rs: bool = True)`
  - `read_previous_ranks`：以空白开头的行跳过

- [ ] **Step 1: 写失败测试 (tests/test_etf_rs.py)**

```python
def test_write_ranking_appends_indented_extra_line(tmp_path):
    table = pd.DataFrame({"raw_score": [0.2, 0.1], "rs_percentile": [99, 50]}, index=["AAA", "BBB"])
    out = etf_rs.write_ranking(table, {"AAA": "甲", "BBB": "乙"}, {"AAA": "X、Y"}, tmp_path, date(2026, 10, 31),
                               extras={"AAA": "1M强: X > Y"})
    assert out.read_text() == "1. AAA - 甲 | X、Y\n   1M强: X > Y\n2. BBB - 乙\n"

def test_write_ranking_without_extras_is_unchanged(tmp_path):
    table = pd.DataFrame({"raw_score": [0.2], "rs_percentile": [99]}, index=["AAA"])
    out = etf_rs.write_ranking(table, {"AAA": "甲"}, {}, tmp_path, date(2026, 10, 31), extras={})
    assert out.read_text() == "1. AAA - 甲\n"

def test_read_previous_ranks_ignores_indented_continuation_lines(tmp_path):
    d = tmp_path / "TV" / "US"; d.mkdir(parents=True)
    (d / "2026_10_30_ETF_rs_3m.txt").write_text("1. AAA - 甲\n   1M强: X > Y\n2. BBB - 乙\n   1M强: Z\n")
    assert etf_rs.read_previous_ranks(tmp_path, date(2026, 10, 31)) == {"AAA": 1, "BBB": 2}

def test_run_passes_extras_to_both_files(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {"AAA": _kline(5), "BBB": _kline(1), "SPY": _kline(0)})
    import etf_holdings_rs
    seen = {}
    def fake(cfg, etfs, bench, today, out):
        seen["etfs"] = list(etfs); seen["bench_rows"] = len(bench)
        return {"AAA": "1M强: X > Y"}
    monkeypatch.setattr(etf_holdings_rs, "compute_extras", fake)
    etf_rs.run_etf_rs(_cfg(tickers={"AAA": "甲", "BBB": "乙"}), tmp_path, date(2026, 10, 31))
    for stem in ("ETF_rs_3m", "ETF_rs_1m"):
        txt = (tmp_path / "TV" / "US" / f"2026_10_31_{stem}.txt").read_text()
        assert "\n   1M强: X > Y\n" in txt
    assert seen["etfs"] == ["AAA", "BBB"] and seen["bench_rows"] == 90

def test_run_writes_files_without_extras_when_step_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {"AAA": _kline(5), "SPY": _kline(0)})
    import etf_holdings_rs
    monkeypatch.setattr(etf_holdings_rs, "compute_extras", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert etf_rs.run_etf_rs(_cfg(tickers={"AAA": "甲"}), tmp_path, date(2026, 10, 31)) is not None
    txt = (tmp_path / "TV" / "US" / "2026_10_31_ETF_rs_3m.txt").read_text()
    assert "1M强" not in txt and txt.startswith("1. AAA")

def test_run_holdings_rs_false_skips_step(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {"AAA": _kline(5), "SPY": _kline(0)})
    import etf_holdings_rs
    monkeypatch.setattr(etf_holdings_rs, "compute_extras", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    etf_rs.run_etf_rs(_cfg(tickers={"AAA": "甲"}), tmp_path, date(2026, 10, 31), holdings_rs=False)
```

并在 `tests/test_build_etf_rs_page.py` 加：

```python
def test_build_disables_holdings_rs(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {"AAA": _walk(1), "BBB": _walk(2), "SPY": _walk(9)})
    import etf_holdings_rs
    monkeypatch.setattr(etf_holdings_rs, "compute_extras", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    assert build_etf_rs_page.build(tmp_path / "site", _config(tmp_path, _CFG)) == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_etf_rs.py tests/test_build_etf_rs_page.py -q`
Expected: 新测试 FAIL（`TypeError: unexpected keyword 'extras'` / `'holdings_rs'`、名次断言失败）

- [ ] **Step 3: 实现**

`run_etf_rs`：在 `rank_etfs` 之前
```python
extras: dict[str, str] = {}
if holdings_rs:
    try:
        import etf_holdings_rs
        extras = etf_holdings_rs.compute_extras(cfg.get("holdings_rs", {}), etfs, klines.get(benchmark), today, output_dir)
    except Exception as e:
        logger.warning(f"[{_LABEL}] holdings 1M RS failed, rankings written without it: {e}")
```
`extras` 传给两次 `write_ranking`；`read_previous_ranks` 循环开头 `if line[:1].isspace(): continue`；
`build_etf_rs_page.build` 改为 `etf_rs.run_etf_rs(cfg, Path(scratch), today, holdings_rs=False)`。
更新 `etf_rs.py` 模块 docstring 与 `write_ranking` docstring（续行格式一句话）。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_etf_rs.py tests/test_build_etf_rs_page.py tests/test_etf_rs_chart.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add etf_rs.py scripts/build_etf_rs_page.py tests/test_etf_rs.py tests/test_build_etf_rs_page.py
git commit -m "feat(etf-rs): append per-ETF 1M strongest-holdings line; cloud page build opts out"
```

---

### Task 5: 配置、清理规则、文档与实跑验证

**Files:**
- Modify: `config.toml` (`[etf_rs]` 段末尾、`[etf_rs.tickers]` 之前)
- Modify: `cleanup.py` (`_RETENTION_RULES`，紧跟 `premarket_catalyst` 规则之后)
- Modify: `CLAUDE.md` "ETF 3M RS ranking" 一节
- Test: `tests/test_cleanup.py`

**Interfaces:**
- Consumes: Task 2 的缓存文件名 `etf_holdings_<YYYY_MM_DD>.json`。

- [ ] **Step 1: 写失败测试 (tests/test_cleanup.py)**

```python
def test_etf_holdings_cache_uses_eight_day_window(output_tree: Path) -> None:
    for d in ("2026_05_15", "2026_05_08", "2026_05_07"):
        _touch(output_tree / "state" / f"etf_holdings_{d}.json")
    cleanup_old_outputs(output_tree, date(2026, 5, 15))
    assert (output_tree / "state" / "etf_holdings_2026_05_15.json").exists()
    assert (output_tree / "state" / "etf_holdings_2026_05_08.json").exists()     # today + 7 prior days
    assert not (output_tree / "state" / "etf_holdings_2026_05_07.json").exists()
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cleanup.py -q -k etf_holdings`
Expected: FAIL（05_07 的文件仍存在）

- [ ] **Step 3: 实现**

`cleanup.py`：`_Rule("state", re.compile(rf"^etf_holdings_{_DATE_U}\.json$"), "%Y_%m_%d", 8)`，
注释说明 7 天走回 + 1。

`config.toml` 加段（含注释）：
```toml
# 成分股 1M 强势前 5 (etf_holdings_rs.py): 每只 ETF 行下缩进一行 "1M强: A > B > C > D > E"。
# 成分股来源链: 当日缓存 → stockanalysis.com holdings 页 (免费版只给权重前 25)
# → ≤ cache_max_age_days 天内旧缓存 → yfinance 前十大 (行尾标 "(前十大)") → 放弃 (不写该行)。
# 只算美股上市的成分股 (海外代码跳过); 1M 算法与上面的 _1m 文件相同 (对 SPY 超额), 只在该 ETF 内部排序。
# 缓存 output/state/etf_holdings_<date>.json (8 天清理)。软步骤: 失败只少这一行。
# GitHub Pages 云端构建不跑这一步 (页面不显示)。
[etf_rs.holdings_rs]
enabled = true
top_n = 5                   # 每只 ETF 下面列几只
cache_max_age_days = 7      # stockanalysis 抓取失败时最多走回几天缓存
request_delay_sec = 0.8     # 相邻两次 stockanalysis 请求的间隔 (秒)
```
注意：TOML 里 `[etf_rs.holdings_rs]` 子表必须放在 `[etf_rs]` 的顶层键之后、
但可以在 `[etf_rs.tickers]` 之前或之后 —— 放在 `chart_top_n` 之后、`[etf_rs.tickers]` 之前。

`CLAUDE.md` ETF 一节末尾加一条 bullet：续行格式、来源链与 25 只上限、
缓存文件与 8 天窗口、`read_previous_ranks` 跳过缩进行、云端 `holdings_rs=False`、
spec 路径。

- [ ] **Step 4: 全量测试**

Run: `uv run pytest tests/ -q`
Expected: 全部 PASS

- [ ] **Step 5: 实跑验证**

Run: `uv run main.py --mode etf-rs 2>&1 | grep -E "持仓RS|holdings|ETF RS\]" | tail -70`
然后 `grep -c "1M强" output/TV/US/2026_10_31_ETF_rs_3m.txt`、
`ls output/state/etf_holdings_2026_10_31.json`。
Expected：日志里每只 ETF 一行成分统计；3M/1M 文件里绝大多数 ETF 带续行
（IBIT / BWET 等无股票持仓的可能没有）；缓存文件存在且不含 `yf_top10`。
再跑一次 `--mode etf-rs`，日志应显示走今日缓存、没有 stockanalysis 请求。

- [ ] **Step 6: Commit**

```bash
git add config.toml cleanup.py CLAUDE.md tests/test_cleanup.py
git commit -m "feat(etf-holdings-rs): config, cache retention rule, docs"
```
