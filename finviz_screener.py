"""Drop-in replacement for `finviz.screener.Screener` against Finviz's
redesigned screener page.

2026-10-20 Finviz moved the screener from `screener.ashx` (server-rendered
HTML table) to `/screener/stocks` (301 from the old URL), with the rows
embedded as JSON in a `<script type="application/json">` SSR island. The
`finviz` 2.0.0 library still parses the old table, finds no rows and raises
`NoResults` — every US EOD screen (Longs, Leaders, Shorts, IPO) came back
empty with no error.

Same surface as the library class so `main.run_screener` and its tests are
unchanged: `Screener(filters=, signal=, order=, table=)` → `.data` (list of
``{"Ticker", "Market Cap"}`` dicts, Market Cap as a ``"<millions>M"``
string for `main.parse_number`) and `._total_rows`; zero rows raise
`NoResults`. A page that answers 200 but carries no rows block raises
`FinvizLayoutError` instead of reading as "no results", so the next
redesign is loud. The redesign also silently ignores a filter code it does
not know (`ta_perf_52w150o` came back as the whole market), so every code
is checked against the filter definitions the page ships and an unknown
one raises `FinvizFilterError` — no data beats the wrong data. The 1000-row page cap is unchanged (pages past r=1000
403 without Elite): paging stops there and `run_screener` fetches the tail
in descending order, as before.
"""
from __future__ import annotations

import json
import logging
import re

import requests
from finviz.config import USER_AGENT
from finviz.helper_functions.error_handling import NoResults

logger = logging.getLogger(__name__)

SCREENER_URL = "https://finviz.com/screener/stocks"
PAGE_CAP = 1000
_TIMEOUT = (10, 30)
_SSR_JSON_RE = re.compile(
    r'<script type="application/json"[^>]*>(.*?)</script>', re.DOTALL
)


# `/screener/stocks` is stocks-only, so the old industry pseudo-filter is
# implied (the page drops it from its echoed URL).
_IMPLIED_FILTERS = frozenset({"ind_stocksonly"})


class FinvizLayoutError(RuntimeError):
    """The screener page answered but no rows block could be found in it."""


class FinvizFilterError(ValueError):
    """Filter codes the screener does not define — it would ignore them."""

    def __init__(self, codes: list[str]):
        self.codes = codes
        super().__init__(
            f"finviz ignores unknown filter code(s) {', '.join(codes)} — "
            "pick a value the screener offers"
        )


def _ssr_blobs(html: str):
    """Every parseable `<script type="application/json">` blob of a page."""
    for m in _SSR_JSON_RE.finditer(html):
        try:
            yield json.loads(m.group(1))
        except ValueError:
            continue


def _find_rows_block(node):
    """Depth-first search for the ``{"rows": [...], "totalCount": N}`` dict."""
    if isinstance(node, dict):
        if isinstance(node.get("rows"), list) and "totalCount" in node:
            return node
        children = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return None
    for child in children:
        found = _find_rows_block(child)
        if found is not None:
            return found
    return None


def parse_screener_page(html: str) -> tuple[list[dict], int]:
    """(rows, totalCount) from one screener page; raises FinvizLayoutError."""
    for blob in _ssr_blobs(html):
        block = _find_rows_block(blob)
        if block is None:
            continue
        rows = []
        for r in block["rows"]:
            text = r.get("text") or {}
            numbers = r.get("numbers") or {}
            ticker = text.get("ticker")
            if not ticker:
                continue
            row = {"Ticker": ticker}
            cap = numbers.get("marketCap")
            if cap is not None:
                row["Market Cap"] = f"{cap}M"
            rows.append(row)
        return rows, int(block["totalCount"])
    raise FinvizLayoutError(
        "finviz screener page has no rows block — page layout changed?"
    )


def _filter_definitions(node, out: dict[str, set[str]]) -> dict[str, set[str]]:
    """``{key: {option values}}`` of every ``{"key", "options"}`` dict."""
    if isinstance(node, dict):
        if isinstance(node.get("key"), str) and isinstance(node.get("options"), list):
            out.setdefault(node["key"], set()).update(
                o["value"] for o in node["options"]
                if isinstance(o, dict) and o.get("value")
            )
        children = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return out
    for child in children:
        _filter_definitions(child, out)
    return out


def unknown_filters(html: str, filters: list[str]) -> list[str]:
    """Codes in `filters` that are not ``<key>_<option value>`` of any filter
    the page defines. Empty when the page carries no definitions at all —
    the rows check owns layout changes."""
    defs: dict[str, set[str]] = {}
    for blob in _ssr_blobs(html):
        _filter_definitions(blob, defs)
    if not defs:
        return []
    unknown = []
    for code in filters:
        if code in _IMPLIED_FILTERS:
            continue
        if not any(
            code.startswith(key + "_") and code[len(key) + 1:] in values
            for key, values in defs.items()
        ):
            unknown.append(code)
    return unknown


def _fetch_page(params: dict) -> requests.Response:
    response = requests.get(
        SCREENER_URL,
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=_TIMEOUT,
    )
    if response.text == "Too many requests.":
        raise Exception("Too many requests.")
    return response


class Screener:
    """Fetch every page (up to the 1000-row cap) of one Finviz screen."""

    def __init__(
        self,
        filters: list[str] | None = None,
        signal: str | None = None,
        order: str | None = None,
        table: str | None = None,  # noqa: ARG002 — overview rows carry Market Cap
    ):
        params: dict = {"v": "111"}
        if filters:
            params["f"] = ",".join(filters)
        if signal:
            params["s"] = signal
        if order:
            params["o"] = order
        self.data: list[dict] = []
        self._total_rows = 0

        seen: set[str] = set()
        row = 1
        while True:
            response = _fetch_page({**params, "r": row} if row > 1 else params)
            if response.status_code == 403 and row > PAGE_CAP:
                break  # non-Elite page cap; run_screener fetches the tail
            response.raise_for_status()
            rows, total = parse_screener_page(response.text)
            if row == 1:
                unknown = unknown_filters(response.text, filters or [])
                if unknown:
                    raise FinvizFilterError(unknown)
                self._total_rows = total
            if not rows:
                break
            for r in rows:
                if r["Ticker"] not in seen:
                    seen.add(r["Ticker"])
                    self.data.append(r)
            row += len(rows)
            if row > self._total_rows:
                break

        if self._total_rows == 0:
            raise NoResults(params.get("f", ""))
