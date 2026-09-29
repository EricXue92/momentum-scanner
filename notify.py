"""ntfy.sh push notifications. Single side-effect: HTTP POST.

Failures are swallowed and logged. Never raises — same contract as futu_sync.
"""

import logging
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 5.0


def _format_body(tickers: list[str], max_in_body: int, total: int) -> str:
    shown = tickers[:max_in_body]
    extra = len(tickers) - len(shown)
    parts = [", ".join(shown)]
    if extra > 0:
        parts.append(f"(+{extra} more)")
    parts.append(f" · total: {total}")
    return " ".join(parts)


def _ntfy_post(server: str, topic: str, title: str, body: str, priority: str) -> bool:
    """POST one message. Returns True when ntfy accepted it, False otherwise."""
    url = f"{server}/{topic}"
    req = Request(
        url,
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": priority},
        method="POST",
    )
    try:
        with urlopen(req, timeout=_TIMEOUT_SEC) as resp:
            logger.info(f"[Notify] pushed: {title} (HTTP {resp.status})")
            return True
    except (URLError, TimeoutError, OSError) as e:
        logger.warning(f"[Notify] ntfy POST failed: {e}")
    except Exception as e:
        logger.warning(f"[Notify] unexpected error: {e}")
    return False


def notify_morning_gap(
    new_tickers: list[str],
    offset_min: int,
    total: int,
    config: dict,
    promoted: list[str] | None = None,
    market: str = "US",
    all_tickers: list[str] | None = None,
) -> None:
    """Push ntfy alert(s) for a morning-gap scan.

    Args:
        new_tickers: brand-new tickers this scan (not seen in any earlier
            scan of the same phase, and — for post-open — not in pre-market
            seen either). Decides *whether* the regular alert fires and the
            "N new" count in its title.
        offset_min: minutes from market open. Negative = pre-market.
        total: total ticker count for this scan (including repeats).
        config: full parsed config.toml dict. Reads [notify] section.
        promoted: tickers that appeared in pre-market and are now first-confirmed
            post-open (post-open volume gate just crossed). Fires a separate
            high-priority alert. Always empty for pre-market scans and for
            HK (HK has no pre phase).
        market: "US" or "HK" — controls the title prefix only.
        all_tickers: the full scan list. When given, the regular alert body
            lists every ticker (not just the delta), so a "2 new / total 33"
            scan still shows all 33 names. Falls back to new_tickers if None.
    """
    promoted = promoted or []
    if not new_tickers and not promoted:
        return

    notify_cfg = config.get("notify") or {}
    if not notify_cfg.get("enabled", False):
        return

    topic = notify_cfg.get("ntfy_topic")
    if not topic:
        logger.warning("[Notify] ntfy_topic missing in [notify] config")
        return

    server = notify_cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    max_in_body = int(notify_cfg.get("max_tickers_in_body", 10))
    sign = "" if offset_min < 0 else "+"
    prefix = "HK Morning Gap" if market == "HK" else "Morning Gap"

    if new_tickers:
        title = f"{prefix} {sign}{offset_min}min | {len(new_tickers)} new"
        body_tickers = all_tickers if all_tickers is not None else new_tickers
        body = _format_body(body_tickers, max_in_body, total)
        _ntfy_post(server, topic, title, body, priority="default")

    if promoted:
        title = f"{prefix} {sign}{offset_min}min | {len(promoted)} PROMOTED"
        body = (
            _format_body(promoted, max_in_body, total)
            + " · pre-market gap confirmed by RTH volume"
        )
        _ntfy_post(server, topic, title, body, priority="high")


def notify_morning_catalyst_ready(
    *,
    report_path: Path,
    offset_min: int,
    n_tickers: int,
    config: dict,
) -> None:
    """Push the second-stage ntfy when the catalyst report is written.

    Independent from `notify_morning_gap` — that one fires immediately
    with the ticker list; this one fires a few minutes later with the
    report link. Same ntfy topic, different title.
    """
    notify_cfg = config.get("notify") or {}
    if not notify_cfg.get("enabled", False):
        return

    topic = notify_cfg.get("ntfy_topic")
    if not topic:
        logger.warning("[Notify] ntfy_topic missing for catalyst report")
        return

    server = notify_cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    sign = "" if offset_min < 0 else "+"
    title = f"Catalyst Report Ready ({sign}{offset_min}min, {n_tickers} tickers)"
    body = str(report_path)
    _ntfy_post(server, topic, title, body, priority="default")


def notify_scan_skipped(mode: str, reason: str, config: dict) -> None:
    """Push a high-priority ntfy alert when a scheduled scan is skipped.

    Used by the net-ready gate: a morning-gap run that clean-exits because
    the network never came up loses its scan window silently (observed
    2026-08-27 — Clash DNS flap killed all three pre-market scans with no
    trace outside the log). Best-effort: if the network is still down the
    POST fails and is swallowed by `_ntfy_post`, preserving the clean-exit
    contract.
    """
    notify_cfg = config.get("notify") or {}
    if not notify_cfg.get("enabled", False):
        return

    topic = notify_cfg.get("ntfy_topic")
    if not topic:
        logger.warning("[Notify] ntfy_topic missing for scan-skipped alert")
        return

    server = notify_cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    title = f"SKIPPED: {mode} scan"
    _ntfy_post(server, topic, title, reason, priority="high")


def notify_opend_down(
    mode: str, host: str, port: int, config: dict, output_dir: Path, today: str
) -> None:
    """Push a high-priority ntfy alert when Futu OpenD is unreachable.

    Every Futu consumer soft-fails, so a dead OpenD only leaves WARNINGs in
    the log while scans come back empty (2026-09-28: OpenD died in the
    afternoon; HK EOD, all 9 US morning-gap scans and the next day's runs
    went by silently). Fires **once per day** across all modes — the marker
    `state/opend_down_alerted_<today>.txt` is written only after a
    successful POST, so a failed push is retried by the next scan.
    """
    notify_cfg = config.get("notify") or {}
    if not notify_cfg.get("enabled", False):
        return

    topic = notify_cfg.get("ntfy_topic")
    if not topic:
        logger.warning("[Notify] ntfy_topic missing for OpenD-down alert")
        return

    marker = output_dir / "state" / f"opend_down_alerted_{today}.txt"
    try:
        if marker.exists():
            return
    except OSError:
        pass

    server = notify_cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    title = "OpenD DOWN: Futu scans are failing"
    body = (
        f"{mode}: OpenD not reachable at {host}:{port}. "
        "Morning-gap scans / HK data / Futu sync are skipped until it is "
        "started and logged in."
    )
    if not _ntfy_post(server, topic, title, body, priority="high"):
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{mode}\n")
    except OSError as e:
        logger.warning(f"[Notify] could not write {marker.name}: {e}")
