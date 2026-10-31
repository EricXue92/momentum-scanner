# ETF 成分股 1M 相对强度前 5 —— 设计

日期: 2026-10-31
状态: 设计已与用户确认 (对话), 待计划 / 实现

## 1. 意图与动机

用户的用法是自上而下: 先看 `output/TV/US/<date>_ETF_rs_3m.txt` /
`_ETF_rs_1m.txt` 里的 ETF 排名定方向, 再从强势 ETF 的成分股里挑个股盯盘。
今天每行只带一段静态的"前十大持仓"字符串, 不告诉他这些股票里谁最近最强,
他要自己一只只去看。

本功能: 对 ETF 名单里的每只 ETF, 取它的成分股 (美股上市部分), 按 **1M 相对
强度** (与 ETF 1M 文件同一套算法) 排名, 把最强的前 5 只**换行缩进**写在该 ETF
那一行下面, 3M 和 1M 两个文件都加。

## 2. 目标 / 非目标 / 反目标

**目标**

- 每只 ETF 下面一行 `   1M强: AAA > BBB > CCC > DDD > EEE`, 强者在前。
- 成分股来源: stockanalysis.com 的 holdings 页 (免费版按权重前 25 只),
  本地按日缓存; 抓不到时退回 yfinance `funds_data.top_holdings` (前 10)。
- 只看美股上市的成分股; 海外代码直接跳过。
- 算法与 ETF 1M 文件一致: `us_rs_3m.WEIGHTS_1M_BARS` 周线阶梯, 对 SPY 的超额
  收益; 只在该 ETF 自己的成分股里排序 (不做百分位, 样本太小没有意义)。
- 作为 us-eod 末尾 ETF RS 步骤内部的**软步骤**: 任何失败只少这一行, 文件照
  常生成, EOD 退出码不变。

**非目标 (本期不做, 但结构上留口)**

- 全量成分股。免费数据源只有前 25; 以后按需为单个发行商 (SSGA 等) 加适配器
  补全 —— 成分股获取要做成"来源链", 加一家发行商 = 加一个来源函数, 其余不动。
- GitHub Pages 走势页上显示这 5 只。云端构建跑的是同一个 `run_etf_rs`,
  本期在云端把这一步**关掉** (见 §5.7), 页面不变。
- 海外成分股的换算 / 对齐。
- 可导入 TradingView 的逗号名单。
- 3M 口径的成分股排名 (只做 1M)。

**反目标 (做成这样算失败)**

- 这一步把 ETF 的 `.txt` 弄丢、弄空, 或拖垮 us-eod。
- 对 stockanalysis 高频请求 (每天一次、每只 ETF 一个 GET、有间隔、有缓存;
  同日重跑读缓存不再抓)。
- 续行破坏既有的名次变动标记 (`read_previous_ranks` 把 `1M强:` 当成 ticker)。

## 3. 约束

- 数据源免费层 25 只上限 (页面底部 "Showing 25 of 78 holdings, Subscribe to
  see the full list"), robots.txt 允许 `/etf/`; 页面是 SSR, `<table>` 里前 25
  行直接在 HTML 中, 列为 No./Symbol/Name/Weight/Shares/Stock Price/% Change。
- 美股代码在页面上是裸 ticker (`NVDA`, `BRK.B`), 海外是带交易所前缀或后缀
  (`TLV:NXSN`, `SHE:002472`, `688836.SZ`, `278A.T` 样式)。
- yfinance 的本机节流是项目一贯的痛点 (见 CLAUDE.md); 成分股去重后约 1000 只,
  一个批次、走已有的 `fetch_us_klines_yf(recover=True)`。
- 现有不变量: 文件只有带日期的, 同日重跑覆盖 (ETF 排名是快照), 不进
  `eod_seen`, 不镜像 Webull, 不同步 Futu/TV; `TV/US` 5 天清理。
- 走势页 `etf_rs_chart` 不读 `.txt` (从 K 线重算), 不受续行影响。

## 4. 输出格式 (用户确认: 同一个 .txt 换行显示)

```
9. OIH 新 - 油服 | SLB、BKR、FTI、HAL、TS、SEI、RIG、WFRD、NOV、NE
   1M强: SEI > RIG > NE > SLB > HAL
10. IHI 新 - 医疗器械 | ABT、ISRG、MDT、IDXX、DXCM、EW、SYK、BDX、RMD、BSX
   1M强: ISRG > BSX > DXCM > EW > SYK
```

- 续行固定 3 个空格缩进 + `1M强: ` + 以 ` > ` 连接的 ticker, 最多
  `top_n` (5) 只; 不足 5 只就写有几只写几只; 一只都没有 (没拿到成分股 /
  全是海外 / 都没打上分) 就**不写续行**。
- 来源是 yfinance 前十大兜底时, 行尾加 ` (前十大)`, 让用户知道这只 ETF 当天
  只看了 10 只。stockanalysis 来源 (今天的或走回的缓存) 不加标注, 缓存走回
  只进日志。
- 显示用页面上的写法 (`BRK.B`), 只在请求 yfinance 时换成 `BRK-B`。
- 3M 文件和 1M 文件下挂**同一份**续行 (同一次计算结果)。

## 5. 机制

### 5.1 新模块 `etf_holdings_rs.py`

对外两个函数:

- `fetch_constituents(etfs, today, output_dir, cfg) -> dict[str, Constituents]`
  —— §5.2–5.4。`Constituents = (symbols: list[str], source: str)`,
  `source ∈ {"stockanalysis", "stockanalysis-cache:<date>", "yf_top10"}`。
- `rank_holdings(constituents, klines, benchmark_kline, top_n) -> dict[str, list[str]]`
  —— §5.5。

`etf_rs.run_etf_rs` 负责编排 (§5.6)。模块自己不读 config 文件, 吃传进来的
`cfg["holdings_rs"]` 字典。

### 5.2 成分股来源链

每只 ETF 按顺序尝试, 第一个成功的即采用:

1. **今日缓存** `output/state/etf_holdings_<YYYY_MM_DD>.json` 里有该 ETF →
   直接用 (同日重跑不再上网)。
2. **stockanalysis 在线抓取** `https://stockanalysis.com/etf/<etf 小写>/holdings/`
   (`timeout` 15s, 浏览器 UA, 每个请求间隔 `request_delay_sec` 默认 0.8s;
   HTTP 非 200、超时、解析不到表格 → 本来源失败并 WARNING)。
3. **缓存走回**: 最近 `cache_max_age_days` (7) 天内任一日缓存里有该 ETF 且
   来源是 stockanalysis → 用, 日志标明用了哪天的。
4. **yfinance 前十大** `yf.Ticker(etf).funds_data.top_holdings` 的 index →
   `source = "yf_top10"`。
5. 都没有 → 该 ETF 没有成分股, 不写续行, WARNING。

以后加发行商适配器 = 在 2 前面插一个来源函数 (例如 `_from_ssga`), 签名统一
为 `(etf) -> list[str] | None`。

### 5.3 stockanalysis 页面解析

- 定位第一个 `<table>`, 表头含 `Symbol`; 每个 `<tr>` 取 Symbol 列文本。
- 去 HTML 标签、`&amp;` 反转义、strip。
- 表格缺失或 0 行 → 视为失败 (走 §5.2 第 3 步), 不缓存空结果。
- 顺序保留 (页面按权重降序), 以后可能用上; 本期只用集合。
- 不解析权重 / 价格列。

### 5.4 美股过滤

符合 `^[A-Z]{1,5}(\.[A-Z])?$` 的保留, 其余 (含冒号前缀、数字开头、`.SZ`/`.T`
后缀、空、`-`、`n/a`) 丢弃。过滤发生在**排名前**, 缓存里存的是解析出的原始
列表 (含海外), 这样以后要用海外部分不必重抓。

yfinance 代码映射: 显示 `BRK.B` ↔ 请求 `BRK-B` (`.` → `-`); 返回后映射回
显示代码。

### 5.5 打分与排名

- 全部 ETF 的美股成分股去重后一次 `us_rs_3m.fetch_us_klines_yf(symbols,
  period="3mo", fill_last_close=True)` (21 根起算, 3mo 有余量)。
- 每只股票: `us_rs_3m._score_from_kline_bars(df, WEIGHTS_1M_BARS)`; SPY 的
  分数用 `run_etf_rs` 已拿到的 benchmark K 线同样算一次; 股票分 − SPY 分 =
  超额。SPY 缺席 → 用绝对分排序并 WARNING (与 ETF 排名的既有行为一致)。
- 每只 ETF: 在自己的成分股里按超额降序, 取前 `top_n`; 没打上分的
  (`reason != "ok"`) 不参与。
- 日志: 每只 ETF 一行 `成分 N (来源) / 美股 M / 打分 K / 前5: ...`, 以及
  汇总 "K 线覆盖 X/Y"。

### 5.6 在 `run_etf_rs` 中的编排

```
klines (ETF + SPY) 拉取完成
→ [软] extras = holdings_rs 步骤 (§5.2–5.5)   # 失败 → extras = {}
→ 3M: rank_etfs → collapse → write_ranking(..., extras=extras)
→ [软] 1M: 同 write_ranking(..., extras=extras)
→ [软] 走势页 (不变)
```

- 放在写文件**之前**, 这样一次计算两个文件都能挂上, 不用回写。代价是 3M
  文件晚几分钟落地; 这一步整体 try/except, 失败就 `extras = {}`, 文件格式退回
  今天的样子。
- `write_ranking(..., extras: dict[str, str] | None = None)`: 某 ETF 在
  `extras` 里有非空字符串 → 在它那行后面追加 `"   " + extras[t]`。
  `_format_line` 不变。
- `read_previous_ranks`: **以空白开头的行跳过**。现有测试 "已标注行也能解析"
  继续成立; 新测试: 带续行的文件解析出的名次与不带时一致。
- 同名 collapse 后被隐藏的 ETF 自然没有行, 也就没有续行; extras 按 ticker
  查, 不需要特殊处理。

### 5.7 云端 Pages 构建

`scripts/build_etf_rs_page.py` 调 `run_etf_rs` 只为了走势页。给
`run_etf_rs` 加一个参数 `holdings_rs: bool = True`, 构建脚本传 `False`
(或等价地把 `cfg["holdings_rs"]["enabled"]` 置 False)。云端不抓
stockanalysis、不拉 1000 只 K 线, GH job 时长和失败面不变。

### 5.8 缓存与清理

- 文件: `output/state/etf_holdings_<YYYY_MM_DD>.json`, 内容
  `{"XLK": {"symbols": [...], "source": "stockanalysis"}, ...}`; 每天跑完写
  一次 (只写 stockanalysis 来源的条目; `yf_top10` 不进缓存, 以免走回时把
  兜底当正式数据)。
- `cleanup._RETENTION_RULES` 加一条 `state` 规则, 8 天窗口 (覆盖 7 天走回)。
- 写失败软处理 (WARNING, 不影响输出)。

### 5.9 配置 `[etf_rs.holdings_rs]`

```toml
[etf_rs.holdings_rs]
enabled = true
top_n = 5                   # 每只 ETF 下面列几只
cache_max_age_days = 7      # stockanalysis 抓取失败时最多走回几天缓存
request_delay_sec = 0.8     # 相邻两次 stockanalysis 请求的间隔
```

缺省值与上面一致; 整段缺失 = 启用、缺省值。

## 6. 测试 (pytest, 不上网)

- 解析: 用 `tests/fixtures/stockanalysis_holdings_sample.html` (从真实页面
  裁剪、保留表格) 解析出 25 个 symbol, 含 `BRK.B`, 顺序与页面一致; 无表格的
  HTML → None。
- 美股过滤: `TLV:NXSN`、`688836.SZ`、`SHE:002472`、`278A.T`、`-` 被丢,
  `NVDA`、`BRK.B` 保留; `BRK.B` → `BRK-B` 请求映射并映射回来。
- 来源链: 今日缓存命中不调抓取函数; 抓取失败走回 ≤ 7 天的缓存, 8 天前的
  不用; 再失败走 yf 前十大并标 `yf_top10`; 全失败 → 该 ETF 缺席。
- 缓存写入: 只含 stockanalysis 来源条目。
- 排名: 假 K 线 (`_fetch_klines` 同款 monkeypatch 风格), 验证按对 SPY 超额
  降序取前 `top_n`、不足 5 只照实列、无 SPY 时按绝对分、`< 21` 根不参与。
- 输出: `write_ranking(extras=...)` 在对应行后追加 3 空格缩进续行;
  `yf_top10` 来源带 ` (前十大)`; 空 extras 时文件与今天完全一致。
- `read_previous_ranks` 对带续行的文件给出与不带续行时相同的名次。
- `run_etf_rs(holdings_rs=False)` 不调用成分股步骤; 步骤抛异常时两个文件
  仍然写出且不带续行。
- `tests/test_schedule_wakes.py` 等现有测试全绿。

## 7. 交付清单

- `etf_holdings_rs.py` (新) + `tests/test_etf_holdings_rs.py` (新) + fixture。
- `etf_rs.py`: `write_ranking(extras=)`, `read_previous_ranks` 跳过缩进行,
  `run_etf_rs(holdings_rs=)` 编排。
- `scripts/build_etf_rs_page.py`: 传 `holdings_rs=False`。
- `cleanup.py`: 新清理规则。
- `config.toml`: `[etf_rs.holdings_rs]` 段 + 注释。
- `CLAUDE.md` ETF 一节加一条 (续行格式、来源链、缓存、云端关闭、
  `read_previous_ranks` 跳过缩进行)。

## 8. 设计者补定的细节 (reviewer 提出, 作者拍板)

- ` (前十大)` 后缀在 `etf_holdings_rs` 步骤内拼进 extras 字符串,
  `write_ranking` 只负责原样追加续行, 不认识来源。
- 成分股对**全部配置的 ETF** 抓取 (同名 collapse 发生在之后; 今天名单里没有
  重名, 不为此省请求)。
- SPY 的 K 线直接复用 `run_etf_rs` 已拉的那份 (`fill_last_close=True`),
  成分股批次同样开 `fill_last_close=True`, 两边最后一根 bar 口径一致。

## 9. 留给实现者的判断

- 解析用正则还是 `html.parser`: 任选, 以 fixture 测试为准; 不新增第三方依赖。
- 1000 只 K 线批次的具体分批大小沿用 `fetch_us_klines_yf` 的既有逻辑。
- 日志措辞。
