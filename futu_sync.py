"""Sync watchlists to Futu (富途牛牛) via OpenAPI.

Requires FutuOpenD running locally (default 127.0.0.1:11111) and the user
to have pre-created custom watchlist groups matching the configured names —
the API can only modify custom groups, not create them.

The .txt watchlist files remain the primary artifact; failures here log a
warning and return False without raising.
"""

import logging
import socket
from typing import Literal, NamedTuple

logger = logging.getLogger(__name__)


class GapQuote(NamedTuple):
    """One discovery survivor's live comparison basis and its gap.

    ``price`` is what the trader is looking at right now — the pre-market
    print during a negative-offset scan, the last trade during a post-open
    one. The morning-gap trend gate compares against it instead of the
    previous close, which is blind to the very move that surfaced the ticker.
    ``gap`` is the same percentage the discovery threshold was applied to.
    ``pre_volume`` is Futu's accumulated pre-market share volume (US
    pre-market scans only; ``None`` post-open / HK) — the pre-market volume
    gate uses it to reject gaps printed on a handful of thin trades.
    """

    price: float
    gap: float
    pre_volume: float | None = None


def _opend_reachable(host: str, port: int, timeout: float = 1.5) -> bool:
    """Quick TCP probe — OpenQuoteContext retries forever on a closed port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _to_futu_code(ticker: str, market: Literal["US", "HK"]) -> str | None:
    """Convert internal ticker to Futu market.code format.

    US: AAPL → US.AAPL
    HK: HKEX:0522 / 522 / 0522.HK → HK.00522 (5-digit zero-padded)
    """
    t = ticker.strip()
    if not t:
        return None
    if market == "US":
        return f"US.{t}"
    if market == "HK":
        if t.startswith("HKEX:"):
            t = t[5:]
        t = t.replace(".HK", "")
        try:
            n = int(t)
        except ValueError:
            return None
        return f"HK.{n:05d}"
    return None


# Retained one release as a fallback after morning-gap discovery moved to
# `discover_morning_gap_candidates`. Safe to remove once Futu-discovery has
# proven stable in production (re-evaluate after 2026-06-01).
def pre_market_gap_futu(
    tickers: list[str],
    min_gap_pct: float,
    host: str = "127.0.0.1",
    port: int = 11111,
) -> list[str] | None:
    """Filter US tickers by pre-market gap % via Futu OpenAPI snapshot.
    Reads ``pre_change_rate`` (already in percent units) from
    ``get_market_snapshot`` — real-time on US Lv1 BBO accounts. Tickers with
    no pre-market trades (``pre_volume == 0``) are dropped.

    Returns the surviving subset (in input ticker format), or ``None`` on
    any failure so the caller can fall back to another data source.
    """
    if not tickers:
        return []
    try:
        from futu import OpenQuoteContext, RET_OK
    except ImportError:
        logger.warning("  Futu pre-market: futu-api not installed")
        return None

    if not _opend_reachable(host, port):
        logger.warning(
            f"  Futu pre-market: OpenD not reachable at {host}:{port}"
        )
        return None

    code_to_ticker: dict[str, str] = {}
    for t in tickers:
        c = _to_futu_code(t, "US")
        if c:
            code_to_ticker[c] = t
    if not code_to_ticker:
        return []

    ctx = None
    try:
        ctx = OpenQuoteContext(host=host, port=port)
        ret, data = ctx.get_market_snapshot(list(code_to_ticker.keys()))
        if ret != RET_OK:
            logger.warning(f"  Futu pre-market: get_market_snapshot failed — {data}")
            return None

        result: list[str] = []
        for _, row in data.iterrows():
            code = row.get("code")
            ticker = code_to_ticker.get(code)
            if ticker is None:
                continue
            try:
                pre_vol = float(row.get("pre_volume", 0) or 0)
            except (TypeError, ValueError):
                pre_vol = 0
            if pre_vol <= 0:
                logger.info(f"  {ticker}: no pre-market trades yet, dropping")
                continue
            try:
                gap = float(row.get("pre_change_rate"))
            except (TypeError, ValueError):
                logger.info(f"  {ticker}: pre_change_rate unavailable, dropping")
                continue
            if gap >= min_gap_pct:
                result.append(ticker)
            else:
                logger.info(
                    f"  {ticker}: pre-market gap {gap:+.2f}% < +{min_gap_pct}%, dropping"
                )
        return result
    except Exception as e:
        logger.warning(f"  Futu pre-market: unexpected error — {e}")
        return None
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def get_market_caps_futu(
    tickers: list[str],
    market: Literal["US", "HK"],
    host: str = "127.0.0.1",
    port: int = 11111,
) -> dict[str, float] | None:
    """Bulk market-cap lookup via Futu snapshot. Returns
    ``{input_ticker: total_market_val}`` in native currency (USD for US,
    HKD for HK). Tickers with no/zero ``total_market_val`` are omitted
    from the result rather than mapped to 0. Returns ``None`` on any
    failure so the caller can fall back to a per-ticker source.

    Batches at 400 codes/call (Futu snapshot API limit).
    """
    if not tickers:
        return {}
    try:
        from futu import OpenQuoteContext, RET_OK
    except ImportError:
        logger.warning("  Futu market cap: futu-api not installed")
        return None

    if not _opend_reachable(host, port):
        logger.warning(f"  Futu market cap: OpenD not reachable at {host}:{port}")
        return None

    code_to_ticker: dict[str, str] = {}
    for t in tickers:
        c = _to_futu_code(t, market)
        if c:
            code_to_ticker[c] = t
    if not code_to_ticker:
        return {}

    ctx = None
    try:
        ctx = OpenQuoteContext(host=host, port=port)
        result: dict[str, float] = {}
        codes = list(code_to_ticker.keys())
        BATCH = 400
        for i in range(0, len(codes), BATCH):
            batch = codes[i:i + BATCH]
            ret, data = ctx.get_market_snapshot(batch)
            if ret != RET_OK:
                logger.warning(
                    f"  Futu market cap: get_market_snapshot failed — {data}"
                )
                return None
            for _, row in data.iterrows():
                code = row.get("code")
                ticker = code_to_ticker.get(code)
                if ticker is None:
                    continue
                try:
                    cap = float(row.get("total_market_val", 0) or 0)
                except (TypeError, ValueError):
                    cap = 0
                if cap > 0:
                    result[ticker] = cap
        return result
    except Exception as e:
        logger.warning(f"  Futu market cap: unexpected error — {e}")
        return None
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def intraday_cumulative_volume_futu(
    tickers: list[str],
    avg_daily_volumes: dict[str, float],
    host: str = "127.0.0.1",
    port: int = 11111,
    market: Literal["US", "HK"] = "US",
) -> list[str] | None:
    """Filter tickers whose today's RTH cumulative volume >= their 20-day
    average daily volume. Reads ``volume`` from ``get_market_snapshot`` —
    that field is today's regular-session cumulative (separate from
    ``pre_volume`` / ``after_volume``), so calling it at e.g. 09:50 ET
    returns the first ~20 minutes of RTH volume.

    Returns the surviving subset (input ticker format), or ``None`` on any
    failure so the caller can fall back. Tickers with zero or missing
    avg_daily_volume entries are dropped.
    """
    if not tickers:
        return []
    try:
        from futu import OpenQuoteContext, RET_OK
    except ImportError:
        logger.warning("  Futu intraday volume: futu-api not installed")
        return None

    if not _opend_reachable(host, port):
        logger.warning(
            f"  Futu intraday volume: OpenD not reachable at {host}:{port}"
        )
        return None

    code_to_ticker: dict[str, str] = {}
    for t in tickers:
        c = _to_futu_code(t, market)
        if c:
            code_to_ticker[c] = t
    if not code_to_ticker:
        return []

    ctx = None
    try:
        ctx = OpenQuoteContext(host=host, port=port)
        ret, data = ctx.get_market_snapshot(list(code_to_ticker.keys()))
        if ret != RET_OK:
            logger.warning(f"  Futu intraday volume: get_market_snapshot failed — {data}")
            return None

        result: list[str] = []
        for _, row in data.iterrows():
            code = row.get("code")
            ticker = code_to_ticker.get(code)
            if ticker is None:
                continue
            avg = avg_daily_volumes.get(ticker)
            if avg is None or avg <= 0:
                continue
            try:
                vol = float(row.get("volume", 0) or 0)
            except (TypeError, ValueError):
                vol = 0
            if vol >= avg:
                result.append(ticker)
            else:
                logger.info(
                    f"  {ticker}: cumulative {vol:,.0f} < 20d avg {avg:,.0f}, dropping"
                )
        return result
    except Exception as e:
        logger.warning(f"  Futu intraday volume: unexpected error — {e}")
        return None
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def sync_to_futu(
    tickers: list[str],
    group_name: str,
    market: Literal["US", "HK"],
    host: str = "127.0.0.1",
    port: int = 11111,
    append_only: bool = False,
) -> bool:
    """Sync the ticker list to a Futu custom watchlist group.

    Computes the diff against the group's current contents and applies only
    the necessary ADD / DEL ops (saves API calls — limit is 10 per 30s).

    When ``append_only`` is True, the DEL phase is skipped: tickers are only
    added, never removed. Used for shared/merged groups (e.g. multiple
    scanners feeding into one Futu group) so each scanner doesn't clobber
    others' contributions. The group accumulates monotonically across runs.
    """
    try:
        from futu import OpenQuoteContext, ModifyUserSecurityOp, RET_OK
    except ImportError:
        logger.warning(f"  futu-api not installed; skipping Futu sync for '{group_name}'")
        return False

    futu_codes = [c for t in tickers if (c := _to_futu_code(t, market))]
    if not futu_codes:
        logger.info(f"  Futu sync ({group_name}): no tickers to sync")
        return False
    desired = set(futu_codes)

    if not _opend_reachable(host, port):
        logger.warning(
            f"  Futu sync ({group_name}): OpenD not reachable at {host}:{port}, skipping"
        )
        return False

    ctx = None
    try:
        ctx = OpenQuoteContext(host=host, port=port)
    except Exception as e:
        logger.warning(
            f"  Futu sync ({group_name}): cannot connect to OpenD at {host}:{port} — {e}"
        )
        return False

    try:
        ret, data = ctx.get_user_security(group_name)
        if ret != RET_OK:
            logger.warning(f"  Futu sync ({group_name}): get_user_security failed — {data}")
            return False

        if hasattr(data, "columns") and "code" in data.columns:
            current = set(data["code"].tolist())
        else:
            current = set()

        to_add = sorted(desired - current)
        to_del = [] if append_only else sorted(current - desired)

        if to_del:
            ret, msg = ctx.modify_user_security(group_name, ModifyUserSecurityOp.DEL, to_del)
            if ret != RET_OK:
                logger.warning(f"  Futu sync ({group_name}): DEL failed — {msg}")
        if to_add:
            ret, msg = ctx.modify_user_security(group_name, ModifyUserSecurityOp.ADD, to_add)
            if ret != RET_OK:
                logger.warning(f"  Futu sync ({group_name}): ADD failed — {msg}")
                return False

        final_size = len(current | desired) if append_only else len(desired)
        logger.info(
            f"  Futu sync ({group_name}): +{len(to_add)} -{len(to_del)} "
            f"({final_size} tickers in group{', append-only' if append_only else ''})"
        )
        return True
    except Exception as e:
        logger.warning(f"  Futu sync ({group_name}): unexpected error — {e}")
        return False
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def discover_morning_gap_candidates(
    min_gap_pct: float,
    min_market_cap: float,
    min_price: float,
    pre_market: bool,
    exchanges: list[str],
    host: str = "127.0.0.1",
    port: int = 11111,
) -> dict[str, GapQuote] | None:
    """Discover US morning-gap candidates via Futu snapshot.

    Pipeline:
      1. ``get_stock_basicinfo(market=US, stock_type=STOCK)``.
      2. Filter rows to ``exchange_type in exchanges`` and not delisted.
      3. ``get_market_snapshot`` in batches of 400.
      4. Keep tickers with ``total_market_val >= min_market_cap``,
         ``last_price >= min_price``, and gap above ``min_gap_pct`` —
         pre-market uses ``pre_change_rate`` (and ``pre_volume > 0``);
         post-open uses ``(last_price - prev_close_price) / prev_close_price * 100``
         (the snapshot DataFrame has no plain ``change_rate`` column in this SDK
         version, so we derive it).

    Returns ``{plain_ticker: GapQuote}`` (e.g. ``{"TWLO": GapQuote(...)}``,
    not ``"US.TWLO"``), insertion-ordered. ``price`` is ``pre_price`` during a
    pre-market scan (falling back to ``last_price`` when Futu reports
    ``N/A``) and ``last_price`` post-open. Returns ``None`` on any failure so
    callers can decide whether to fall back. Logs a single warning per
    failure mode.
    """
    try:
        from futu import OpenQuoteContext, RET_OK, Market, SecurityType
    except ImportError:
        logger.warning("  Futu discovery: futu-api not installed")
        return None

    if not _opend_reachable(host, port):
        logger.warning(f"  Futu discovery: OpenD not reachable at {host}:{port}")
        return None

    exchanges_set = set(exchanges)
    ctx = None
    try:
        ctx = OpenQuoteContext(host=host, port=port)
        ret, basic = ctx.get_stock_basicinfo(
            market=Market.US, stock_type=SecurityType.STOCK
        )
        if ret != RET_OK:
            logger.warning(f"  Futu discovery: get_stock_basicinfo failed — {basic}")
            return None
        if basic is None or len(basic) == 0:
            logger.warning("  Futu discovery: empty basicinfo result")
            return None

        # Note: `suspension` is a string column ("N/A") in this SDK, not a bool —
        # comparing to False matches 0 rows. We rely on `delisting` (bool) plus
        # the exchange whitelist; that yields ~7k US common-stock tickers.
        mask = (
            basic["exchange_type"].isin(exchanges_set)
            & (basic["delisting"] == False)  # noqa: E712
        )
        codes = basic.loc[mask, "code"].tolist()
        logger.info(
            f"  Futu discovery: basicinfo={len(basic)} "
            f"after exchange/delisting filter={len(codes)}"
        )
        if not codes:
            return {}

        survivors: dict[str, GapQuote] = {}
        BATCH = 400
        for i in range(0, len(codes), BATCH):
            batch = codes[i:i + BATCH]
            ret, snap = ctx.get_market_snapshot(batch)
            if ret != RET_OK:
                logger.warning(
                    f"  Futu discovery: snapshot batch {i}-{i + len(batch)} "
                    f"failed — {snap}"
                )
                return None
            for _, row in snap.iterrows():
                code = row.get("code")
                if not code or not code.startswith("US."):
                    continue
                try:
                    cap = float(row.get("total_market_val", 0) or 0)
                    price = float(row.get("last_price", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if cap < min_market_cap or price < min_price:
                    continue
                if pre_market:
                    try:
                        pre_vol = float(row.get("pre_volume", 0) or 0)
                        gap = float(row.get("pre_change_rate"))
                    except (TypeError, ValueError):
                        continue
                    if pre_vol <= 0 or gap < min_gap_pct:
                        continue
                    # pre_price is "N/A" outside the pre-auction window; the
                    # last regular-session trade is the honest fallback.
                    try:
                        basis = float(row.get("pre_price"))
                    except (TypeError, ValueError):
                        basis = 0.0
                    if basis <= 0:
                        basis = price
                    survivors[code[len("US."):]] = GapQuote(
                        price=basis, gap=gap, pre_volume=pre_vol
                    )
                    continue
                else:
                    try:
                        prev_close = float(row.get("prev_close_price", 0) or 0)
                        if prev_close <= 0:
                            continue
                        gap = (price - prev_close) / prev_close * 100.0
                    except (TypeError, ValueError):
                        continue
                    if gap < min_gap_pct:
                        continue
                    basis = price
                survivors[code[len("US."):]] = GapQuote(price=basis, gap=gap)

        logger.info(
            f"  Futu discovery: {len(survivors)} candidates "
            f"({'pre-market' if pre_market else 'post-open'}, "
            f"gap>={min_gap_pct}%, cap>=${min_market_cap:,.0f}, "
            f"price>=${min_price:.2f})"
        )
        return survivors
    except Exception as e:
        logger.warning(f"  Futu discovery: unexpected error — {e}")
        return None
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def discover_hk_morning_gap_candidates(
    min_gap_pct: float,
    min_market_cap: float,
    min_price: float,
    exchanges: list[str],
    host: str = "127.0.0.1",
    port: int = 11111,
) -> dict[str, GapQuote] | None:
    """Discover HK morning-gap candidates via Futu snapshot (post-open only).

    HK does not have a US-style pre-market session — Futu snapshot fields
    ``pre_change_rate`` / ``pre_volume`` / ``pre_price`` return ``N/A`` for
    all HK tickers regardless of time-of-day (verified 2026-05-11). HK's
    pre-auction (09:00–09:20 HKT) IEP/IEV are not exposed by the snapshot
    API at our account permission level. So this function only does the
    post-open path: gap = (last_price - prev_close_price) / prev_close_price.

    Returns ``{yfinance_ticker: GapQuote}`` (e.g. ``{"0700.HK": GapQuote(...)}``)
    so the keys feed directly into the existing HK yfinance metrics pipeline.
    ``price`` is ``last_price`` — HK has no usable pre-market basis. Returns
    ``None`` on any failure.
    """
    try:
        from futu import OpenQuoteContext, RET_OK, Market, SecurityType
    except ImportError:
        logger.warning("  Futu HK discovery: futu-api not installed")
        return None

    if not _opend_reachable(host, port):
        logger.warning(f"  Futu HK discovery: OpenD not reachable at {host}:{port}")
        return None

    exchanges_set = set(exchanges)
    ctx = None
    try:
        ctx = OpenQuoteContext(host=host, port=port)
        ret, basic = ctx.get_stock_basicinfo(
            market=Market.HK, stock_type=SecurityType.STOCK
        )
        if ret != RET_OK:
            logger.warning(f"  Futu HK discovery: get_stock_basicinfo failed — {basic}")
            return None
        if basic is None or len(basic) == 0:
            logger.warning("  Futu HK discovery: empty basicinfo result")
            return None

        # Probe (2026-05-11): `delisting` is bool for HK basicinfo too — all
        # False on a healthy day. Filter by exchange_type whitelist (defaults
        # to HK_MAINBOARD; ~2,400 names) and drop delisted rows.
        mask = (
            basic["exchange_type"].isin(exchanges_set)
            & (basic["delisting"] == False)  # noqa: E712
        )
        codes = basic.loc[mask, "code"].tolist()
        logger.info(
            f"  Futu HK discovery: basicinfo={len(basic)} "
            f"after exchange/delisting filter={len(codes)}"
        )
        if not codes:
            return {}

        survivors: dict[str, GapQuote] = {}
        BATCH = 400
        for i in range(0, len(codes), BATCH):
            batch = codes[i:i + BATCH]
            ret, snap = ctx.get_market_snapshot(batch)
            if ret != RET_OK:
                logger.warning(
                    f"  Futu HK discovery: snapshot batch {i}-{i + len(batch)} "
                    f"failed — {snap}"
                )
                return None
            for _, row in snap.iterrows():
                code = row.get("code")
                if not code or not code.startswith("HK."):
                    continue
                try:
                    cap = float(row.get("total_market_val", 0) or 0)
                    price = float(row.get("last_price", 0) or 0)
                    prev_close = float(row.get("prev_close_price", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if cap < min_market_cap or price < min_price or prev_close <= 0:
                    continue
                gap = (price - prev_close) / prev_close * 100.0
                if gap < min_gap_pct:
                    continue
                # Strip "HK." prefix and convert "HK.00700" → "0700.HK" so
                # the result feeds directly into yfinance (HK pipeline uses
                # the 4-digit + ".HK" form; TV/Futu conversion happens later).
                num_part = code[len("HK."):]
                try:
                    n = int(num_part)
                except ValueError:
                    continue
                survivors[f"{n:04d}.HK"] = GapQuote(price=price, gap=gap)

        logger.info(
            f"  Futu HK discovery: {len(survivors)} candidates "
            f"(post-open, gap>={min_gap_pct}%, "
            f"cap>=HK${min_market_cap:,.0f}, price>=HK${min_price:.2f})"
        )
        return survivors
    except Exception as e:
        logger.warning(f"  Futu HK discovery: unexpected error — {e}")
        return None
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass
