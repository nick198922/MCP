"""Forecast journal of the assistant: daily risk-neutral range forecasts for BTC and ETH, scored at delivery.

Docs: docs/ideas/IDEA-20261018-assistant.md ("Журнал прогнозів"). The collector job `forecast_log` (off by default) writes
the day's forecasts once a day; the tool here settles the matured ones and reports Brier, calibration and the weekly
table, and the same scores for the RV30 model (a physical, history-only baseline) with a paired comparison. Local
bookkeeping only (table `forecast_log`), nothing is traded or sent anywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.forecast_log import KINDS, RV_DAYS, as_model, brier, build_forecasts, compare, outcome, pick_expiry, score, weekly
from ..analytics.options import build_chain, build_smile, group_by_expiry
from ..analytics.radar import trailing_rv
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY
from .journal import _store_guard

ASSETS = ("BTC", "ETH")
RV_VENUE = "binance"  # spot daily closes of the RV30 model, as the radar's RV30
FORECAST_WARNING = (
    "RISK-NEUTRAL probabilities from the Deribit smile (what options price), NOT real-world odds and not advice: the "
    "volatility risk premium alone should make the bands contain the outcome more often than priced, and the log is "
    "kept to measure that gap (a hypothesis under test, no edge shown). Forecasts issued on consecutive days for one "
    "expiry share the outcome, so they are correlated: read `n_expiries`, not `n`, as the sample size. The climatology "
    "reference is in-sample (it knows the base rate in hindsight). Few settled expiries: the scores are descriptive. "
    "The RV30 model is a naive physical baseline (zero-drift lognormal at the trailing 30-day realized vol, no skew or "
    "events), not the assistant's judgment. Compare the two models only through `comparison` (paired on the same rows); "
    "`scores_rv30` covers only rows that have the RV30 model, so it is not on the same sample as `scores`. The comparison's "
    "unit is the expiry date (BTC and ETH of one date move together): it is descriptive until `enough` (30 dates)."
)


def _iso(ts: int | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(int(ts), UTC).isoformat(timespec="seconds")


def _rounded(x: Any, nd: int = 4) -> Any:
    if isinstance(x, float):
        return round(x, nd)
    if isinstance(x, dict):
        return {k: _rounded(v, nd) for k, v in x.items()}
    if isinstance(x, list):
        return [_rounded(v, nd) for v in x]
    return x


def _view(x: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": x["id"],
        "day": x["day"],
        "issued": _iso(x["issued_ts"]),
        "asset": x["asset"],
        "kind": x["kind"],
        "expiry": _iso(x["expiry_ts"]),
        "spot": round(float(x["spot"]), 2),
        "forward": round(float(x["forward"]), 2),
        "atm_iv": round(float(x["atm_iv"]), 4),
        "lo": None if x["lo"] is None else round(float(x["lo"]), 2),
        "hi": None if x["hi"] is None else round(float(x["hi"]), 2),
        "prob": round(float(x["prob"]), 4),
        "ref_prob": None if x["ref_prob"] is None else round(float(x["ref_prob"]), 4),
        "rv30": None if x.get("rv30") is None else round(float(x["rv30"]), 4),
        "prob_rv": None if x.get("prob_rv") is None else round(float(x["prob_rv"]), 4),
        "covered": x["covered"],
        "status": x["status"],
    }
    if x["status"] == "settled":
        out.update(delivery_price=x["delivery_price"], outcome=x["outcome"], brier=round(float(x["brier"]), 4))
        out["brier_rv"] = None if x.get("brier_rv") is None else round(float(x["brier_rv"]), 4)
    return out


async def forecast_rows(rt: Runtime, b: Builder, asset: str) -> list[dict[str, Any]] | None:
    """Today's forecast rows for ``asset`` from the live Deribit chain; None on failure (recorded in ``b.missing``)."""
    book = await b.guard(f"option_book:{asset}", "deribit", rt.deribit.book_summary(asset, "option"))
    if book is None:
        return None
    now = rt.now()
    by = group_by_expiry(x for x in build_chain(book, now) if x.instrument.startswith(f"{asset}-"))
    exp = pick_expiry(by, now)
    if exp is None:
        b.miss(f"expiry:{asset}", "tradedesk", "data_unavailable", "no listed expiry within 4..11 days")
        return None
    rows = by[exp]
    smile = build_smile(rows)
    spot = next((x.index for x in rows if x.index), None)
    rv = await _rv30(rt, b, asset) if smile is not None and spot else None
    out = build_forecasts(smile, asset=asset, spot=float(spot), expiry=exp, now=now, rv=rv) if smile is not None and spot else []
    if not out:
        b.miss(f"smile:{asset}", "tradedesk", "data_unavailable", f"no usable smile or index on {exp.date().isoformat()}")
        return None
    return out


async def _rv30(rt: Runtime, b: Builder, asset: str) -> float | None:
    """Trailing RV30 of ``asset`` from Binance completed daily closes (today's partial bar is dropped: weighted as a full
    day it would shrink the vol); None (with a warning, not a `missing` entry) when it fails.

    The risk-neutral forecast does not depend on it, so a closes failure must not make the collector retry the day."""
    rb = Builder()
    bars = await rb.guard(f"rv30:{asset}", f"ccxt:{RV_VENUE}", rt.exchanges.ohlcv(RV_VENUE, f"{asset}/USDT", "1d", RV_DAYS + 3))
    today_ms = int(rt.now().timestamp()) // 86400 * 86_400_000
    closes = []
    for x in bars or []:
        try:
            if int(x[0]) < today_ms:
                closes.append((int(x[0]), float(x[4])))
        except (TypeError, ValueError, IndexError):
            continue
    rv, why = (
        trailing_rv(closes, rt.now(), RV_DAYS)
        if closes
        else (None, "; ".join(f"{m.reason}: {m.detail}" for m in rb.missing) or "no closes")
    )
    if rv is None:
        b.warn(f"rv30:{asset}: the RV30 model is left empty for today ({why})")
    return rv


async def record_today(rt: Runtime, b: Builder, assets: tuple[str, ...] = ASSETS) -> int:
    """Write today's (UTC) forecasts of every asset not yet recorded today; returns the rows written. Idempotent."""
    assert rt.store is not None
    day = rt.now().astimezone(UTC).date().isoformat()
    n = 0
    for asset in assets:
        if await rt.store.forecast_has_day(day, asset):
            continue
        rows = await forecast_rows(rt, b, asset)
        if rows:
            n += await rt.store.forecast_insert_many(rows)
    return n


async def settle_due(rt: Runtime, b: Builder) -> int:
    """Settle every open forecast whose expiry has passed at the Deribit delivery price; returns the rows settled.

    A delivery price not yet published (or a feed failure) leaves the row open and adds a `missing` entry."""
    assert rt.store is not None
    now_ts = int(rt.now().timestamp())
    due = [x for x in await rt.store.forecast_list(status="open", limit=100_000) if x["expiry_ts"] <= now_ts]
    n = 0
    for asset in sorted({x["asset"] for x in due}):
        got = await b.guard(f"delivery_prices:{asset}", "deribit", rt.deribit.delivery_prices(f"{asset.lower()}_usd", count=1000))
        if got is None:
            continue
        prices: dict[str, float] = {}
        feed = got.get("data") if isinstance(got, dict) else None
        for d in feed or []:
            try:
                if d.get("delivery_price"):
                    prices[str(d.get("date"))] = float(d["delivery_price"])
            except (TypeError, ValueError, AttributeError):
                b.miss(f"delivery_prices:{asset}", "deribit", "parse_error", f"bad delivery row {str(d)[:80]}")
        missing_days: set[str] = set()
        for x in (x for x in due if x["asset"] == asset):
            day = datetime.fromtimestamp(x["expiry_ts"], UTC).date().isoformat()
            dp = prices.get(day)
            if dp is None:
                missing_days.add(day)
                continue
            o = outcome(x["lo"], x["hi"], dp)
            brv = None if x.get("prob_rv") is None else brier(x["prob_rv"], o)
            n += await rt.store.forecast_settle(
                x["id"], settled_ts=x["expiry_ts"], delivery_price=dp, outcome=o, brier=brier(x["prob"], o), brier_rv=brv
            )
        for day in sorted(missing_days):
            b.miss(f"forecast_log:{asset}:{day}", "deribit", "data_unavailable", f"no {asset} delivery price for {day} published yet")
    return n


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,  # settling writes to the LOCAL store only — no orders, no external effects
        description=(
            "Forecast journal of the assistant: daily RISK-NEUTRAL range forecasts for BTC and ETH and how well they scored. "
            "Each day (collector job `forecast_log`, off by default: TRADEDESK_PAPER_FORECAST_LOG_ENABLED=1) the Deribit smile of "
            "the listed expiry closest to 7 days (4..11) gives the probability of four events on that expiry's delivery price "
            "S_T (USD): `above_spot` S_T > index at issue; `in_1sigma` inside F*exp(-+ATM IV*sqrt(T)) (T = days/365); "
            "`in_pct_5` / `in_pct_10` within +-5 % / +-10 % of the index. This tool first settles every open forecast whose "
            "08:00 UTC expiry has passed at the Deribit delivery price (outcome 1/0, Brier (p - o)^2), then returns: `latest` "
            "(the newest day's forecasts per asset: bounds lo/hi in USD, prob decimal), `scores` (all and per kind: n, "
            "n_expiries, mean prob, base rate, Brier, Brier of the in-sample climatology and of the fixed reference 0.5 / "
            "0.6827 where it exists, skill = 1 - BS/BS_ref, calibration in 10 probability bins with forecast mean vs observed "
            "frequency, binned reliability / resolution / uncertainty) and `weekly` (by ISO week of the expiry, UTC). "
            "Beside it, the RV30 model, a naive PHYSICAL baseline: ln(S_T/S0) ~ N(0, RV30^2 T), RV30 = trailing 30-day "
            "zero-mean close-to-close realized vol of Binance spot (decimal, annualised x365; `above_spot` is always 0.5): "
            "`scores_rv30` (the same scores on the rows that have it) and `comparison` (all and per kind, paired on the same "
            "rows: Brier of each, diff = BS risk-neutral - BS RV30, negative = options better, clustered by expiry date "
            "(`n_dates`) with se and a Student-t ci95, `enough` once 30 dates are settled). RV30 uses completed daily bars only. "
            "Probabilities are what options price, not real-world odds (see `warnings`); forecasts on consecutive days share "
            "an outcome, so `n_expiries` is the sample size. A delivery price not yet published leaves the row open (`missing`)."
        ),
    )
    async def forecast_log_status(
        asset: Annotated[Literal["BTC", "ETH"] | None, Field(description="Only this asset; default both.")] = None,
        weeks: Annotated[int, Field(ge=1, le=104, description="How many most recent weeks to list in `weekly` (default 12).")] = 12,
        include_settled: Annotated[bool, Field(description="Also list settled forecasts (newest 200).")] = False,
    ) -> dict[str, Any]:
        b = Builder()
        b.warn(FORECAST_WARNING)
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        settled_now = await settle_due(rt, b)
        rows = await rt.store.forecast_list(asset=asset, limit=100_000)
        settled = [x for x in rows if x["status"] == "settled"]
        rv = as_model(settled, "rv30")
        latest: dict[str, list[dict[str, Any]]] = {}
        order = {k: i for i, k in enumerate(KINDS)}
        for a in sorted({x["asset"] for x in rows}):
            mine = [x for x in rows if x["asset"] == a]
            day = max(x["day"] for x in mine)
            latest[a] = [_view(x) for x in sorted((x for x in mine if x["day"] == day), key=lambda x: order.get(x["kind"], 99))]
        if not rows:
            b.warn("the forecast log is empty: enable the collector job `forecast_log` (TRADEDESK_PAPER_FORECAST_LOG_ENABLED=1)")
        data: dict[str, Any] = {
            "label": "risk_neutral",
            "settled_this_call": settled_now,
            "n_open": sum(1 for x in rows if x["status"] == "open"),
            "n_settled": len(settled),
            "latest": latest,
            "scores": _rounded({"all": score(settled), "by_kind": {k: score([x for x in settled if x["kind"] == k]) for k in KINDS}}),
            "scores_rv30": _rounded({"all": score(rv), "by_kind": {k: score([x for x in rv if x["kind"] == k]) for k in KINDS}}),
            "comparison": _rounded(
                {"all": compare(settled), "by_kind": {k: compare([x for x in settled if x["kind"] == k]) for k in KINDS}}, 5
            ),
            "weekly": _rounded(weekly(settled)[-weeks:]),
        }
        if include_settled:
            data["settled"] = [_view(x) for x in settled[:200]]
        return b.build(data)
