"""Trend page for the daily ETF 3M RS ranking (``etf_rs``).

The dated ``TV/US/<date>_ETF_rs.txt`` snapshots age out after 5 days and carry
no scores, so they cannot show how the ranking evolved. This module instead
**recomputes** the 3M relative score for every past trading day from the same
klines ``run_etf_rs`` already fetched (no extra network call, no state file)
and renders one self-contained HTML page:

``output/Reports/ETF/etf_rs_trend.html`` — X = trading day, Y = the 0-99 RS
percentile (default, with a marked RS 90 line), rank, or the raw 3M relative
score (toggle); the latest top ``chart_top_n`` are drawn thick, every ETF's
中文名 + 前五大持仓 sit in the side list / tooltip. The percentile is **within
the ETF set** (like the daily ranking), so RS >= 90 is by construction the
top ~10% of the list — about 5 of 52 names.

Deliberately **undated and overwritten** every run: the page carries its whole
history, so dated copies would be pure duplicates (and no cleanup rule is
needed — ``_RETENTION_RULES`` never matches it).

History is recomputed with *today's* ticker list and split/dividend-adjusted
closes, so a past day can sit a place or two off that day's ``.txt``; the last
day matches today's ranking exactly (same klines, same algorithm).

Soft-fail by design: ``run_etf_rs`` wraps the call, a failure only warns.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import us_rs_3m

logger = logging.getLogger("momentum_scanner")

_LABEL = "ETF RS"
_SUBDIR = Path("Reports") / "ETF"
_FILENAME = "etf_rs_trend.html"
_DATA_SLOT = "__ETF_RS_DATA__"


def _ticker_scores(df: pd.DataFrame | None) -> pd.Series:
    """Absolute 3M score for every bar of one kline, indexed by naive date.

    Same arithmetic as ``us_rs_3m._score_from_kline`` applied to the kline
    truncated at each bar: offsets count the ticker's **own** rows, NaN before
    the 64th bar and wherever a close involved is non-positive."""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    idx = pd.DatetimeIndex(pd.to_datetime(df["time_key"]))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    closes = pd.Series(df["close"].astype(float).to_numpy(), index=idx.normalize())
    closes = closes[~closes.index.duplicated(keep="last")].sort_index()
    last = closes.to_numpy()
    score = np.zeros(len(last))
    valid = last > 0
    with np.errstate(divide="ignore", invalid="ignore"):
        for months, weight in us_rs_3m.WEIGHTS_3M:
            lag = months * 21
            past = np.full(len(last), np.nan)
            if lag < len(last):
                past[lag:] = last[:-lag]
            valid &= past > 0
            score += weight * (last / past - 1.0)
    score[~valid] = np.nan
    return pd.Series(score, index=closes.index)


def score_history(
    klines: dict[str, pd.DataFrame],
    tickers: list[str],
    benchmark: str,
    max_days: int | None = None,
) -> pd.DataFrame:
    """Benchmark-relative 3M score per trading day (index) and ticker (columns,
    in ``tickers`` order) — each row is what ``etf_rs.rank_etfs`` would have
    scored on klines truncated to that day.

    The calendar is the benchmark's scorable days. A ticker with no bar on a
    day carries its latest earlier bar (the daily run scores whatever its last
    row is); days before its 64th bar are NaN. Tickers that are never scorable
    are left out. A missing/unscorable benchmark falls back to absolute scores
    on the union calendar, like the daily ranking."""
    cols = {t: _ticker_scores(klines[t]) for t in tickers if t in klines}
    cols = {t: s for t, s in cols.items() if s.notna().any()}
    if not cols:
        return pd.DataFrame()
    bench = _ticker_scores(klines.get(benchmark)).dropna()
    if bench.empty:
        calendar = pd.DatetimeIndex(sorted(set().union(*(s.index for s in cols.values()))))
    else:
        calendar = bench.index
    frame = pd.DataFrame({t: s.reindex(calendar, method="ffill") for t, s in cols.items()})
    if not bench.empty:
        frame = frame.sub(bench, axis=0)
    frame = frame.dropna(how="all")
    return frame.tail(max_days) if max_days else frame


def percentile_history(history: pd.DataFrame) -> pd.DataFrame:
    """0-99 RS percentile per day among the tickers scored that day — the
    formula of ``us_rs_3m.compute_us_rs_3m_table`` (average-rank pct x 99,
    rounded), so each row equals that day's ``rs_percentile`` column.
    **Within the ETF set**, like the daily ranking; unscored days are <NA>."""
    pct = history.rank(axis=1, method="average", pct=True) * 99
    return pct.round().astype("Int64")


def render_html(
    history: pd.DataFrame,
    names: dict[str, str],
    holdings: dict[str, str],
    benchmark: str,
    today: date,
    top_n: int = 20,
) -> str:
    """The self-contained page. Series keep ``history``'s column order (the
    caller passes today's ranking order, strongest first); per day it embeds
    the 0-99 RS percentile (``p``) and the raw relative score in percent
    (``s``), unscored days as null."""
    pct = percentile_history(history)
    payload = {
        "generated": today.isoformat(),
        "benchmark": benchmark,
        "top_n": top_n,
        "dates": [d.strftime("%Y-%m-%d") for d in history.index],
        "series": [
            {
                "t": t,
                "name": names.get(t, ""),
                "holdings": holdings.get(t, ""),
                "s": [None if pd.isna(v) else round(float(v) * 100, 2) for v in history[t]],
                "p": [None if pd.isna(v) else int(v) for v in pct[t]],
            }
            for t in history.columns
        ],
    }
    # "<" never appears raw, so config text cannot close the <script> block.
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return _TEMPLATE.replace(_DATA_SLOT, data)


def write_trend_chart(
    klines: dict[str, pd.DataFrame],
    tickers: list[str],
    benchmark: str,
    names: dict[str, str],
    holdings: dict[str, str],
    output_dir: Path,
    today: date,
    top_n: int = 20,
) -> Path | None:
    """Write ``Reports/ETF/etf_rs_trend.html`` (overwritten every run) for
    ``tickers`` in the given order. No scorable history → no file, None."""
    history = score_history(klines, tickers, benchmark)
    if history.empty:
        logger.warning(f"[{_LABEL}] no score history; trend chart not written")
        return None
    target = output_dir / _SUBDIR
    target.mkdir(parents=True, exist_ok=True)
    out = target / _FILENAME
    out.write_text(render_html(history, names, holdings, benchmark, today, top_n), encoding="utf-8")
    logger.info(
        f"[{_LABEL}] trend chart: {len(history.columns)} ETFs x {len(history)} days "
        f"({history.index[0]:%Y-%m-%d} → {history.index[-1]:%Y-%m-%d}) -> {out}"
    )
    return out


_TEMPLATE = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ETF 3M RS 走势</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --band: #f3f2ee;
  --ink: #0b0b0b; --ink2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --rest: #c9c8c0; --top: #6f6e69;
  --up: #006300; --down: #c22f2f;
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100;
  --s5: #e87ba4; --s6: #008300; --s7: #4a3aa7; --s8: #e34948;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --band: #141413;
    --ink: #ffffff; --ink2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --rest: #45453f; --top: #a9a8a0;
    --up: #0ca30c; --down: #e66767;
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
    --s5: #d55181; --s6: #008300; --s7: #9085e9; --s8: #e66767;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", "PingFang SC", sans-serif;
}
.wrap { max-width: 1560px; margin: 0 auto; padding: 20px 16px 32px; }
h1 { font-size: 20px; font-weight: 650; margin: 0; }
.sub { color: var(--ink2); margin: 2px 0 14px; font-size: 13px; }
.filters { display: flex; flex-wrap: wrap; gap: 8px 18px; align-items: center; margin-bottom: 12px; }
.filters .lbl { color: var(--muted); font-size: 12px; margin-right: 6px; }
.seg { display: inline-flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; background: var(--surface); }
.seg button {
  font: inherit; font-size: 13px; color: var(--ink2); background: none; border: 0;
  padding: 5px 12px; cursor: pointer; border-left: 1px solid var(--border);
}
.seg button:first-child { border-left: 0; }
.seg button:hover { background: var(--band); }
.seg button[aria-pressed="true"] { background: var(--ink); color: var(--surface); font-weight: 600; }
.ghost {
  font: inherit; font-size: 13px; color: var(--ink2); background: var(--surface);
  border: 1px solid var(--border); border-radius: 8px; padding: 5px 12px; cursor: pointer;
}
.ghost:hover { background: var(--band); }
.chk { font-size: 13px; color: var(--ink2); cursor: pointer; user-select: none; }
.chk[hidden] { display: none; }
.chk input { accent-color: var(--ink); margin: 0 4px 0 0; vertical-align: -1px; }
.hint { color: var(--muted); font-size: 12px; }
.hint.warn { color: var(--down); }
button:focus-visible { outline: 2px solid var(--s1); outline-offset: -2px; }
.grid { display: grid; grid-template-columns: minmax(0, 1fr) 390px; gap: 16px; align-items: start; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; }
.card h2 { font-size: 13px; font-weight: 600; margin: 0; padding: 12px 14px 4px; color: var(--ink2); }
.chart { position: relative; padding: 0 6px 6px; }
.chart svg { display: block; width: 100%; touch-action: pan-y; }
.list { position: sticky; top: 16px; max-height: calc(100vh - 32px); overflow: auto; }
.list h2 { position: sticky; top: 0; background: var(--surface); padding-bottom: 8px; border-bottom: 1px solid var(--grid); z-index: 1; }
@media (max-width: 1020px) {
  .grid { grid-template-columns: minmax(0, 1fr); }
  .list { position: static; max-height: none; }
}

/* chart marks */
.ln { fill: none; stroke-linejoin: round; stroke-linecap: round; }
.ln.rest { stroke: var(--rest); stroke-width: 1; }
.ln.top { stroke: var(--top); stroke-width: 2.25; opacity: 0.5; }
.ln.pin { stroke-width: 2.75; }
.ln.hov { stroke-width: 3.5; }
svg.dim .ln.rest { opacity: 0.45; }
svg.dim .ln.top { opacity: 0.16; }
svg.dim .ln.pin { opacity: 0.35; }
.gridl { stroke: var(--grid); stroke-width: 1; }
.zero { stroke: var(--axis); stroke-width: 1; }
.thr { stroke: var(--ink2); stroke-width: 1.25; }
.bandbg { fill: var(--band); }
.tick { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.note { fill: var(--ink2); font-size: 10.5px; paint-order: stroke; stroke: var(--surface); stroke-width: 3px; stroke-linejoin: round; }
.cross { stroke: var(--axis); stroke-width: 1; }
.dot { stroke: var(--surface); stroke-width: 2; }
.lab { cursor: pointer; }
.lab text { font-size: 11px; }
.lab .rk { fill: var(--muted); font-variant-numeric: tabular-nums; }
.lab .tk { fill: var(--ink); font-weight: 600; }
.lab .nm { fill: var(--ink2); }
.lab .lead { stroke: var(--axis); stroke-width: 1; fill: none; }
.lab.is-hover .tk, .lab.is-pin .tk { font-weight: 750; }
svg.dim .lab:not(.is-hover) { opacity: 0.4; }
.hit { fill: transparent; cursor: crosshair; }

/* tooltip */
.tip {
  position: absolute; pointer-events: none; z-index: 3; min-width: 190px; max-width: 320px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  box-shadow: 0 6px 22px rgba(0,0,0,0.16); padding: 9px 11px; font-size: 12px;
}
.tip[hidden] { display: none; }
.tip .d { color: var(--muted); margin-bottom: 4px; }
.tip .main { margin-bottom: 4px; }
.tip .main .who { font-weight: 650; font-size: 13px; }
.tip .main .who .n { font-weight: 400; color: var(--ink2); margin-left: 5px; }
.tip .main .val { margin-top: 2px; }
.tip .main .val b { font-size: 15px; }
.tip .main .hold { color: var(--ink2); margin-top: 4px; }
.tip .rows { border-top: 1px solid var(--grid); margin-top: 6px; padding-top: 5px; }
.tip .r { display: grid; grid-template-columns: 14px 1fr auto auto auto; gap: 8px; align-items: center; font-variant-numeric: tabular-nums; }
.tip .r b { font-weight: 650; }
.tip .r .t { color: var(--ink2); }
.key { display: inline-block; width: 12px; height: 3px; border-radius: 2px; background: var(--top); vertical-align: middle; }
.up { color: var(--up); } .down { color: var(--down); } .flat { color: var(--muted); }

/* ranking list */
.row {
  display: grid; grid-template-columns: 24px 38px 14px minmax(0, 1fr) auto; column-gap: 6px;
  align-items: baseline; width: 100%; text-align: left; font: inherit; color: inherit;
  background: none; border: 0; border-bottom: 1px solid var(--grid); padding: 7px 14px; cursor: pointer;
}
.row:hover, .row.is-hover { background: var(--band); }
.row .rk { color: var(--muted); font-variant-numeric: tabular-nums; text-align: right; }
.row .dl { font-size: 11.5px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.row .key { align-self: center; visibility: hidden; }
.row.is-pin .key { visibility: visible; }
.row .who { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row .tk { font-weight: 650; }
.row .nm { color: var(--ink2); margin-left: 6px; }
.row .sc { font-variant-numeric: tabular-nums; color: var(--muted); font-size: 12px; white-space: nowrap; }
.row .sc b { color: var(--ink); font-size: 14px; font-weight: 650; margin-right: 6px; }
.row .hd { grid-column: 4 / -1; color: var(--muted); font-size: 12px; margin-top: 1px; }
.row.rest .tk { font-weight: 500; }
.split { padding: 6px 14px; font-size: 11.5px; color: var(--muted); background: var(--band); border-bottom: 1px solid var(--grid); }
</style>
</head>
<body>
<div class="wrap">
  <h1>ETF 3M 相对强度走势</h1>
  <p class="sub" id="sub"></p>
  <div class="filters">
    <span><span class="lbl">范围</span><span class="seg" id="range"></span></span>
    <span><span class="lbl">Y 轴</span><span class="seg" id="mode"></span></span>
    <label class="chk" id="clip-wrap"><input type="checkbox" id="clip"> 裁剪极端值</label>
    <button class="ghost" id="clear" type="button">清除选中</button>
    <span class="hint" id="hint"></span>
  </div>
  <div class="grid">
    <section class="card">
      <h2 id="chart-title"></h2>
      <div class="chart" id="chart"><svg id="svg" role="img"></svg><div class="tip" id="tip" hidden></div></div>
    </section>
    <aside class="card list" id="list"></aside>
  </div>
</div>
<script id="etf-rs-data" type="application/json">__ETF_RS_DATA__</script>
<script>
(function () {
  'use strict';
  const D = JSON.parse(document.getElementById('etf-rs-data').textContent);
  const S = D.series, dates = D.dates, n = dates.length, N = S.length;
  const TOP = Math.min(D.top_n, N);
  const SLOTS = 8, NS = 'http://www.w3.org/2000/svg';
  const HINT = '点击线条或右侧列表可固定颜色(最多 ' + SLOTS + ' 只);悬停查看持仓';
  const RANGES = [['1M', 21], ['3M', 63], ['6M', 126], ['全部', 0]];
  const MODES = [['RS 评分', 'rs'], ['名次', 'rank'], ['超额收益 %', 'score']];
  const RS_LINE = 90;
  const WEEK = ['日', '一', '二', '三', '四', '五', '六'];

  // rank of every series on every day (1 = strongest); ties keep payload
  // order, which is today's ranking order.
  const ranks = S.map(() => new Array(n).fill(null));
  for (let i = 0; i < n; i++) {
    const order = [];
    for (let k = 0; k < N; k++) if (S[k].s[i] != null) order.push(k);
    order.sort((a, b) => (S[b].s[i] - S[a].s[i]) || (a - b));
    order.forEach((k, j) => { ranks[k][i] = j + 1; });
  }

  const store = {
    get(key, dflt) {
      try { const v = localStorage.getItem('etfrs.' + key); return v == null ? dflt : JSON.parse(v); }
      catch (e) { return dflt; }
    },
    set(key, v) { try { localStorage.setItem('etfrs.' + key, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  };
  const known = new Map(S.map((s, k) => [s.t, k]));
  function loadPins() {
    const saved = store.get('pins', null);
    const pins = new Array(SLOTS).fill(null);
    if (Array.isArray(saved)) {
      saved.slice(0, SLOTS).forEach((t, i) => { if (known.has(t)) pins[i] = t; });
    } else {
      S.slice(0, Math.min(5, TOP)).forEach((s, i) => { pins[i] = s.t; });
    }
    return pins;
  }
  const state = {
    range: [21, 63, 126, 0].includes(store.get('range', 63)) ? store.get('range', 63) : 63,
    mode: ['rs', 'rank', 'score'].includes(store.get('mode', 'rs')) ? store.get('mode', 'rs') : 'rs',
    clip: store.get('clip', true) !== false,   // score mode: keep one runaway ETF from flattening the rest
    pins: loadPins(),   // slot index = color; a slot stays with its ETF until unpinned
    hover: null,
  };
  let G = null;         // geometry of the last render, for the pointer handlers

  const $ = id => document.getElementById(id);
  const svg = $('svg'), tip = $('tip'), chartEl = $('chart'), listEl = $('list');
  function el(name, attrs, parent) {
    const e = document.createElementNS(NS, name);
    for (const a in attrs) e.setAttribute(a, attrs[a]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function h(tag, cls, text, parent) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    if (parent) parent.appendChild(e);
    return e;
  }
  const slotOf = k => state.pins.indexOf(S[k].t);
  const colorOf = k => { const s = slotOf(k); return s < 0 ? null : 'var(--s' + (s + 1) + ')'; };
  const fmtScore = v => v == null ? '–' : (v > 0 ? '+' : '') + v.toFixed(1) + '%';
  function delta(k, i) {
    const cur = ranks[k][i], prev = i > 0 ? ranks[k][i - 1] : null;
    if (cur == null || i === 0) return ['', 'flat'];
    if (prev == null) return ['新', 'flat'];
    if (prev === cur) return ['–', 'flat'];
    return prev > cur ? ['▲' + (prev - cur), 'up'] : ['▼' + (cur - prev), 'down'];
  }
  const shortName = name => name.length > 9 ? name.slice(0, 8) + '…' : name;

  // ---------- controls ----------
  function segmented(host, options, key) {
    options.forEach(([label, value]) => {
      const b = h('button', '', label, host);
      b.type = 'button';
      b.addEventListener('click', () => {
        state[key] = value; store.set(key, value); syncControls(); render();
      });
      b.dataset.value = String(value);
    });
  }
  function syncControls() {
    for (const [host, key] of [[$('range'), 'range'], [$('mode'), 'mode']]) {
      host.querySelectorAll('button').forEach(b => {
        b.setAttribute('aria-pressed', String(b.dataset.value === String(state[key])));
      });
    }
    $('clip-wrap').hidden = state.mode !== 'score';
    $('clip').checked = state.clip;
    const thick = ';粗线 = 当日前 ' + TOP + ')';
    $('chart-title').textContent = state.mode === 'rs'
      ? '3M RS 评分走势(名单内 0–99 百分位;横线 = RS ' + RS_LINE + thick
      : state.mode === 'rank' ? '名次走势(1 = 最强' + thick
      : '3M 加权超额收益(相对 ' + D.benchmark + ',%;RS 评分的原始分数' + thick;
  }
  let hintTimer = 0;
  function flashHint(text) {
    const e = $('hint');
    e.textContent = text; e.classList.add('warn');
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => { e.textContent = HINT; e.classList.remove('warn'); }, 2600);
  }
  function togglePin(k) {
    const s = slotOf(k);
    if (s >= 0) state.pins[s] = null;
    else {
      const free = state.pins.indexOf(null);
      if (free < 0) { flashHint('最多固定 ' + SLOTS + ' 只,先取消一只再选'); return; }
      state.pins[free] = S[k].t;
    }
    store.set('pins', state.pins);
    render(); renderList();
  }

  // ---------- chart ----------
  function niceTicks(lo, hi) {
    const span = (hi - lo) || 1, raw = span / 6;
    const mag = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / mag;
    const step = (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * mag;
    const a = Math.floor(lo / step) * step, b = Math.ceil(hi / step) * step, ticks = [];
    for (let v = a; v <= b + step / 2; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
    return { a, b, ticks };
  }
  function render() {
    const W = Math.max(320, chartEl.clientWidth - 12), narrow = W < 640;
    const rankMode = state.mode === 'rank', rsMode = state.mode === 'rs';
    const H = rsMode ? 760 : rankMode ? 700 : 580;
    const m = { l: 36, r: narrow ? 60 : 178, t: 12, b: 28 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    const cnt = state.range ? Math.min(state.range, n) : n, i0 = n - cnt;
    const X = i => m.l + (cnt === 1 ? pw / 2 : (i - i0) / (cnt - 1) * pw);
    const val = rsMode ? ((k, i) => S[k].p[i]) : rankMode ? ((k, i) => ranks[k][i]) : ((k, i) => S[k].s[i]);

    svg.replaceChildren();
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.setAttribute('height', H);
    svg.setAttribute('aria-label', $('chart-title').textContent);
    svg.classList.remove('dim');
    tip.hidden = true;
    state.hover = null;
    listEl.querySelectorAll('.row.is-hover').forEach(r => r.classList.remove('is-hover'));
    const clipId = 'plot-clip';
    el('rect', { x: m.l, y: m.t - 2, width: pw + 2, height: ph + 4 },
      el('clipPath', { id: clipId }, el('defs', {}, svg)));

    let Y, note = null, offScale = () => false, G0 = null;
    if (rsMode) {
      Y = v => m.t + (100 - v) / 100 * ph;
      el('rect', { class: 'bandbg', x: m.l, y: Y(100), width: pw, height: Y(RS_LINE) - Y(100) }, svg);
      for (let v = 0; v <= 100; v += 10) {
        el('line', { class: v === RS_LINE ? 'thr' : 'gridl', x1: m.l, x2: m.l + pw, y1: Y(v), y2: Y(v) }, svg);
        el('text', { class: 'tick', x: m.l - 8, y: Y(v) + 4, 'text-anchor': 'end' }, svg).textContent = v;
      }
      note = { x: m.l + 6, y: Y(RS_LINE) - 6, text: 'RS ≥ ' + RS_LINE };
    } else if (rankMode) {
      // Focus + context: the top band (ranks 1..TOP) gets most of the height,
      // the rest is compressed below it.
      const split = N > TOP, ht = split ? ph * 0.68 : ph;
      Y = r => r <= TOP || !split
        ? m.t + (r - 0.5) / (split ? TOP : N) * ht
        : m.t + ht + (r - TOP - 0.5) / (N - TOP) * (ph - ht);
      if (split) {
        el('rect', { class: 'bandbg', x: m.l, y: m.t + ht, width: pw, height: ph - ht }, svg);
        note = { x: m.l + 6, y: m.t + ph - 6, text: '第 ' + (TOP + 1) + '–' + N + ' 名(压缩显示)' };
      }
      const ticks = [1];
      for (let r = 5; r <= TOP; r += 5) ticks.push(r);
      for (let r = Math.ceil((TOP + 1) / 10) * 10; r <= N; r += 10) ticks.push(r);
      ticks.forEach(r => {
        el('line', { class: 'gridl', x1: m.l, x2: m.l + pw, y1: Y(r), y2: Y(r) }, svg);
        el('text', { class: 'tick', x: m.l - 8, y: Y(r) + 4, 'text-anchor': 'end' }, svg).textContent = r;
      });
    } else {
      const vals = [];
      for (let k = 0; k < N; k++) for (let i = i0; i < n; i++) if (S[k].s[i] != null) vals.push(S[k].s[i]);
      vals.sort((a, b) => a - b);
      let lo = vals.length ? vals[0] : -1, hi = vals.length ? vals[vals.length - 1] : 1;
      if (state.clip && vals.length > 8) {
        // Scale to the largest value inside a wide Tukey fence (4 x IQR);
        // anything beyond runs off the plot and is flagged on its label.
        const q = f => vals[Math.min(vals.length - 1, Math.floor(f * vals.length))];
        const iqr = q(0.75) - q(0.25), fLo = q(0.25) - 4 * iqr, fHi = q(0.75) + 4 * iqr;
        lo = vals.find(v => v >= fLo);
        for (let j = vals.length - 1; j >= 0; j--) if (vals[j] <= fHi) { hi = vals[j]; break; }
      }
      const t = niceTicks(Math.min(lo, 0), Math.max(hi, 0));
      G0 = t;
      const yRaw = v => m.t + (t.b - v) / (t.b - t.a) * ph;
      Y = v => Math.max(m.t - 2, Math.min(m.t + ph + 2, yRaw(v)));
      offScale = v => v > t.b || v < t.a;
      t.ticks.forEach(v => {
        el('line', { class: v === 0 ? 'zero' : 'gridl', x1: m.l, x2: m.l + pw, y1: Y(v), y2: Y(v) }, svg);
        el('text', { class: 'tick', x: m.l - 8, y: Y(v) + 4, 'text-anchor': 'end' }, svg).textContent = v;
      });
      note = { x: m.l + 6, y: Y(0) - 5, text: '0 = ' + D.benchmark };
    }

    // x ticks: weeks for short ranges, months otherwise
    const weekly = cnt <= 30;
    let lastTickX = -Infinity;
    for (let i = i0; i < n; i++) {
      const d = new Date(dates[i] + 'T00:00:00');
      const p = i > 0 ? new Date(dates[i - 1] + 'T00:00:00') : null;
      const boundary = !p ? false : weekly ? d.getDay() < p.getDay() : d.getMonth() !== p.getMonth();
      if (!boundary || X(i) - lastTickX < 44) continue;
      lastTickX = X(i);
      const label = weekly ? (d.getMonth() + 1) + '/' + d.getDate()
        : d.getMonth() === 0 ? d.getFullYear() + '年1月' : (d.getMonth() + 1) + '月';
      el('line', { class: 'gridl', x1: X(i), x2: X(i), y1: m.t, y2: m.t + ph }, svg);
      el('text', { class: 'tick', x: X(i), y: H - 9, 'text-anchor': 'middle' }, svg).textContent = label;
    }

    const Yline = G0 ? (v => m.t + (G0.b - v) / (G0.b - G0.a) * ph) : Y;
    function pathOf(k) {
      let d = '', pen = false;
      for (let i = i0; i < n; i++) {
        const v = val(k, i);
        if (v == null) { pen = false; continue; }
        d += (pen ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Yline(v).toFixed(1);
        pen = true;
      }
      return d;
    }
    // draw order: context, top band, pinned, hover overlay
    const clip = { 'clip-path': 'url(#' + clipId + ')' };
    const layers = { rest: el('g', clip, svg), top: el('g', clip, svg), pin: el('g', clip, svg) };
    for (let k = N - 1; k >= 0; k--) {
      const c = colorOf(k), kind = c ? 'pin' : k < TOP ? 'top' : 'rest';
      const p = el('path', { class: 'ln ' + kind, d: pathOf(k) }, layers[kind]);
      if (c) p.style.stroke = c;
    }
    if (note) el('text', { class: 'note', x: note.x, y: note.y }, svg).textContent = note.text;
    const hovPath = el('path', { class: 'ln hov', visibility: 'hidden' }, el('g', clip, svg));

    // end labels: top band + anything pinned; pushed apart, tied back by leaders
    const labs = [];
    for (let k = 0; k < N; k++) {
      const v = val(k, n - 1);
      if (v != null && (k < TOP || slotOf(k) >= 0)) labs.push({ k, y0: Y(v), y: Y(v) });
    }
    labs.sort((a, b) => a.y0 - b.y0);
    const gap = 13, floor = m.t + ph;
    for (let j = 1; j < labs.length; j++) labs[j].y = Math.max(labs[j].y, labs[j - 1].y + gap);
    if (labs.length && labs[labs.length - 1].y > floor) {
      labs[labs.length - 1].y = floor;
      for (let j = labs.length - 2; j >= 0; j--) labs[j].y = Math.min(labs[j].y, labs[j + 1].y - gap);
    }
    const xe = X(n - 1), lx = xe + (narrow ? 12 : 22);
    const labEls = new Map();
    labs.forEach(({ k, y0, y }) => {
      const c = colorOf(k);
      const g = el('g', { class: 'lab' + (c ? ' is-pin' : '') }, svg);
      el('path', { class: 'lead', d: 'M' + (xe + 6) + ' ' + y0.toFixed(1) + 'L' + (lx - 4) + ' ' + y.toFixed(1) }, g);
      if (c) el('circle', { class: 'dot', cx: xe, cy: y0, r: 4 }, g).style.fill = c;
      const t = el('text', { x: lx, y: y + 4 }, g);
      if (!narrow) el('tspan', { class: 'rk' }, t).textContent = (rsMode ? S[k].p[n - 1] : ranks[k][n - 1]) + ' ';
      el('tspan', { class: 'tk' }, t).textContent = S[k].t;
      const sv = S[k].s[n - 1];
      if (G0 && offScale(sv)) {
        el('tspan', { class: 'nm', dx: 5 }, t).textContent = (sv > 0 ? '↑ ' : '↓ ') + fmtScore(sv) + ' 超出刻度';
      } else if (!narrow && S[k].name) el('tspan', { class: 'nm', dx: 5 }, t).textContent = shortName(S[k].name);
      // a wide transparent strip so the whole label row is the hit target
      el('rect', { x: xe + 4, y: y - gap / 2, width: W - xe - 4, height: gap, fill: 'transparent' }, g);
      g.addEventListener('pointerenter', () => setHover(k, null));
      g.addEventListener('pointerleave', () => setHover(null, null));
      g.addEventListener('click', () => togglePin(k));
      labEls.set(k, g);
    });

    const cross = el('line', { class: 'cross', y1: m.t, y2: m.t + ph, visibility: 'hidden' }, svg);
    const hovDot = el('circle', { class: 'dot', r: 4.5, visibility: 'hidden' }, svg);
    const hit = el('rect', { class: 'hit', x: m.l, y: m.t, width: pw, height: ph }, svg);
    hit.addEventListener('pointermove', onMove);
    hit.addEventListener('pointerleave', () => setHover(null, null));
    hit.addEventListener('click', () => { if (state.hover != null) togglePin(state.hover); });

    G = { W, H, m, pw, ph, cnt, i0, X, Y, val, pathOf, hovPath, cross, hovDot, labEls };
  }

  function onMove(ev) {
    const box = svg.getBoundingClientRect();
    const px = (ev.clientX - box.left) * (G.W / box.width);
    const py = (ev.clientY - box.top) * (G.H / box.height);
    let i = G.cnt === 1 ? G.i0 : Math.round((px - G.m.l) / G.pw * (G.cnt - 1)) + G.i0;
    i = Math.max(G.i0, Math.min(n - 1, i));
    // nearest line at that day; pinned, then thick, lines win near-ties
    let best = null, bd = 16;
    for (let k = 0; k < N; k++) {
      const v = G.val(k, i);
      if (v == null) continue;
      const d = Math.abs(G.Y(v) - py) + (slotOf(k) >= 0 ? 0 : k < TOP ? 2 : 4);
      if (d < bd) { bd = d; best = k; }
    }
    setHover(best, i, px, py);
  }

  function setHover(k, i, px, py) {
    state.hover = k;
    svg.classList.toggle('dim', k != null);
    if (k != null) {
      G.hovPath.setAttribute('d', G.pathOf(k));
      G.hovPath.style.stroke = colorOf(k) || 'var(--ink)';
      G.hovPath.setAttribute('visibility', 'visible');
    } else G.hovPath.setAttribute('visibility', 'hidden');
    G.labEls.forEach((g, key) => g.classList.toggle('is-hover', key === k));
    listEl.querySelectorAll('.row').forEach(r => r.classList.toggle('is-hover', Number(r.dataset.k) === k));

    if (i == null) {
      G.cross.setAttribute('visibility', 'hidden');
      G.hovDot.setAttribute('visibility', 'hidden');
      tip.hidden = true;
      return;
    }
    const x = G.X(i);
    G.cross.setAttribute('x1', x); G.cross.setAttribute('x2', x);
    G.cross.setAttribute('visibility', 'visible');
    const v = k != null ? G.val(k, i) : null;
    if (v != null) {
      G.hovDot.setAttribute('cx', x); G.hovDot.setAttribute('cy', G.Y(v));
      G.hovDot.style.fill = colorOf(k) || 'var(--ink)';
      G.hovDot.setAttribute('visibility', 'visible');
    } else G.hovDot.setAttribute('visibility', 'hidden');
    showTip(i, k, px, py);
  }

  function showTip(i, k, px, py) {
    tip.replaceChildren();
    const d = new Date(dates[i] + 'T00:00:00');
    h('div', 'd', dates[i] + ' 周' + WEEK[d.getDay()], tip);
    if (k != null) {
      const main = h('div', 'main', null, tip);
      const who = h('div', 'who', null, main);
      const key = h('span', 'key', null, who);
      key.style.background = colorOf(k) || 'var(--ink)';
      key.style.marginRight = '6px';
      who.appendChild(document.createTextNode(S[k].t));
      if (S[k].name) h('span', 'n', S[k].name, who);
      const valEl = h('div', 'val', null, main);
      h('b', '', 'RS ' + S[k].p[i], valEl);
      valEl.appendChild(document.createTextNode(' · 第 ' + ranks[k][i] + ' 名 '));
      const [txt, cls] = delta(k, i);
      if (txt) h('span', cls, txt, valEl);
      valEl.appendChild(document.createTextNode(' · 超额 ' + fmtScore(S[k].s[i])));
      if (S[k].holdings) h('div', 'hold', '持仓:' + S[k].holdings, main);
    }
    const others = [];
    state.pins.forEach(t => {
      if (t == null) return;
      const j = known.get(t);
      if (j !== k && ranks[j][i] != null) others.push(j);
    });
    others.sort((a, b) => ranks[a][i] - ranks[b][i]);
    if (others.length) {
      const rows = h('div', k != null ? 'rows' : '', null, tip);
      others.forEach(j => {
        const r = h('div', 'r', null, rows);
        h('span', 'key', null, r).style.background = colorOf(j);
        h('span', 't', S[j].t, r);
        h('b', '', 'RS ' + S[j].p[i], r);
        h('span', 't', '#' + ranks[j][i], r);
        h('span', 't', fmtScore(S[j].s[i]), r);
      });
    } else if (k == null) h('div', 'd', '移到线条上查看该 ETF', tip);
    tip.hidden = false;
    const scale = svg.getBoundingClientRect().width / G.W;
    const cx = px * scale + 6, cy = py * scale;
    const tw = tip.offsetWidth, th = tip.offsetHeight, cw = chartEl.clientWidth;
    let left = cx + 16;
    if (left + tw > cw - 4) left = cx - 16 - tw;
    tip.style.left = Math.max(4, left) + 'px';
    tip.style.top = Math.max(4, Math.min(cy - 24, chartEl.clientHeight - th - 4)) + 'px';
  }

  // ---------- ranking list (also the table view of the latest day) ----------
  function renderList() {
    listEl.replaceChildren();
    const last = n - 1;
    h('h2', '', '当日排名 · ' + dates[last] + '(RS 评分 / 超额收益)', listEl);
    const order = S.map((_, k) => k).sort((a, b) =>
      (ranks[a][last] == null) - (ranks[b][last] == null) || (ranks[a][last] - ranks[b][last]) || (a - b));
    let below = false;
    order.forEach((k, pos) => {
      if (!below && pos > 0 && !(S[k].p[last] >= RS_LINE)) {
        below = true;
        h('div', 'split', 'RS ' + RS_LINE + ' 以下', listEl);
      }
      if (pos === TOP && N > TOP) h('div', 'split', '第 ' + (TOP + 1) + ' 名之后(图中为细线)', listEl);
      const c = colorOf(k);
      const row = h('button', 'row' + (c ? ' is-pin' : '') + (pos >= TOP ? ' rest' : ''), null, listEl);
      row.type = 'button';
      row.dataset.k = k;
      row.setAttribute('aria-pressed', String(!!c));
      h('span', 'rk', ranks[k][last] == null ? '–' : ranks[k][last], row);
      const [txt, cls] = delta(k, last);
      h('span', 'dl ' + cls, txt, row);
      const key = h('span', 'key', null, row);
      if (c) key.style.background = c;
      const who = h('span', 'who', null, row);
      h('span', 'tk', S[k].t, who);
      if (S[k].name) h('span', 'nm', S[k].name, who);
      const sc = h('span', 'sc', null, row);
      h('b', '', S[k].p[last] == null ? '–' : S[k].p[last], sc);
      sc.appendChild(document.createTextNode(fmtScore(S[k].s[last])));
      if (S[k].holdings) h('span', 'hd', S[k].holdings, row);
      row.addEventListener('pointerenter', () => setHover(k, null));
      row.addEventListener('pointerleave', () => setHover(null, null));
      row.addEventListener('focus', () => setHover(k, null));
      row.addEventListener('blur', () => setHover(null, null));
      row.addEventListener('click', () => togglePin(k));
    });
  }

  // ---------- boot ----------
  $('sub').textContent = '数据截至 ' + dates[n - 1] + ' · 相对 ' + D.benchmark + ' · ' + N + ' 只 ETF · '
    + n + ' 个交易日 · RS 评分为名单内百分位(非全市场)· 生成于 ' + D.generated;
  $('hint').textContent = HINT;
  segmented($('range'), RANGES, 'range');
  segmented($('mode'), MODES, 'mode');
  $('clip').addEventListener('change', ev => {
    state.clip = ev.target.checked; store.set('clip', state.clip); render();
  });
  $('clear').addEventListener('click', () => {
    state.pins.fill(null); store.set('pins', state.pins); render(); renderList();
  });
  syncControls();
  renderList();
  render();
  let lastW = chartEl.clientWidth;
  new ResizeObserver(() => {
    if (chartEl.clientWidth !== lastW) { lastW = chartEl.clientWidth; render(); }
  }).observe(chartEl);
})();
</script>
</body>
</html>
"""
