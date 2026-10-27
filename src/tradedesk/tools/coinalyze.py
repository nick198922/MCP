from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.coinalyze_series import FUNDING_PCT_TO_DECIMAL, long_short_series, ohlc_series, sum_histories
from ..analytics.liquidations_agg import EXCHANGE_CODES, aggregated_symbol, api_interval, exchange_symbol, parse_exchanges, summarise
from ..core.envelope import Builder, Fetched
from ..runtime import Runtime
from ._util import READ_ONLY

HOURLY_CAP_DAYS = 30
BINANCE_ONLY = (
    "Binance only (suffix '.A' = Binance in Coinalyze; verified against Binance OI/funding/long-short, not an "
    "all-exchange sum); the current (last) interval is still open."
)
ALLOWED_EXCHANGES = "allowed: 'binance' (code '.A') and 'bybit' (code '.6', USDT-margined only)"
USD_NOTE = (
    "USD values are Coinalyze's own conversion: coins x the close of the interval (checked on daily liquidations "
    "2026-10-25: coins x the day's Binance USD-M close), not each value at its own price, so on crash days the USD "
    "figures are shifted against the coin counts. Exact USD liquidation sums: liquidations_summary (our own live "
    "stream)."
)

Interval = Annotated[Literal["daily", "hourly"], Field(description="Bucket size (UTC-aligned).")]
Days = Annotated[int, Field(ge=1, le=365, description="Look-back window in days (hourly is capped at 30 days).")]
Margin = Annotated[Literal["usdt", "usd"], Field(description="'usdt' = USDT-margined perps (default), 'usd' = coin-margined perps.")]
Symbol = Annotated[str, Field(description="Base asset ticker, e.g. 'BTC', 'ETH', 'SOL'.")]
Exchanges = Annotated[
    list[str] | None,
    Field(
        description=(
            "Exchanges to sum, by name or Coinalyze code: 'binance' / '.A' (default when omitted) and 'bybit' / '.6' "
            "(USDT-margined only). Any other value is rejected (no request is made)."
        )
    ),
]


def _exchange_symbols(b: Builder, field: str, symbol: str, margin: str, exchanges: Sequence[str] | None) -> list[tuple[str, str]] | None:
    """[(exchange, coinalyze symbol)] for the validated whitelist, or None after recording `missing`."""
    names, bad = parse_exchanges(exchanges)
    if bad or not names:
        head = f"unknown exchange(s) {bad[:3]}" if bad else "no exchange given"
        b.miss("exchanges", "coinalyze", "invalid_argument", f"{head}; {ALLOWED_EXCHANGES}")
        return None
    pairs: list[tuple[str, str]] = []
    for ex in names:
        sym = exchange_symbol(symbol, margin, ex)
        if sym is None:
            b.miss(f"{field}.{ex}", "coinalyze", "invalid_argument", f"no verified Coinalyze symbol on {ex} for margin='{margin}'")
            continue
        pairs.append((ex, sym))
    return pairs or None


async def _summed(
    b: Builder,
    field: str,
    label: str,
    pairs: list[tuple[str, str]],
    fetch: Callable[[str], Awaitable[Fetched]],
    parse: Callable[[list[Any]], dict[str, Any] | None],
    keys: tuple[str, ...],
) -> dict[str, Any] | None:
    """Fetch every (exchange, symbol), parse each, sum across the exchanges that returned data.

    A failed or empty exchange goes to `missing` and the rest is summed; None (after `missing`) if nothing is left.
    Adds `symbol` (comma-joined symbols used) and `exchanges` (one entry per exchange used) to the data.
    """
    raws = await asyncio.gather(*(b.guard(f"{field}.{ex}", "coinalyze", fetch(sym)) for ex, sym in pairs))
    used: list[tuple[str, str, list[Any], dict[str, Any]]] = []
    for (ex, sym), rows in zip(pairs, raws, strict=True):
        if rows is None:
            continue
        one = parse(rows)
        if one is None:
            b.miss(f"{field}.{ex}", "coinalyze", "data_unavailable", f"no {label} history for {sym} in the range")
            continue
        used.append((ex, sym, rows, one))
    if not used:
        return None
    names = [u[0] for u in used]
    if len(used) == 1:
        data = used[0][3]
    else:
        merged, dropped = sum_histories([u[2] for u in used], keys)
        data = parse(merged)
        if data is None:
            b.miss(field, "coinalyze", "data_unavailable", f"no {label} interval common to {', '.join(names)}")
            return None
        if dropped:
            b.warn(f"{dropped} interval(s) left out of the sum: not present on every selected exchange.")
    lost = [ex for ex, _ in pairs if ex not in names]
    if lost:
        b.warn(f"partial sum: {', '.join(lost)} returned no data (see missing); figures cover {', '.join(names)} only.")
    if names == ["binance"]:
        b.warn(BINANCE_ONLY)
    else:
        b.warn(
            f"Sum of {', '.join(names)} only, not a market-wide total (other venues are not included); the current "
            "(last) interval is still open."
        )
    data["symbol"] = ",".join(u[1] for u in used)
    data["exchanges"] = [
        {"exchange": ex, "code": EXCHANGE_CODES[ex], "symbol": sym, "intervals": len(one["rows"])} for ex, sym, _, one in used
    ]
    return data


def _window(b: Builder, rt: Runtime, symbol: str, margin: str, interval: str, days: int) -> tuple[str, int, int, int] | None:
    """(coinalyze symbol, span_days, aligned start, end) or None after recording `missing` for a bad symbol."""
    sym = aggregated_symbol(symbol, margin)
    if sym is None:
        b.miss("symbol", "coinalyze", "invalid_argument", "symbol must be a plain ticker like 'BTC'")
        return None
    span_days = HOURLY_CAP_DAYS if interval == "hourly" and days > HOURLY_CAP_DAYS else days
    if span_days != days:
        b.warn(f"hourly history is capped at {HOURLY_CAP_DAYS} days; days was reduced to {HOURLY_CAP_DAYS}.")
    step = 3600 if interval == "hourly" else 86_400
    end = int(rt.now().timestamp())
    start = end - span_days * 86_400
    return sym, span_days, start // step * step, end


def _gap_warning(b: Builder, gaps: int) -> None:
    if gaps:
        b.warn(f"{gaps} gap(s) in the series (missing intervals); stats cover only returned rows.")


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Historical long/short liquidations of a perpetual from Coinalyze (needs the COINALYZE_API_KEY setting), "
            "on Binance by default or summed over the selected exchanges: exchanges = 'binance' (Coinalyze code "
            "'.A') and/or 'bybit' ('.6', USDT-margined only); no other codes are accepted. Each code is one "
            "exchange, not an all-exchanges aggregate; the response lists the exchanges used, and an exchange that "
            "fails or has no data goes to `missing` while the rest is summed (only intervals present on every used "
            "exchange are summed). Per interval: long (forced sells of longs), short (forced buys of shorts), "
            "total, net = long - short, long_share in [0, 1]; plus totals and the peak interval. Units: USD by "
            "default, or base-asset coins with in_usd=false. USD in Coinalyze = coins x the close of the day "
            "(interval), not each liquidation at its own price, so on crash days the USD sums are shifted (long "
            "liquidations fill above the close, so they are understated); exact USD sums come from "
            "liquidations_summary (our own live-collected stream). Intervals are UTC-aligned daily or hourly. "
            "Coinalyze keeps hourly data only for a limited recent window (older ranges come back empty). "
            "Liquidation counts are exchange-reported and understate forced closes on venues that publish only the "
            "largest order per second. Do not read the totals as market-wide liquidations."
        ),
    )
    async def liquidations_history(
        symbol: Symbol,
        interval: Interval = "daily",
        days: Days = 30,
        margin: Margin = "usdt",
        in_usd: Annotated[bool, Field(description="Convert quantities to USD (true) or return base-asset units (false).")] = True,
        exchanges: Exchanges = None,
    ) -> dict[str, Any]:
        b = Builder()
        w = _window(b, rt, symbol, margin, interval, days)
        if w is None:
            return b.build(None, ok=False)
        _, span_days, start, end = w
        pairs = _exchange_symbols(b, "liquidations", symbol, margin, exchanges)
        if pairs is None:
            return b.build(None, ok=False)
        api_iv = api_interval(interval)
        data = await _summed(
            b,
            "liquidations",
            "liquidation",
            pairs,
            lambda sym: rt.coinalyze.liquidation_history(sym, api_iv, start, end, convert_to_usd=in_usd),
            lambda rows: summarise(rows, interval),
            ("l", "s"),
        )
        if data is None:
            return b.build(None, ok=False)
        data.update({"interval": interval, "unit": "USD" if in_usd else symbol.upper(), "days": span_days})
        if data["totals"]["gaps"]:
            b.warn(f"{data['totals']['gaps']} gap(s) in the series (missing intervals); totals cover only returned rows.")
        if in_usd:
            b.warn(USD_NOTE + " Long liquidations of a crash day fill above its close, so their USD is understated.")
        b.warn("Not a trading signal by itself: no backtest of liquidation spikes is attached to this tool.")
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Open-interest history (OHLC per interval) of a perpetual from Coinalyze (needs the COINALYZE_API_KEY "
            "setting), on Binance by default or summed over the selected exchanges: exchanges = 'binance' (Coinalyze "
            "code '.A') and/or 'bybit' ('.6', USDT-margined only); no other codes are accepted. Each code is one "
            "exchange, not an all-exchanges aggregate; the response lists the exchanges used, and an exchange that "
            "fails or has no data goes to `missing` while the rest is summed (only intervals present on every used "
            "exchange are summed; summed high/low are sums of per-exchange extremes, i.e. bounds). Rows: "
            "open/high/low/close OI; stats over the closes: latest, mean, stdev, min, max, z-score of the latest, "
            "change and change_pct (fraction, 0.05 = +5 %) from the first to the last close. Units: USD by default, "
            "or base-asset coins with in_usd=false. USD in Coinalyze = coins x the close of the interval (its own "
            "conversion, checked on daily liquidations), so on crash days USD values are shifted against the coin "
            "counts; exact USD liquidation sums come from liquidations_summary. Timestamps are interval starts "
            "(Binance openInterestHist stamps the interval end, so its daily series sits one day later). Daily "
            "history goes back years; hourly only for a limited recent window. For live OI per venue use "
            "perps_open_interest, for a short multi-venue history perps_oi_history. Descriptive data, not a signal."
        ),
    )
    async def coinalyze_oi_history(
        symbol: Symbol,
        interval: Interval = "daily",
        days: Days = 30,
        margin: Margin = "usdt",
        in_usd: Annotated[bool, Field(description="OI in USD (true) or in base-asset coins (false).")] = True,
        exchanges: Exchanges = None,
    ) -> dict[str, Any]:
        b = Builder()
        w = _window(b, rt, symbol, margin, interval, days)
        if w is None:
            return b.build(None, ok=False)
        _, span_days, start, end = w
        pairs = _exchange_symbols(b, "open_interest", symbol, margin, exchanges)
        if pairs is None:
            return b.build(None, ok=False)
        api_iv = api_interval(interval)
        data = await _summed(
            b,
            "open_interest",
            "open-interest",
            pairs,
            lambda sym: rt.coinalyze.open_interest_history(sym, api_iv, start, end, convert_to_usd=in_usd),
            lambda rows: ohlc_series(rows, interval),
            ("o", "h", "l", "c"),
        )
        if data is None:
            return b.build(None, ok=False)
        data.update({"interval": interval, "unit": "USD" if in_usd else symbol.upper(), "days": span_days})
        _gap_warning(b, data["stats"]["gaps"])
        if margin == "usd" and not in_usd:
            b.warn("coin-margined OI without USD conversion: the native unit (coins or contracts) is not verified.")
        if in_usd:
            b.warn(USD_NOTE)
        b.warn("Timestamps are interval starts.")
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Funding-rate history (OHLC per interval) of a perpetual on **Binance only** from Coinalyze (symbol suffix "
            "'.A' is the Binance exchange code, not an all-exchanges aggregate; needs the COINALYZE_API_KEY setting). "
            "Units: decimal fractions per funding interval (0.0001 = 0.01 % per settlement, usually 8 h on Binance, "
            "some symbols 4 h or 1 h); NOT annualised. Coinalyze sends percent; we divide by 100. For daily bars use "
            "close (= last settlement of the UTC day) and high (= largest settlement), both checked against Binance "
            "payments 2026-10-18; open and low may carry the previous day's last rate, so they are not this day's "
            "settlements. Stats over the closes: latest, mean, stdev, min, max, z-score of the latest, change. The "
            "mean of daily closes is neither the average rate of the day nor the sum of its payments: do not compute "
            "carry from it. Hourly bars are unverified (in hours without a settlement Coinalyze may repeat the last "
            "rate, send a predicted one or skip the bar). Daily history goes back years; hourly only for a limited "
            "recent window. For multi-venue funding with carry and annualisation use perps_funding_history / "
            "perps_funding_compare. Descriptive data, not a signal."
        ),
    )
    async def coinalyze_funding_history(
        symbol: Symbol,
        interval: Interval = "daily",
        days: Days = 30,
        margin: Margin = "usdt",
    ) -> dict[str, Any]:
        b = Builder()
        w = _window(b, rt, symbol, margin, interval, days)
        if w is None:
            return b.build(None, ok=False)
        sym, span_days, start, end = w
        rows = await b.guard("funding", "coinalyze", rt.coinalyze.funding_rate_history(sym, api_interval(interval), start, end))
        if rows is None:
            return b.build(None, ok=False)
        data = ohlc_series(rows, interval, scale=FUNDING_PCT_TO_DECIMAL, with_pct_change=False)
        if data is None:
            b.miss("funding", "coinalyze", "data_unavailable", f"no funding history for {sym} in the range")
            return b.build(None, ok=False)
        data.update({"symbol": sym, "interval": interval, "unit": "decimal per funding interval", "days": span_days})
        _gap_warning(b, data["stats"]["gaps"])
        b.warn(BINANCE_ONLY + " Rates are per funding interval, not annualised.")
        b.warn(
            "Use close (last settlement) and high (largest settlement); open/low may carry the previous bar's last "
            "rate. The mean of closes is not the average rate or the sum of payments: not a carry basis."
        )
        if interval == "hourly":
            b.warn(
                "hourly bar semantics are not verified: in hours without a settlement the value may be the last "
                "settled rate forward-filled, a predicted rate, or the bar may be missing (affects gaps, mean, z-score)."
            )
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Long/short account-ratio history of a perpetual on **Binance only** from Coinalyze (symbol suffix '.A' "
            "is the Binance exchange code, not an all-exchanges aggregate; needs the COINALYZE_API_KEY setting). "
            "Rows: ratio = long accounts / short accounts, long_share and short_share as fractions in [0, 1] "
            "(Coinalyze sends percent; we divide by 100). Counts accounts, not notional: a crowd of small longs can "
            "face a few large shorts. Matches Binance globalLongShortAccountRatio (checked 2026-10-18), including "
            "its timestamps one to one: a row at t is most likely Binance's snapshot taken at t (known at t, not at "
            "the end of the bar; inferred, not independently verified), unlike the OI/funding OHLC rows whose t is "
            "the interval start; mind lookahead in backtests. Stats: ratio "
            "latest, mean, stdev, min, max, z-score of the latest; latest and mean long_share. Daily history goes "
            "back years; hourly only for a limited recent window. For Binance + Bybit side by side use "
            "perps_long_short_ratio. Descriptive data, not a signal."
        ),
    )
    async def coinalyze_long_short_history(
        symbol: Symbol,
        interval: Interval = "daily",
        days: Days = 30,
        margin: Margin = "usdt",
    ) -> dict[str, Any]:
        b = Builder()
        w = _window(b, rt, symbol, margin, interval, days)
        if w is None:
            return b.build(None, ok=False)
        sym, span_days, start, end = w
        rows = await b.guard(
            "long_short_ratio", "coinalyze", rt.coinalyze.long_short_ratio_history(sym, api_interval(interval), start, end)
        )
        if rows is None:
            return b.build(None, ok=False)
        data = long_short_series(rows, interval)
        if data is None:
            b.miss("long_short_ratio", "coinalyze", "data_unavailable", f"no long/short history for {sym} in the range")
            return b.build(None, ok=False)
        data.update({"symbol": sym, "interval": interval, "unit": "ratio; shares as fractions", "days": span_days})
        _gap_warning(b, data["stats"]["gaps"])
        b.warn(BINANCE_ONLY + " Accounts, not notional.")
        return b.build(data)
