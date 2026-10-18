"""Polymarket <-> Deribit options gap: scan open BTC/ETH price markets, store an hourly snapshot.

Docs: docs/ideas/IDEA-20261017-polymarket-vs-options-forward.md. :func:`scan_gaps` reads public data only (Gamma events,
CLOB books, the Deribit option book and DVOL, all through the shared providers) and returns one row per open market:
order-book top, volume, time to the end, resolution text and the risk-neutral probability of the same event from the
Deribit smile. The `polymarket_options_gap` tool and the hourly job share it. Nothing here trades.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING, Any

from ..analytics.options import Smile, build_chain, build_smile, group_by_expiry
from ..analytics.pm_gap import classify_open_market, model_probability, path_extremes, pick_expiry, top_of_book, touch_state
from ..core.envelope import Builder
from ..core.errors import ProviderError

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

EVENT_TAGS = ("multi-strikes", "hit-price")  # Gamma tags of the daily threshold and the touch ("hit price") events
MAX_EVENT_PAGES = 60  # pages of 100 events per tag; an unfinished listing is reported, not hidden
BOOK_CHUNK = 50  # tokens per CLOB /books request
CURRENCIES = ("BTC", "ETH")
PATH_VENUE = "binance"  # spot: the venue whose <ASSET>/USDT candles resolve the markets
PATH_BARS = 400  # 1h bars: a weekly window is 168, plus slack


async def _events(rt: "Runtime", b: Builder, now_ts: int, max_days: float, skipped: Counter) -> list[dict[str, Any]]:
    """Open BTC/ETH price markets inside the horizon, as classified dicts (see ``classify_open_market``).

    Events and BTC/ETH markets are counted per tag: a tag that returns nothing (a wrong or retired tag slug gives an empty
    list, not an error) lands in ``missing``, and so does a scan that found no BTC/ETH market under any tag, because daily
    markets always exist: zero means a broken query, not a quiet market.
    """
    found: dict[str, dict[str, Any]] = {}
    counted: set[str] = set()
    per_tag: dict[str, list[int]] = {}
    for tag in EVENT_TAGS:
        stats = per_tag[tag] = [0, 0]  # events, BTC/ETH price markets
        try:
            async for events, _last in rt.polymarket.iter_events(
                {"tag_slug": tag, "active": "true", "closed": "false"}, max_pages=MAX_EVENT_PAGES
            ):
                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    stats[0] += 1
                    for m in ev.get("markets") or []:
                        if not isinstance(m, dict):
                            continue
                        c = classify_open_market(ev, m, now_ts, max_days)
                        if c["status"] not in ("slug", "asset"):
                            stats[1] += 1
                        if c["status"] == "ok":
                            found[c["market_id"]] = c
                        elif c["status"] != "slug" and c["market_id"] not in counted:
                            counted.add(c["market_id"])  # the same event can sit under both tags
                            skipped[c["status"]] += 1  # a foreign slug is not interesting, anything else is counted
        except ProviderError as e:
            b.miss(f"events:{tag}", e.provider or "polymarket:gamma", e.reason, e.detail)
            continue
        if stats[0] == 0:
            b.miss(
                f"events:{tag}", "polymarket:gamma", "data_unavailable", f"tag '{tag}' returned no events (is the tag slug still valid?)"
            )
    b.warn("events per tag (events/BTC-ETH markets): " + ", ".join(f"{t}={s[0]}/{s[1]}" for t, s in per_tag.items()))
    if not any(s[1] for s in per_tag.values()):
        b.miss("events", "polymarket:gamma", "data_unavailable", "no BTC/ETH price market under any tag: query or slug patterns broken")
    return list(found.values())


async def _paths(rt: "Runtime", b: Builder, markets: list[dict[str, Any]], now_ts: int) -> dict[str, dict[str, Any]]:
    """Binance spot path per asset for touch markets whose window has begun: ``{asset: {"bars": [...]}}``.

    The markets resolve on Binance ``<ASSET>/USDT`` candles, so the High/Low since the window start is read from the same
    pair (1h bars: same extremes as the 1-minute candles because windows begin on a whole ET hour). A failed fetch leaves
    the asset out; its open touch markets then get ``model_prob = None`` (``no_path``).
    """
    starts: dict[str, int] = {}
    for m in markets:
        ws = m.get("window_start")
        if m["kind"] == "touch" and ws is not None and ws <= now_ts:
            starts[m["asset"]] = min(ws, starts.get(m["asset"], ws))
    out: dict[str, dict[str, Any]] = {}
    for asset, start in starts.items():
        bars = await b.guard(
            f"binance_path:{asset}",
            "ccxt:binance",
            rt.exchanges.ohlcv(PATH_VENUE, f"{asset}/USDT", "1h", PATH_BARS, since_ms=start * 1000),
        )
        if bars:
            out[asset] = {"bars": bars}
    return out


async def _books(rt: "Runtime", b: Builder, tokens: list[str]) -> dict[str, dict[str, Any]]:
    """Order book per token id (CLOB batch endpoint); a failed chunk leaves its tokens out and lands in ``missing``."""
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(tokens), BOOK_CHUNK):
        data = await b.guard(f"books:{i // BOOK_CHUNK}", "polymarket:clob", rt.polymarket.books(tokens[i : i + BOOK_CHUNK]))
        for bk in data or []:
            if isinstance(bk, dict) and bk.get("asset_id") is not None:
                out[str(bk["asset_id"])] = bk
    return out


async def scan_gaps(rt: "Runtime", b: Builder, *, max_days: float = 14.0) -> list[dict[str, Any]]:
    """One row per open market (see ``PM_GAP_COLUMNS`` in the store); never raises, failures go to ``b.missing``.

    A market without a YES order book is skipped. A market whose event has no usable Deribit expiry is kept with
    ``model_prob = None``. The Deribit side is read once per currency.
    """
    now = rt.now()
    now_ts = int(now.timestamp())
    now_ms = now_ts * 1000
    deribit: dict[str, dict[str, Any]] = {}
    for cur in CURRENCIES:
        raw = await b.guard(f"option_book:{cur}", "deribit", rt.deribit.book_summary(cur, "option"))
        chain = build_chain(raw, now) if raw else []
        dv = await b.guard(f"dvol:{cur}", "deribit", rt.deribit.volatility_index_data(cur, now_ms - 3 * 3_600_000, now_ms, 3600))
        series = (dv or {}).get("data") or []
        deribit[cur] = {
            "by_exp": group_by_expiry(chain),
            "spot": next((r.index for r in chain if r.index), None),
            "dvol": float(series[-1][4]) / 100.0 if series else None,
            "smiles": {},
        }

    skipped: Counter = Counter()
    markets = await _events(rt, b, now_ts, max_days, skipped)
    books = await _books(rt, b, [t for m in markets for t in (m["yes_token"], m["no_token"])]) if markets else {}
    paths = await _paths(rt, b, markets, now_ts)

    def smile_for(cur: str, exp: Any) -> Smile | None:
        d = deribit[cur]
        if exp not in d["smiles"]:
            d["smiles"][exp] = build_smile(d["by_exp"][exp])
        return d["smiles"][exp]

    rows: list[dict[str, Any]] = []
    for m in markets:
        yes_b, no_b = books.get(m["yes_token"]), books.get(m["no_token"])
        yes, no = top_of_book(yes_b or {}), top_of_book(no_b or {})
        if yes["bid"] is None and yes["ask"] is None:
            skipped["no_book"] += 1
            continue
        d = deribit[m["asset"]]
        ext: dict[str, float | None] = {"high": None, "low": None, "last": None}
        state = "open"
        if m["kind"] == "touch":
            if m["asset"] in paths and m["window_start"] is not None:
                ext = path_extremes(paths[m["asset"]]["bars"], m["window_start"])
            state = touch_state(m["direction"], m["strike"], now_ts, m["window_start"], ext["high"], ext["low"])
        model: dict[str, Any] | None = None
        exp = None
        if state == "touched":  # the level was already reached inside the window: the event has happened
            model = {"prob": 1.0, "iv": None, "covered": None, "note": "touched"}
        elif state != "open":  # pre_window / no_path: no honest number
            model = {"prob": None, "iv": None, "covered": None, "note": state}
        else:
            exp = pick_expiry(d["by_exp"], m["end_ts"])
            smile = smile_for(m["asset"], exp) if exp is not None else None
            if smile is not None:
                model = model_probability(smile, m["kind"], m["strike"], now_ts, m["end_ts"], m["direction"])
                if model is None:
                    exp = None
            else:
                exp = None
        rows.append(
            {
                "market_id": m["market_id"],
                "asset": m["asset"],
                "kind": m["kind"],
                "direction": m["direction"],
                "strike": m["strike"],
                "event_slug": m["event_slug"],
                "question": m["question"],
                "end_ts": m["end_ts"],
                "window_start": m["window_start"],
                "hours_to_end": (m["end_ts"] - now_ts) / 3600.0,
                "yes_bid": yes["bid"],
                "yes_ask": yes["ask"],
                "yes_bid_usd": yes["bid_usd"],
                "yes_ask_usd": yes["ask_usd"],
                "no_bid": no["bid"],
                "no_ask": no["ask"],
                "no_bid_usd": no["bid_usd"],
                "no_ask_usd": no["ask_usd"],
                "volume_usd": m["volume_usd"],
                "fee_rate": m["fee_rate"],
                "fee_exp": m["fee_exp"],
                "rules": m["rules"],
                "rules_hash": m["rules_hash"],
                "rules_standard": int(m["rules_standard"]),
                "path_high": ext["high"],
                "path_low": ext["low"],
                "binance_last": ext["last"],
                "model_prob": model["prob"] if model else None,
                "model_note": model["note"] if model else "no_expiry",
                "model_iv": model["iv"] if model else None,
                "model_expiry": exp.isoformat() if model and exp else None,
                "expiry_gap_hours": (exp.timestamp() - m["end_ts"]) / 3600.0 if model and exp else None,
                "model_covered": int(model["covered"]) if model and model["covered"] is not None else None,
                "spot": d["spot"],
                "dvol": d["dvol"],
            }
        )
    if skipped:
        b.warn("markets left out: " + ", ".join(f"{k}={v}" for k, v in sorted(skipped.items())))
    return rows


async def collect_pm_gap(rt: "Runtime") -> int:
    """Collector job: store one snapshot of ``scan_gaps``. Returns the rows written.

    Fails (so the scheduler records it) only when nothing was found and a provider reported a problem; an empty
    market list without errors (no open markets) is a normal zero.
    """
    assert rt.store is not None
    b = Builder()
    rows = await scan_gaps(rt, b)
    if not rows and b.missing:
        raise RuntimeError("pm_gap: no rows; " + "; ".join(f"{m.field}: {m.reason}" for m in b.missing[:4]))
    if b.missing:
        log.warning("pm_gap: partial snapshot, missing: %s", "; ".join(f"{m.field}: {m.reason}" for m in b.missing[:6]))
    return await rt.store.insert_pm_gap(int(rt.now().timestamp()), rows)
