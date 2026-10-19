"""Paper journal of the assistant's option recommendations (local bookkeeping only, no orders).

Each recommendation is booked as a paper trade at the Deribit marks +- 1 vol pt, sized from the owner's deposit and
risk percentage, and settled at the expiry delivery price. Rows live in their own table (`rec_paper`) and are tagged
'hypothesis': the method was tested on 2026-10-18 and showed no edge (docs/verification/2026-10-18-assistant-recommendations.md).
They are NOT mirrored into the owner's `journal`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.instruments import expiry_token
from ..analytics.option_replay import build_legs, strike_roles
from ..analytics.options import OptionRow, black76_greeks, build_chain, group_by_expiry
from ..analytics.rec_paper import open_paper, settle_paper, summarize
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, find_expiry, r
from .journal import _store_guard

PAPER_WARNING = (
    "HYPOTHESIS, paper trade in the local store only: no order is sent. The recommendation method was tested on 2026-10-18 "
    "(docs/verification/2026-10-18-assistant-recommendations.md): trades it picked ahead of time averaged -0.59 % of spot per trade "
    "[-2.43; +1.51], n = 27, so no positive expectancy is shown; an edge of about +0.2 R (the study's R: P&L / premium for a debit, "
    "P&L / |5 % quantile| for a credit) is not excluded; `r_study` of rows with source 'method' is what tests it, `pnl_pct_spot` "
    "is the main metric and `r_worst_case` is a different R. Entry is the Deribit mark +- 1 vol point priced with Black-76 (r = 0) with modelled Deribit fees, not a real fill. "
    "Rows are kept apart from the real trades in `journal`. Risk-neutral marks, not forecasts."
)
Structure = Literal[
    "long_straddle",
    "short_straddle",
    "long_strangle25",
    "short_strangle25",
    "iron_condor",
    "long_strangle10",
    "reverse_iron_fly",
    "call_debit_spread",
    "put_debit_spread",
]
DEFAULT_LOT = {"BTC": 0.1, "ETH": 1.0}  # Deribit minimum option sizes
TARGET_DTE, DTE_BAND = 40.0, (28.0, 56.0)


def _iso(ts: int | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def surface_of(rows: list[OptionRow]) -> dict[str, Any]:
    """One expiry in the shape `option_replay.strike_roles` reads: strikes with mark iv and put delta (OI > 0 only)."""
    strikes: dict[float, dict[str, float]] = {}
    for x in rows:
        if x.iv is None or x.open_interest <= 0:
            continue
        s = strikes.setdefault(x.strike, {"mark_iv": x.iv})
        if not x.is_call and x.delta is not None:
            s["put_delta"] = x.delta
    for k, s in strikes.items():
        if "put_delta" not in s:
            s["put_delta"] = black76_greeks(rows[0].forward, k, rows[0].T, s["mark_iv"], False)["delta"]
    return {"F": rows[0].forward, "strikes": strikes}


def _view(t: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": t["id"],
        "label": t["label"],
        "status": t["status"],
        "structure": t["structure"],
        "currency": t["currency"],
        "opened": _iso(t["opened_ts"]),
        "expiry": _iso(t["expiry_ts"]),
        "spot": t["spot"],
        "forward": r(t["forward"], 2),
        "legs": [
            {"strike": k, "type": "call" if c else "put", "qty": q, "exec_iv": r(iv, 4), "entry_price_usd": r(p, 2)}
            for k, c, q, iv, p in t["legs"]
        ],
        "premium_usd_per_btc": r(t["premium_per_btc"], 2),
        "entry_fees_usd_per_btc": r(t["entry_fees_per_btc"], 2),
        "source": t["source"],
        "regime": t["regime"],
        "trigger": t.get("trigger"),
        "method_mean_pct": r(t["method_mean_pct"], 5),
        "method_q05_pct": r(t["method_q05_pct"], 5),
        "max_loss_usd_per_btc": r(t["max_loss_per_btc"], 2),
        "risk_basis": t["risk_basis"],
        "size_btc": t["size_btc"],
        "deposit_usd": t["deposit_usd"],
        "risk_pct": t["risk_pct"],
        "risk_usd": r(t["risk_usd"], 2),
        "within_risk": t["within_risk"],
        "note": t["note"],
    }
    if t["status"] == "settled":
        out.update(
            settled=_iso(t["settled_ts"]),
            delivery_price=t["delivery_price"],
            pnl_usd=r(t["pnl_usd"], 2),
            pnl_pct_spot=r(t["pnl_pct_spot"], 5),
            r_worst_case=r(t["r_worst_case"], 3),
            r_study=r(t["r_study"], 3),
        )
    return out


async def prepare_chain(
    rt: Runtime,
    b: Builder,
    currency: str,
    expiry: str | None,
    *,
    target: float = TARGET_DTE,
    band: tuple[float, float] = DTE_BAND,
) -> dict[str, Any] | None:
    """Live Deribit chain -> the expiry to price (given, else closest to ``target`` days within ``band``) with its surface and roles.

    Shared by the tool and the collector job so both book through the same code. Failures go into ``b.missing``.
    """
    book = await b.guard("option_book", "deribit", rt.deribit.book_summary(currency, "option"))
    if book is None:
        return None
    now = rt.now()
    by = group_by_expiry(build_chain(book, now))
    if expiry:
        exp, err = find_expiry(by, expiry)
        if exp is None:
            b.miss("expiry", "tradedesk", "data_unavailable", err or "")
            return None
    else:
        cands = [e for e in by if band[0] <= (e - now).total_seconds() / 86400 <= band[1]]
        if not cands:
            b.miss("expiry", "tradedesk", "data_unavailable", f"no listed expiry within {band[0]:g}..{band[1]:g} days; pass `expiry`")
            return None
        exp = min(cands, key=lambda e: abs((e - now).total_seconds() / 86400 - target))
    rows = by[exp]
    t_years = rows[0].T
    spot = next((x.index for x in rows if x.index), None)
    if t_years <= 0 or not spot:
        b.miss("expiry", "tradedesk", "data_unavailable", f"{expiry_token(exp)} is expired or has no index price")
        return None
    surf = surface_of(rows)
    return {"now": now, "exp": exp, "t_years": t_years, "spot": float(spot), "surf": surf, "roles": strike_roles(surf)}


async def book_paper(
    rt: Runtime,
    b: Builder,
    ctx: dict[str, Any],
    *,
    structure: str,
    currency: str,
    deposit_usd: float,
    risk_pct: float,
    size_btc: float | None,
    lot: float,
    source: str,
    method_mean_pct: float | None,
    method_q05_pct: float | None,
    regime: str | None,
    note: str | None,
    trigger: str | None = None,
) -> dict[str, Any] | None:
    """Price ``structure`` on the prepared chain and insert the paper trade; returns the view, None on failure (in ``b``)."""
    assert rt.store is not None
    exp, surf, now, t_years, spot = ctx["exp"], ctx["surf"], ctx["now"], ctx["t_years"], ctx["spot"]
    built = build_legs(structure, ctx["roles"], surf["F"], spread_vol=0.01)
    if built is None:
        b.miss("structure", "tradedesk", "data_unavailable", f"{structure}: no suitable strikes with open interest on {expiry_token(exp)}")
        return None
    legs = built[0]
    rec = open_paper(
        legs, forward=surf["F"], spot=spot, t_years=t_years, deposit_usd=deposit_usd, risk_pct=risk_pct, size_btc=size_btc, lot=lot
    )
    if not rec["within_risk"]:
        b.warn(
            f"booked size {rec['size_btc']:g} {currency} risks {rec['risk_usd']:.0f} USD > the {rec['risk_budget_usd']:.0f} USD budget "
            f"({risk_pct:g} % of {deposit_usd:g}); max size under the budget is {rec['max_size_btc']:g}"
        )
    if rec["risk_basis"] != "max_loss":
        b.warn("this structure has unbounded loss: `risk_usd` is a stress (downside to zero or forward +30 %), not a bound")
    dupes = [
        x["id"]
        for x in await rt.store.rec_paper_list(status="open")
        if x["structure"] == structure and x["expiry_ts"] == int(exp.timestamp()) and x["currency"] == currency
    ]
    if dupes:
        b.warn(f"open paper trade(s) {dupes} with the same structure and expiry already exist; recording another")
    stored_legs = [[k, c, q, iv, p] for (k, c, q, iv), p in zip(legs, rec["leg_prices"], strict=True)]
    trade_id = await rt.store.rec_paper_insert(
        opened_ts=int(now.timestamp()),
        currency=currency,
        structure=structure,
        expiry_ts=int(exp.timestamp()),
        spot=spot,
        forward=surf["F"],
        t_years=t_years,
        legs=stored_legs,
        premium_per_btc=rec["premium"],
        entry_fees_per_btc=rec["entry_fees"],
        max_loss_per_btc=rec["max_loss_per_btc"],
        risk_basis=rec["risk_basis"],
        size_btc=rec["size_btc"],
        deposit_usd=deposit_usd,
        risk_pct=risk_pct,
        risk_usd=rec["risk_usd"],
        within_risk=rec["within_risk"],
        note=note,
        source=source,
        method_mean_pct=method_mean_pct,
        method_q05_pct=method_q05_pct,
        regime=regime,
        trigger=trigger,
    )
    row = await rt.store.rec_paper_get(trade_id)
    if row is None:
        b.miss("rec_paper", "tradedesk", "data_unavailable", f"trade {trade_id} was written but could not be read back")
        return None
    return {**_view(row), "max_size_btc": rec["max_size_btc"], "risk_budget_usd": r(rec["risk_budget_usd"], 2)}


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,  # assistant_paper_open writes to the LOCAL store only — no orders, no external effects
        description=(
            "Record a PAPER trade of an option-structure recommendation in the local store (nothing is sent to Deribit, not "
            "mirrored into `journal`). Prices the structure from the live Deribit chain: strikes ATM / 25-delta / 10-delta on "
            "the chosen expiry, bought legs at mark IV + 1 vol point, sold legs at mark IV - 1 vol point, Black-76 on the "
            "forward (r = 0); entry time is the time of this call (the study entered at 09:00 UTC marks). `*_per_btc` fields are per "
            "1 coin of the underlying (BTC or ETH). Sized from `deposit_usd` and `risk_pct` (percent of the deposit risked per trade): the largest "
            "multiple of `lot_btc` whose worst loss fits, unless `size_btc` is given; `within_risk` says whether the booked "
            "size fits; `size_btc` must be a multiple of the lot (default 0.1 BTC, 1 ETH). Worst loss is exact for bounded structures; for net short-call structures (short straddle / strangle) "
            "it is the worst of the downside to zero and a +30 % upside stress, not a bound (`risk_basis`). Worst loss includes entry and delivery fees. Money is USD, IV and fractions are decimals; P&L is linear USD accounting "
            "while Deribit options are inverse (premium and payoff in the coin), so a coin-margined result would differ. "
            "`source` is always 'manual' here: rows tagged 'method' are written only by the collector job `assistant_paper` "
            "and are the only ones that test the method; manual rows are kept apart. "
            "Expiry defaults to the listed one closest to 40 days (28..56). The row is tagged 'hypothesis' (see `warnings`). "
            "Settle with assistant_paper_status after the 08:00 UTC expiry."
        ),
    )
    async def assistant_paper_open(
        structure: Annotated[Structure, Field(description="Structure name, as in the recommendation method (e.g. long_strangle25).")],
        expiry: Annotated[str | None, Field(description="Expiry as 27NOV26 or 2026-11-27; default: closest to 40 days.")] = None,
        currency: Annotated[Literal["BTC", "ETH"], Field(description="Deribit inverse-option currency.")] = "BTC",
        deposit_usd: Annotated[float, Field(gt=0, le=1e9, description="Owner's deposit in USD (default 10000).")] = 10_000.0,
        risk_pct: Annotated[float, Field(gt=0, le=100, description="Risk per trade, percent of the deposit (default 1).")] = 1.0,
        size_btc: Annotated[float | None, Field(gt=0, le=1000, description="Override the size, in coins; default: sized by risk.")] = None,
        lot_btc: Annotated[
            float | None, Field(gt=0, le=10, description="Lot step in coins; default 0.1 for BTC and 1 for ETH (Deribit minimums).")
        ] = None,
        source: Annotated[
            Literal["manual"],
            Field(description="Only 'manual' (picked by a person or model); 'method' rows are written by the collector job alone."),
        ] = "manual",
        method_mean_pct: Annotated[
            float | None,
            Field(
                description="Method simulation mean P&L at entry, fraction of the entry forward (as `option_replay.method_summary` returns it)."
            ),
        ] = None,
        method_q05_pct: Annotated[
            float | None,
            Field(
                description="Method simulation 5 % quantile of P&L at entry, fraction of the entry forward (negative; as `method_summary` returns it); gives r_study for credits."
            ),
        ] = None,
        regime: Annotated[str | None, Field(max_length=60, description="Method regime bucket at entry.")] = None,
        note: Annotated[
            str | None, Field(max_length=1000, description="Free-form note, e.g. the thesis shown with the recommendation.")
        ] = None,
    ) -> dict[str, Any]:
        b = Builder()
        b.warn(PAPER_WARNING)
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        lot = lot_btc if lot_btc is not None else DEFAULT_LOT[currency]
        if size_btc is not None and abs(size_btc / lot - round(size_btc / lot)) > 1e-6:
            b.miss("size_btc", "tradedesk", "invalid_input", f"size_btc {size_btc:g} is not a multiple of the lot {lot:g}")
            return b.build(None, ok=False)
        await rt.store.init()
        ctx = await prepare_chain(rt, b, currency, expiry)
        if ctx is None:
            return b.build(None, ok=False)
        out = await book_paper(
            rt,
            b,
            ctx,
            structure=structure,
            currency=currency,
            deposit_usd=deposit_usd,
            risk_pct=risk_pct,
            size_btc=size_btc,
            lot=lot,
            source=source,
            method_mean_pct=method_mean_pct,
            method_q05_pct=method_q05_pct,
            regime=regime,
            note=note,
        )
        return b.build(out, ok=False) if out is None else b.build(out)

    @server.tool(
        annotations=READ_ONLY,  # settling writes to the LOCAL store only — no orders
        description=(
            "Paper journal of the assistant's option recommendations (all tagged 'hypothesis', kept apart from the real trades "
            "in `journal`). First settles every open paper trade whose expiry has passed, at the Deribit delivery price "
            "(GET delivery prices, USD): payoff of the legs minus the entry premium, entry fees and delivery fees, for the "
            "booked size. Per settled trade: `pnl_usd`, `pnl_pct_spot` (decimal: P&L per 1 BTC of structure / entry spot) and "
            "`r_worst_case` (P&L / the trade's worst-loss risk in USD; NOT the R of the 2026-10-18 report) and `r_study` "
            "(per-unit P&L / premium for a debit, / |method 5 % quantile| * spot for a credit; null for a credit without "
            "`method_q05_pct`). Main metric: `pnl_pct_spot`. Returns open trades, settled trades and `summary_settled` "
            "reported separately: 'method' (chain trades booked first / after an expiry, comparable with the walk-forward), "
            "'method_triggered' (extra trades on a regime change or an event; they overlap the chain, so they are not independent) "
            "and 'manual' (not a test of the method): n, win share, mean % of spot, mean R (both kinds), sum USD. A trade whose delivery price is not yet "
            "published stays open and is listed in `missing`. Few trades: the statistics are descriptive, not evidence."
        ),
    )
    async def assistant_paper_status(
        include_settled: Annotated[bool, Field(description="List settled trades too (newest 200).")] = True,
    ) -> dict[str, Any]:
        b = Builder()
        b.warn(PAPER_WARNING)
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        now = rt.now()
        now_ts = int(now.timestamp())
        open_rows = await rt.store.rec_paper_list(status="open", limit=5000)
        due = [x for x in open_rows if x["expiry_ts"] <= now_ts]
        if due:
            deliveries: dict[str, dict[str, float]] = {}
            for cur in sorted({x["currency"] for x in due}):
                got = await b.guard(f"delivery_prices:{cur}", "deribit", rt.deribit.delivery_prices(f"{cur.lower()}_usd", count=1000))
                if got is not None:
                    deliveries[cur] = {}
                    for d in got.get("data") or []:
                        try:
                            if d.get("delivery_price"):
                                deliveries[cur][str(d.get("date"))] = float(d["delivery_price"])
                        except (TypeError, ValueError, AttributeError):
                            b.miss(f"delivery_prices:{cur}", "deribit", "parse_error", f"bad delivery row {str(d)[:80]}")
            for t in due:
                day = datetime.fromtimestamp(t["expiry_ts"], UTC).date().isoformat()
                dp = deliveries.get(t["currency"], {}).get(day)
                if dp is None:
                    if t["currency"] in deliveries:
                        b.miss(f"rec_paper:{t['id']}", "deribit", "data_unavailable", f"no delivery price for {day} published yet")
                    continue
                res = settle_paper(t, dp)
                await rt.store.rec_paper_settle(
                    t["id"],
                    settled_ts=t["expiry_ts"],
                    delivery_price=dp,
                    pnl_usd=res["pnl_usd"],
                    pnl_pct_spot=res["pnl_pct_spot"],
                    r_worst_case=res["r_worst_case"],
                    r_study=res["r_study"],
                )
        open_now = await rt.store.rec_paper_list(status="open", limit=5000)
        settled = await rt.store.rec_paper_list(status="settled", limit=5000)
        summary: dict[str, Any] = {}
        meth = [x for x in settled if x["source"] == "method"]
        groups = {
            # 'method' = the chain trades (first / after expiry), the ones comparable with the H1 walk-forward;
            # 'method_triggered' = extra trades opened on a regime change or an event (overlap the chain, correlated)
            "method": [x for x in meth if x.get("trigger") in (None, "chain")],
            "method_triggered": [x for x in meth if x.get("trigger") not in (None, "chain")],
            "manual": [x for x in settled if x["source"] == "manual"],
        }
        for src, rows_ in groups.items():
            s = summarize(rows_)
            for k, nd in (("win_share", 3), ("mean_pnl_pct_spot", 5), ("mean_r_worst_case", 3), ("mean_r_study", 3), ("sum_pnl_usd", 2)):
                if k in s:
                    s[k] = r(s[k], nd)
            summary[src] = s
        data: dict[str, Any] = {"label": "hypothesis", "open": [_view(x) for x in open_now], "summary_settled": summary}
        if include_settled:
            data["settled"] = [_view(x) for x in settled[:200]]
        return b.build(data)
