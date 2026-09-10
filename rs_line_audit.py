"""RS-line direction audit over the cross-day 'seen' masters.

Scores every already-surfaced ticker (output/state/eod_seen_{US,HK}.txt) by its
RS-line 21EMA 5-bar direction, prints the full distribution weakest-first with
the tolerance cut line marked, and **prunes the drops from the master** so they
can re-qualify on a future EOD run. Backs the master up first
(``eod_seen_<MKT>.txt.bak.<timestamp>``). No Futu side effects. yfinance is
acceptable here (manual, one-off, bounded list).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import rs_line

logger = logging.getLogger(__name__)


def _hk_master_to_futu(entry: str) -> str:
    """'HKEX:522' / '522' -> Futu key 'HK.00522' (5-digit, zero-padded)."""
    code = entry.split(":", 1)[-1].strip()
    return f"HK.{int(code):05d}"


def classify(
    ids: list[str],
    direction: pd.DataFrame,
    reversal: pd.DataFrame,
    tolerance: float,
    adr_mult: float,
) -> dict:
    """Score ids by RS-line 21EMA 5-bar slope and split into drop / keep / unknown.

    Returns a dict with:
      - scored_asc: ids sorted weakest-first (ascending chg_5d) — for the report.
      - drops: ids with chg < -tolerance AND ret_per_adr < adr_mult (or missing),
               in weakest-first order.
      - exempts: ids with chg < -tolerance AND ret_per_adr >= adr_mult.
      - keeps_ranked: kept ids sorted by RS strength desc (chg_5d desc), with
                      unknowns appended at the end.
      - unknowns: ids absent from direction or with NaN chg_5d.
    """
    scored = [i for i in ids if i in direction.index
              and pd.notna(direction.loc[i, "rs_ema_chg_5d"])]
    unknowns = [i for i in ids if i not in scored]
    scored.sort(key=lambda i: float(direction.loc[i, "rs_ema_chg_5d"]))

    def _rpa(i):
        if i in reversal.index and pd.notna(reversal.loc[i, "ret_per_adr"]):
            return float(reversal.loc[i, "ret_per_adr"])
        return None

    drops, exempts, kept_scored = [], [], []
    for i in scored:
        chg = float(direction.loc[i, "rs_ema_chg_5d"])
        if chg < -tolerance:
            rpa = _rpa(i)
            if rpa is not None and rpa >= adr_mult:
                exempts.append(i)
                kept_scored.append(i)
            else:
                drops.append(i)
        else:
            kept_scored.append(i)

    keeps_ranked = sorted(
        kept_scored,
        key=lambda i: float(direction.loc[i, "rs_ema_chg_5d"]),
        reverse=True,
    ) + list(unknowns)

    return {
        "scored_asc": scored,
        "drops": drops,
        "exempts": exempts,
        "keeps_ranked": keeps_ranked,
        "unknowns": unknowns,
    }


def render_report(
    ids: list[str],
    direction: pd.DataFrame,
    reversal: pd.DataFrame,
    tolerance: float,
    adr_mult: float,
    market: str,
    as_of: str,
    anomaly_ids: set[str] | None = None,
) -> str:
    """Build the audit text. ``direction`` indexed by id (rs_ema, rs_ema_chg_5d);
    ``reversal`` indexed by id (adr_pct, ret_lb, ret_per_adr). A direction-cut
    (chg < -tolerance) is EXEMPT ('reversing') when ret_per_adr >= adr_mult, else
    DROP. Ids absent from ``direction`` are 'unknown' (kept downstream).

    ``anomaly_ids`` (optional) lets the report split the unknown bucket into
    "data anomaly" (split / bad quote) vs "insufficient history" — same KEPT
    semantics either way."""
    buckets = classify(ids, direction, reversal, tolerance, adr_mult)
    scored = buckets["scored_asc"]
    unknown = buckets["unknowns"]
    anomaly_ids = anomaly_ids or set()
    anomaly_unknown = [u for u in unknown if u in anomaly_ids]
    short_unknown   = [u for u in unknown if u not in anomaly_ids]
    drop_set = set(buckets["drops"])
    exempt_set = set(buckets["exempts"])

    def _rpa(i):
        if i in reversal.index and pd.notna(reversal.loc[i, "ret_per_adr"]):
            return float(reversal.loc[i, "ret_per_adr"])
        return None

    body = []
    for rank, i in enumerate(scored, 1):
        chg = float(direction.loc[i, "rs_ema_chg_5d"])
        ema = float(direction.loc[i, "rs_ema"])
        rpa_s, flag = "", ""
        if chg < -tolerance:
            rpa = _rpa(i)
            rpa_s = f"{rpa:.2f}x" if rpa is not None else "—"
            flag = "EXEMPT" if i in exempt_set else "DROP"
        body.append(f"  {rank:>4}  {i:<14} {chg*100:>8.2f}%  {ema:>12.6f}  {rpa_s:>8}  {flag}")

    cut = len(drop_set) + len(exempt_set)
    exempt = len(exempt_set)
    lines = [
        f"RS-line direction audit — {market} — {as_of}",
        f"signal: RS-line 21EMA 5-bar slope  |  cut: chg < -{tolerance*100:.2f}%  "
        f"|  exempt if 5d-return >= {adr_mult:.2f}x ADR",
        "",
        f"  {'rank':>4}  {'ticker':<14} {'chg_5d':>9}  {'EMA21':>12}  {'ret/ADR':>8}  flag",
        *body,
        "",
    ]
    if short_unknown:
        lines.append(
            f"unknown — insufficient history (KEPT): {', '.join(short_unknown)}"
        )
    if anomaly_unknown:
        lines.append(
            f"unknown — data anomaly e.g. split (KEPT): {', '.join(anomaly_unknown)}"
        )
    lines.append(
        f"scanned: {len(ids)} | scored: {len(scored)} | "
        f"unknown: {len(unknown)} (short-hist: {len(short_unknown)}, anomaly: {len(anomaly_unknown)}) | "
        f"direction-cut: {cut} -> exempt(reversing): {exempt}, drop: {cut - exempt}"
    )
    return "\n".join(lines)


def write_top_n(
    buckets: dict,
    market: str,
    as_of: str,
    output_dir: Path,
    top_n: int,
) -> Path | None:
    """Write the strongest-N snapshot (``rs_us_<date>.txt`` / ``hk_rs_<date>.txt``).

    Takes the head of ``keeps_ranked`` minus unknowns (no score → never in a
    "strongest" list; they also never pad a short list). Returns the path, or
    None when there is nothing scored to write (empty-list convention: no
    0-byte artifacts). Same-day rerun overwrites — the file is a ranking
    snapshot, not a dedup-bearing watchlist. The US snapshot lands in
    ``TV/US/`` (importable straight into TradingView alongside the
    watchlists); HK stays at the output root.
    """
    unknown = set(buckets["unknowns"])
    top = [i for i in buckets["keeps_ranked"] if i not in unknown][:top_n]
    if not top:
        return None
    stem = "rs_us" if market == "us" else "hk_rs"
    target = output_dir / "TV" / "US" if market == "us" else output_dir
    target.mkdir(parents=True, exist_ok=True)
    out = target / f"{stem}_{as_of}.txt"
    out.write_text(",".join(top) + "\n")
    return out


def _load_seen(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]


def _prune_master(
    seen_path: Path,
    drops: list[str],
    dry_run: bool = False,
    label: str = "rs-line-audit",
) -> None:
    """Remove drop ids from the cross-day master, backing up first.

    Backup name: ``<master>.bak.YYYYmmdd_HHMMSS`` next to the master. Repeated
    audit runs accumulate backups — operator prunes them by hand when stale.
    ``dry_run=True`` logs what would change but writes nothing (no backup, no
    master mutation). ``label`` is the log prefix — other pruners (sma50_prune)
    reuse this function under their own name.
    """
    if not drops:
        logger.info(f"[{label}] no drops to prune from {seen_path.name}")
        return
    if not seen_path.exists():
        logger.warning(
            f"[{label}] master missing, cannot prune: {seen_path}"
        )
        return
    current = [ln.strip() for ln in seen_path.read_text().splitlines() if ln.strip()]
    drop_set = set(drops)
    kept = [t for t in current if t not in drop_set]
    removed = len(current) - len(kept)
    if removed == 0:
        logger.info(
            f"[{label}] {seen_path.name}: drops not present in master, nothing to do"
        )
        return
    if dry_run:
        logger.info(
            f"[{label}] DRY-RUN: would prune {seen_path.name}: "
            f"{len(current)} -> {len(kept)} (-{removed}); no backup written, master untouched"
        )
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = seen_path.parent / f"{seen_path.name}.bak.{stamp}"
    backup.write_text(seen_path.read_text())
    seen_path.write_text("\n".join(kept) + ("\n" if kept else ""))
    logger.info(
        f"[{label}] pruned {seen_path.name}: "
        f"{len(current)} -> {len(kept)} (-{removed}); backup: {backup.name}"
    )


AUDIT_SUBDIR = "rs-audit"


def write_audit_files(
    text: str,
    buckets: dict[str, list[str]],
    market: str,
    as_of: str,
    output_dir: Path,
) -> tuple[Path, Path, Path]:
    """Write the audit report + ``_drop`` / ``_keep_ranked`` sidecars.

    All three land in ``output_dir/rs-audit/`` (created on demand), named
    ``rs_line_audit_<MKT>_<date>{,_drop,_keep_ranked}.txt``. Sidecars are
    comma-separated; an empty bucket yields an empty file (no trailing
    newline) so the operator can still see the audit ran. Returns
    ``(report, drop, keep_ranked)`` paths.
    """
    target = output_dir / AUDIT_SUBDIR
    target.mkdir(parents=True, exist_ok=True)
    stem = f"rs_line_audit_{market.upper()}_{as_of}"
    report = target / f"{stem}.txt"
    report.write_text(text + "\n")
    drop = target / f"{stem}_drop.txt"
    drop.write_text(",".join(buckets["drops"]) + ("\n" if buckets["drops"] else ""))
    keep = target / f"{stem}_keep_ranked.txt"
    keep.write_text(
        ",".join(buckets["keeps_ranked"]) + ("\n" if buckets["keeps_ranked"] else "")
    )
    return report, drop, keep


def _audit_market(
    market: str,
    config: dict,
    output_dir: Path,
    dry_run: bool = False,
    assume_yes: bool = False,
) -> str | None:
    """Return the report text for one market, or None if its master is empty.

    Pruning gate: ``dry_run`` skips pruning entirely (no prompt). Otherwise,
    if ``assume_yes`` is True the master is pruned unconditionally; if False,
    the operator is prompted y/N after the report is printed and only confirmed
    drops are pruned.
    """
    seen_path = output_dir / "state" / f"eod_seen_{market.upper()}.txt"
    ids = _load_seen(seen_path)
    if not ids:
        logger.info(f"[rs-line-audit] {market.upper()} master empty — skipping")
        return None

    direction_kwargs = rs_line.direction_params_from_config(config)
    tolerance = rs_line.tolerance_from_config(config)

    if market == "us":
        from us_rs_3m import fetch_us_klines_yf
        bench = fetch_us_klines_yf(["SPY"], period="6mo", batch_size=1).get("SPY")
        klines = fetch_us_klines_yf(ids, period="6mo", include_ohlcv=True)  # keyed by symbol == master entry
    else:  # hk
        from hk_eod import fetch_hk_klines_yf, fetch_hsi_kline_yf
        bench = fetch_hsi_kline_yf(period="2y")
        codes_4d = [e.split(":", 1)[-1].strip().zfill(4) for e in ids]
        fetched = fetch_hk_klines_yf(codes_4d, period="2y")
        klines = {e: fetched.get(_hk_master_to_futu(e)) for e in ids}

    direction = rs_line.compute_rs_direction(klines, bench, **direction_kwargs)
    reversal = rs_line.compute_rs_reversal(klines, lookback=direction_kwargs["lookback"])
    anomaly_ids = rs_line.find_anomaly_ids(klines, bench, lookback=direction_kwargs["lookback"])
    adr_mult = rs_line.adr_mult_from_config(config)
    as_of = date.today().strftime("%Y-%m-%d")
    text = render_report(
        ids, direction, reversal, tolerance, adr_mult, market.upper(), as_of,
        anomaly_ids=anomaly_ids,
    )

    buckets = classify(ids, direction, reversal, tolerance, adr_mult)
    out_file, drop_file, keep_file = write_audit_files(
        text, buckets, market, as_of, output_dir
    )
    logger.info(f"[rs-line-audit] wrote {out_file}")
    logger.info(f"[rs-line-audit] wrote {drop_file} ({len(buckets['drops'])} ids)")
    logger.info(f"[rs-line-audit] wrote {keep_file} ({len(buckets['keeps_ranked'])} ids)")

    top_file = write_top_n(
        buckets, market, as_of, output_dir, rs_line.top_n_from_config(config)
    )
    if top_file is not None:
        logger.info(f"[rs-line-audit] wrote {top_file} (strongest top-N)")

    print("\n" + text + "\n")

    drops = buckets["drops"]
    if dry_run:
        logger.info(
            f"[rs-line-audit] {market.upper()}: --dry-run set, skipping prune "
            f"({len(drops)} drops left in {seen_path.name})"
        )
        return text

    if not drops:
        _prune_master(seen_path, drops, dry_run=False)  # logs the no-op
        return text

    if assume_yes:
        _prune_master(seen_path, drops, dry_run=False)
        return text

    # Interactive confirmation: report and sidecars already on disk; operator
    # may inspect drop_file before answering. Default is N to favor safety.
    prompt = (
        f"\n[rs-line-audit] {market.upper()}: prune {len(drops)} ticker(s) "
        f"from {seen_path.name}? (y/N): "
    )
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        answer = ""
    if answer in ("y", "yes"):
        _prune_master(seen_path, drops, dry_run=False)
    else:
        logger.info(
            f"[rs-line-audit] {market.upper()}: declined — {seen_path.name} "
            f"left unchanged. Re-run with --yes to auto-confirm, or inspect "
            f"{drop_file.name}."
        )
    return text


def run_audit(
    config: dict,
    output_dir: Path,
    market: str = "both",
    dry_run: bool = False,
    assume_yes: bool = False,
) -> int:
    """Audit entry point. Writes report + sidecars; pruning gate:
    ``dry_run`` skips prune entirely; ``assume_yes`` prunes without asking;
    otherwise prompts y/N per market after printing the report.
    Never touches Futu."""
    markets = ["us", "hk"] if market == "both" else [market]
    for m in markets:
        _audit_market(m, config, output_dir, dry_run=dry_run, assume_yes=assume_yes)
    return 0
