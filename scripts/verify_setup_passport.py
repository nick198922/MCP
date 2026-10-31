#!/usr/bin/env python3
"""Setup passport (IDEA-20261030-demo-setups-depth, items 2-5): the demo setup's trades on history with the demo's
rules and costs, as the reference its live trades are compared with. Pre-registration: the card's sections
«Попередня реєстрація паспорта» and, for S14 / S16 (the demo's ``WIDE_SETUPS``: verdict on the top-250 >= $1M list,
turnover groups on it, a descriptive chain shared by S14 + S16 + S3), «Попередня реєстрація паспортів S14 і S16»;
for S38 / S39 / S40 (4h; S38 with the demo's 2 entries per signal bar; the turnover contrast by year and pooled over
the three; a chain shared by all six setups) «Попередня реєстрація паспортів S38, S39 і S40»; for S12 v7 / S28 (a limit
resting at the level, filled only when the price trades through it; an unfilled limit holds the coin until its
deadline; fill rate, R per placement, a placebo of random limits with the same geometry) «… S12 v7 і S28»; for S17 /
S23 (the same limit model with each signal's own wait and hold; S23's limit cancelled by its target before the fill;
S17 reported per timeframe with ``--tf 1d|4h``, each its own chain) «… S17 і S23».

    uv run python scripts/verify_setup_passport.py S3 --stage sanity        # coverage, units, known number, timing
    uv run python scripts/verify_setup_passport.py S3 --stage run           # per-symbol candidate files (4 workers)
    uv run python scripts/verify_setup_passport.py S3 --stage report        # tables (also <cache>/<setup>/report.txt)

No network: 1h Binance USD-M bars and the point-in-time top-30 universe from the ``course_s14_ema`` cache
(``--s1-cache``), listing / exchange info / daily candles from ``funding_crowding`` (``--data-dir``), BTC cycle-regime
inputs from ``cycle_regime`` (``--regime-cache``), all filled earlier through the runtime providers.

Every candidate signal is simulated once (rule B and Bx, stress costs, late entry) and written to
``<cache>/<setup>/cands/<symbol>.json``; the report applies the demo's selection, the turnover groups and the
one-trade-per-symbol chain afterwards, so every population comes from the same files.
"""

from __future__ import annotations

import argparse
import json
import math
import resource
import sys
import time
import zlib
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s1  # noqa: E402  (loaders, universe, listing)

from tradedesk.analytics import cycle_regime as cr  # noqa: E402
from tradedesk.analytics import cycle_regime_eval as cre  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics import setup_signals as ss  # noqa: E402
from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars, resample, segment_pos  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.market_flow import points  # noqa: E402
from tradedesk.analytics.stop_management import Fees, StopRule, chi2_sf, cochran_q, simulate_stop_rule  # noqa: E402
from tradedesk.execution.orders import reward_risk_at  # noqa: E402  (read-only: the guard's R:R after fees)

DAY_MS = 24 * HOUR_MS
SEED = 20261030
N_BOOT = 10_000
START_MS = s1.ms("2020-02-01T00:00")  # candidates from here (the known-number check); the passport starts in 2021
PASSPORT_MS = s1.ms("2021-01-01T00:00")
OOS_MS = s1.ms("2025-03-01T00:00")
END = {"4h": s1.ms("2026-09-23T00:00"), "1d": s1.ms("2026-08-31T00:00")}
HOURS = {"4h": 4, "1d": 24}
HOLD = {"4h": 7 * DAY_MS, "1d": 30 * DAY_MS}  # demo_trader.MAX_HOLD_MS
MIN_RR, MAX_STOP, MIN_SCORE = 1.5, 0.10, 2  # guard.min_reward_risk, guard.max_stop_pct, demo_trader.MIN_SCORE
RULE_B, RULE_BX = StopRule("B", be_frac=0.5), StopRule("Bx", be_frac=0.5, be_same_bar=True)
F1, F15 = Fees(), Fees().scaled(1.5)
BTC = "BTCUSDT"
TOP_N = 100  # demo_trader scans guard.universe(size=100, min_turnover_usd=5e6): top-100 by turnover AND >= $5M
# demo_trader.WIDE_SETUPS also scan guard.universe(size=WIDE_SIZE=250, min_turnover_usd=WIDE_MIN_TURNOVER_USD=1e6); the
# union with the top-100 >= $5M list is the top-250 >= $1M list (registration «Попередня реєстрація паспортів S14 і S16»)
WIDE = ("S14", "S16", "S38", "S39", "S40")
WIDE_TOP_N = 250
DEMO_ORDER = ("S14", "S16", "S3", "S38", "S39", "S40")  # demo_trader.SETUPS order of the setups with a passport chain
JOINT = {"S14": DEMO_ORDER[:3], "S16": DEMO_ORDER[:3]}  # S14 / S16 registered the chain with S3 only; S38-S40 all six
BAR_CAP = {"S38": 2}  # demo_trader.BAR_CAP: entries per signal bar (registration «… S38, S39 і S40»)
SHORT_FAMILY = ("S38", "S39", "S40")  # pooled turnover contrast and Holm on their 6 contrasts


def top_n_of(setup: str) -> int:
    return WIDE_TOP_N if setup in WIDE else TOP_N


def _only(name: str, fn: Callable[..., list[ss.LiveSignal]]) -> Callable[[Bars, str], list[ss.LiveSignal]]:
    return lambda b, tf: [s for s in fn(b, tf) if s.setup == name]


# setup -> (full-history detector, timeframes, primary turnover floor in USD: the demo's scan list)
SETUPS: dict[str, tuple[Callable[[Bars, str], list[ss.LiveSignal]], tuple[str, ...], float]] = {
    "S3": (_only("S3", ss.range_all), ("4h", "1d"), 5e6),
    "S14": (lambda b, tf: ss.s14_all(b, tf), ("4h", "1d"), 1e6),
    "S16": (_only("S16", ss.range_all), ("4h", "1d"), 1e6),
    "S38": (lambda b, tf: ss.s38_all(b, tf), ("4h",), 1e6),
    "S39": (lambda b, tf: ss.s39_all(b, tf), ("4h",), 1e6),
    "S40": (lambda b, tf: ss.s40_all(b, tf), ("4h",), 1e6),
    # S12 v7 / S28: the entry is a limit resting at the level until ``rest_until_ms`` (registration «… S12 v7 і S28»);
    # not in demo_trader.WIDE_SETUPS, so the demo list is the top-100 >= $5M
    "S12": (lambda b, tf: ss.s12_rest_all(b, tf), ("4h",), 5e6),
    "S28": (lambda b, tf: ss.s28_all(b, tf), ("4h",), 5e6),
    # S17 (POB retest, 1d main row, 4h side row) / S23 (SFP retest, QM proxy, 4h): resting limits too (registration
    # «… S17 і S23», item 11); not in demo_trader.WIDE_SETUPS, so the default demo list (top-100 >= $5M)
    "S17": (lambda b, tf: ss.s17_all(b, tf), ("1d", "4h"), 5e6),
    "S23": (lambda b, tf: ss.s23_all(b, tf), ("4h",), 5e6),
}
LIMIT = ("S12", "S28", "S17", "S23")
CANCEL_AT_TARGET = ("S23",)  # ltf_entry "retest": a target reached before the fill cancels the limit
LIMIT_WARMUP = {"S12": ss.S12_WARMUP, "S28": ss.S12_WARMUP, "S17": ss.S17_WARMUP, "S23": ss.S23_WINDOW - 1}  # placebo pool
RULE_A = StopRule("A")  # no break-even: the known numbers' backtests (step 5 S17, step 2 SFP)
KNOWN_FROM_MS = s1.ms("2024-10-12T00:00")  # S23 known number: 730 days to 2026-10-12 (verify_sfp_ltf.py --days 730)


def own_hold(setup: str, sig: ss.LiveSignal, tf: str, bar_ms: int) -> int:
    """The registered hold of a limit setup's filled trade, from the signal's fields: S23 ``max_hold_ms`` (10 days);
    S17 20 bars of its timeframe (``S17_HOLD_BARS``); S12 v7 / S28 the demo's 7 days on 4h."""
    if "max_hold_ms" in sig.indicators:
        return int(sig.indicators["max_hold_ms"])
    if setup == "S17":
        return ss.S17_HOLD_BARS * bar_ms
    return HOLD[tf]


N_PLACEBO = 2  # random limits of the same geometry per signal (registered)
LIMIT_WIDE_TOP_N, LIMIT_WIDE_FLOOR = 250, 1e6  # registered side row: could the setup take the $1-5M coins?
OUT: dict[str, Any] = {"fh": None, "tf": None}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def iso(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- data


def load_1h_qv(cache: Path, data_dir: Path, s: str) -> tuple[Bars, np.ndarray]:
    """``s1.load_1h`` keeping the quote volume (USD) of each bar."""
    rows: list[list[float]] = []
    for mo in s1.listing(data_dir, s):
        p = cache / "k1h" / s / f"{mo}.json"
        if p.exists():
            rows.extend(s1.rd(p) or [])
    a = np.array(rows, dtype=float) if rows else np.zeros((0, 6))
    if a.size:
        a = a[np.argsort(a[:, 0], kind="stable")]
        a = a[np.concatenate([[True], a[1:, 0] != a[:-1, 0]])]
        a = a[(a[:, 5] > 0) & (a[:, 0] % HOUR_MS == 0)]
    return Bars.from_rows(a[:, :5] if a.size else a, HOUR_MS), (a[:, 5].copy() if a.size else np.zeros(0))


def syms_with_1h(s1_cache: Path, data_dir: Path) -> list[str]:
    have = {p.name for p in (s1_cache / "k1h").iterdir() if p.is_dir()}
    return sorted(set(s1.eligible_syms(data_dir)) & have)


def regime_states(regime_cache: Path) -> dict[int, str | None]:
    spot = cr.parse_closes(json.loads((regime_cache / "spot_1d.json").read_text())["rows"])
    perp = cr.parse_closes(json.loads((regime_cache / "perp_1d.json").read_text())["rows"])
    fund_rows = json.loads((regime_cache / "funding.json").read_text())["rows"]
    funding = points(({"t": r[0], "r": r[1]} for r in fund_rows), "t", "r", lo=-0.1, hi=0.1)
    rows = cre.state_series(spot, perp, funding, START_MS - 2 * DAY_MS, END["4h"])
    return {int(r["t"]): r["state"] for r in rows}


def btc_daily_trend(s1_cache: Path, data_dir: Path) -> dict[str, list[float]]:
    h1, _ = load_1h_qv(s1_cache, data_dir, BTC)
    d = resample(h1, 24)
    return {"t": d.t.tolist(), "trend": sp.trend_series(d).tolist()}


# ---------------------------------------------------------------- per-symbol candidates


def _outcome(path: Bars, t_in: int, e: float, st: float, tg: float, hold: int, rule: StopRule, fees: Fees) -> Any:
    return simulate_stop_rule(path, t_in, e, st, tg, hold, rule, fees=fees)


def run_symbol(args: tuple[str, str, str, str, str]) -> dict[str, Any]:
    setup, s, s1_cache, data_dir, out_dir = args
    t0 = time.time()
    s1c, dd, out = Path(s1_cache), Path(data_dir), Path(out_dir)
    det, tfs, _ = SETUPS[setup]
    member = s1.Membership(s1.members(s1c))
    btc = json.loads((out / "btc_trend.json").read_text())
    btc_t = np.asarray(btc["t"], dtype=np.int64) + DAY_MS  # close times
    btc_tr = np.asarray(btc["trend"], dtype=np.int64)
    states = {int(k): v for k, v in json.loads((out / "regimes.json").read_text()).items()}
    h1, qv = load_1h_qv(s1c, dd, s)
    daily = resample(h1, 24)
    d_tr = sp.trend_series(daily)
    why: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    placebo: list[dict[str, Any]] = []
    for tf in tfs:
        tb = resample(h1, HOURS[tf])
        if setup in LIMIT:

            def ctx(t_in: int, side: str, e: float, st: float, tg: float) -> dict[str, Any] | None:
                """The demo's view at a placement (real prices): None if under $1M / unknown turnover."""
                top = member(s, t_in)
                turn = sp.trailing_turnover(t_in, h1.t, qv)
                grp = sp.turnover_group(top, turn)
                if grp in (sp.LT1M, sp.UNKNOWN):
                    return None
                j = int(np.searchsorted(btc_t, t_in, side="right")) - 1
                rr = reward_risk_at(side, e, st, tg)
                sc = sp.score_points(side, rr, sp.trend_at(daily, d_tr, t_in), int(btc_tr[j]) if j >= 0 else 0, s == BTC)
                return {
                    "sym": s,
                    "side": side,
                    "t_in": t_in,
                    "top30": top,
                    "turn": turn,
                    "grp": grp,
                    "rr": rr,
                    "stop_pct": abs(e - st) / abs(e),
                    "score": sc,
                    "regime": sp.state_before(t_in, states),
                }

            rows_l, pl_l = limit_rows(setup, s, tf, tb, h1, det(tb, tf), ctx, states, why)
            rows.extend(rows_l)
            placebo.extend(pl_l)
            continue
        for sig in det(tb, tf):
            why[f"{tf}:signal"] += 1
            i = int(np.searchsorted(tb.t, sig.signal_ts))
            t_in = sig.signal_ts + tb.bar_ms
            if not (START_MS <= t_in < END[tf]):
                continue
            top = member(s, t_in)
            turn = sp.trailing_turnover(t_in, h1.t, qv)
            grp = sp.turnover_group(top, turn)
            if grp in (sp.LT1M, sp.UNKNOWN):
                why[f"{tf}:{grp}"] += 1
                continue
            sg = 1.0 if sig.side == "long" else -1.0
            e, st, tg = sg * sig.entry, sg * sig.stop, sg * sig.target
            if not (st < e < tg):
                why[f"{tf}:levels"] += 1
                continue
            path = h1 if sig.side == "long" else h1.mirrored()
            hold = HOLD[tf]
            b = _outcome(path, t_in, e, st, tg, hold, RULE_B, F1)
            if b is None:
                why[f"{tf}:no_path"] += 1
                continue
            bx = _outcome(path, t_in, e, st, tg, hold, RULE_BX, F1)
            b15 = _outcome(path, t_in, e, st, tg, hold, RULE_B, F15)
            bb = tb if sig.side == "long" else tb.mirrored()
            late = sp.late_entry(bb, i, st, tg)
            lb = lb15 = None
            if late is not None:
                lb = _outcome(path, late[0], late[1], st, tg, hold, RULE_B, F1)
                lb15 = _outcome(path, late[0], late[1], st, tg, hold, RULE_B, F15)
            j = int(np.searchsorted(btc_t, t_in, side="right")) - 1
            coin_trend = sp.trend_at(daily, d_tr, t_in)
            btc_trend = int(btc_tr[j]) if j >= 0 else 0
            rr = reward_risk_at(sig.side, sig.entry, sig.stop, sig.target)
            rows.append(
                {
                    "sym": s,
                    "tf": tf,
                    "side": sig.side,
                    "t_in": t_in,
                    "top30": top,
                    "turn": turn,
                    "grp": grp,
                    "rr": rr,
                    "stop_pct": abs(sig.entry - sig.stop) / abs(sig.entry),
                    "score": sp.score_points(sig.side, rr, coin_trend, btc_trend, s == BTC),
                    "regime": sp.state_before(t_in, states),
                    "B": b.r_net,
                    "B_k": b.exit_kind,
                    "B_x": b.exit_ms,
                    "Bx": bx.r_net if bx else None,
                    "B15": b15.r_net if b15 else None,
                    "L": lb.r_net if lb else None,
                    "L15": lb15.r_net if lb15 else None,
                    "cost": b.cost_r,
                }
            )
            why[f"{tf}:cand"] += 1
    (out / "cands").mkdir(parents=True, exist_ok=True)
    (out / "cands" / f"{s}.json").write_text(json.dumps({"rows": rows, "why": dict(why), "placebo": placebo}))
    return {"sym": s, "n": len(rows), "why": dict(why), "sec": time.time() - t0, "bars": len(h1), "rss": rss_mb()}


def _lim(
    path: Bars,
    t0: int,
    t1: int,
    e: float,
    st: float,
    tg: float,
    rule: StopRule,
    fees: Fees,
    through: bool = True,
    hold: int = HOLD["4h"],
    cancel: bool = False,
) -> tuple[Any, int]:
    return sp.limit_trade(path, t0, t1, e, st, tg, hold, rule, fees, through, cancel)


def limit_rows(
    setup: str,
    s: str,
    tf: str,
    tb: Bars,
    h1: Bars,
    sigs: list[ss.LiveSignal],
    ctx: Callable[[int, str, float, float, float], dict[str, Any] | None],
    states: dict[int, str | None],
    why: Counter[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rows of a resting-limit setup (S12 v7, S28): one per placement, filled or not (an unfilled limit holds the coin
    until its deadline, so it stays in the chain with B = None), and the registered placebo rows (random limits of the
    same coin, side and geometry, pre-filtered to the demo selection at >= $5M: the only sample they are compared with)."""
    rows: list[dict[str, Any]] = []
    placebo: list[dict[str, Any]] = []
    rng = np.random.default_rng(SEED + zlib.crc32(f"{s}{setup}".encode()))
    pos = segment_pos(tb.t)
    close_t = tb.t + tb.bar_ms
    pool = np.flatnonzero((pos >= LIMIT_WARMUP[setup]) & (close_t >= START_MS) & (close_t < END[tf]))
    cancel = setup in CANCEL_AT_TARGET
    for sig in sigs:
        why[f"{tf}:signal"] += 1
        i = int(np.searchsorted(tb.t, sig.signal_ts))
        t0 = sig.signal_ts + tb.bar_ms
        if not (START_MS <= t0 < END[tf]):
            continue
        t1 = int(sig.indicators["rest_until_ms"])
        hold = own_hold(setup, sig, tf, tb.bar_ms)
        sg = 1.0 if sig.side == "long" else -1.0
        e, st, tg = sg * sig.entry, sg * sig.stop, sg * sig.target
        bb = tb if sig.side == "long" else tb.mirrored()
        path = h1 if sig.side == "long" else h1.mirrored()
        if not (st < e < tg):
            why[f"{tf}:levels"] += 1
            continue
        c_sig = float(bb.c[i])
        for _ in range(N_PLACEBO):  # drawn for every signal in period, before any filter (same draws whatever the filters)
            if pool.size == 0:
                break
            r = int(pool[rng.integers(pool.size)])
            c_r = float(bb.c[r])
            e_r, st_r = c_r * e / c_sig, c_r * st / c_sig
            # S23's target is the range middle, not 2R: carried in percent like the level and the stop (registration
            # «… S17 і S23»); for the 2R setups the two forms are equal, the 2R form is kept so S12 / S28 do not move
            tg_r = c_r * tg / c_sig if setup == "S23" else e_r + 2.0 * (e_r - st_r)
            tr0 = int(tb.t[r]) + tb.bar_ms
            x = ctx(tr0, sig.side, sg * e_r, sg * st_r, sg * tg_r)
            if (
                x is None
                or x["turn"] < 5e6
                or x["rr"] < MIN_RR
                or x["stop_pct"] > MAX_STOP
                or x["score"] < MIN_SCORE
                or not (st_r < e_r < tg_r)
            ):
                continue
            o, tk = _lim(path, tr0, tr0 + (t1 - t0), e_r, st_r, tg_r, RULE_B, F1, hold=hold, cancel=cancel)
            if o is None and tk >= 0:
                continue
            placebo.append(
                {**x, "tf": tf, "B": o.r_net if o else None, "B_x": o.exit_ms if o else tr0 + (t1 - t0), "filled": o is not None}
            )
        x = ctx(t0, sig.side, sig.entry, sig.stop, sig.target)
        if x is None:
            why[f"{tf}:under_1m_or_unknown"] += 1
            continue
        if not c_sig > e:  # the demo does not place a post-only limit the price already reached
            why[f"{tf}:at_level"] += 1
            continue
        kw = {"hold": hold, "cancel": cancel}
        b, tk = _lim(path, t0, t1, e, st, tg, RULE_B, F1, **kw)
        if b is None and tk >= 0:
            why[f"{tf}:no_path"] += 1
            continue
        filled = b is not None
        bx = b15 = dh = None
        if filled:
            bx, _ = _lim(path, t0, t1, e, st, tg, RULE_BX, F1, **kw)
            b15, _ = _lim(path, t0, t1, e, st, tg, RULE_B, F15, **kw)
            dh, _ = _lim(path, t0, t1, e, st, tg, RULE_B, F1, hold=HOLD[tf], cancel=cancel)  # demo_trader.MAX_HOLD_MS
        bt, tkt = _lim(path, t0, t1, e, st, tg, RULE_B, F1, through=False, **kw)
        ka, _ = _lim(path, t0, t1, e, st, tg, RULE_A, F1, through=False, **kw)  # known numbers: touch, no break-even
        lb = lb15 = None
        late_placed = i + 1 < len(bb) and int(bb.t[i + 1]) - int(bb.t[i]) == bb.bar_ms and float(bb.c[i + 1]) > e
        if late_placed:
            lb, _ = _lim(path, t0 + bb.bar_ms, t1 + bb.bar_ms, e, st, tg, RULE_B, F1, **kw)
            lb15, _ = _lim(path, t0 + bb.bar_ms, t1 + bb.bar_ms, e, st, tg, RULE_B, F15, **kw)
        rows.append(
            {
                **x,
                "tf": tf,
                "filled": filled,
                "t_fill": tk,
                "B": b.r_net if b else None,
                "B_k": b.exit_kind if b else "unfilled",
                "B_x": b.exit_ms if b else t1,
                "Bx": bx.r_net if bx else None,
                "B15": b15.r_net if b15 else None,
                "L": lb.r_net if lb else None,
                "L15": lb15.r_net if lb15 else None,
                "late_placed": bool(late_placed),
                "T": bt.r_net if bt else None,
                "T_x": bt.exit_ms if bt else (t1 if tkt < 0 else None),
                "cost": b.cost_r if b else None,
                "D": dh.r_net if dh else None,
                "K": ka.r_net if ka else None,
                "K_c": ka.cost_r if ka else None,
            }
        )
        why[f"{tf}:cand"] += 1
        why[f"{tf}:filled"] += int(filled)
    return rows, placebo


def prepare(a: argparse.Namespace, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    if not (out / "regimes.json").exists():
        t0 = time.time()
        st = regime_states(a.regime_cache)
        (out / "regimes.json").write_text(json.dumps({str(k): v for k, v in st.items()}))
        emit(f"regimes: {len(st)} days in {time.time() - t0:.0f}s; {dict(Counter(v or 'none' for v in st.values()))}")
    if not (out / "btc_trend.json").exists():
        (out / "btc_trend.json").write_text(json.dumps(btc_daily_trend(a.s1_cache, a.data_dir)))


def stage_run(a: argparse.Namespace, out: Path, syms: list[str]) -> None:
    prepare(a, out)
    jobs = [(a.setup, s, str(a.s1_cache), str(a.data_dir), str(out)) for s in syms if a.force or not (out / "cands" / f"{s}.json").exists()]
    emit(f"[run] {a.setup}: {len(jobs)} of {len(syms)} symbols to do, {a.workers} workers")
    t0 = time.time()
    tot = Counter()
    with Pool(a.workers, maxtasksperchild=20) as pool:
        for k, r in enumerate(pool.imap_unordered(run_symbol, jobs), 1):
            tot.update(r["why"])
            if k % 20 == 0 or k == len(jobs):
                el = time.time() - t0
                emit(
                    f"  [run] {k}/{len(jobs)} {el:.0f}s eta {el / k * (len(jobs) - k):.0f}s; last {r['sym']} n={r['n']} {r['sec']:.1f}s worker rss {r['rss']:.0f} MB; main rss {rss_mb():.0f} MB"
                )
    emit(f"[run] done in {time.time() - t0:.0f}s; {dict(sorted(tot.items()))}")


# ---------------------------------------------------------------- sanity


def stage_sanity(a: argparse.Namespace, out: Path, syms: list[str]) -> None:
    elig = s1.eligible_syms(a.data_dir)
    emit(f"eligible USD-M perps (no TradFi, incl. delisted): {len(elig)}; with 1h cache: {len(syms)}")
    have = set(syms)
    qv = s1.daily_qv(a.s1_cache, a.data_dir, elig)
    cnt: Counter[str] = Counter()
    for s, rows in qv.items():
        for t, v in rows:
            if not (PASSPORT_MS <= t < END["4h"]):
                continue
            g = "ge5m" if v >= 5e6 else "1to5m" if v >= 1e6 else None
            if g:
                cnt[f"{g}:all"] += 1
                cnt[f"{g}:have"] += int(s in have)
    for g in ("ge5m", "1to5m"):
        emit(
            f"coin-days 2021-01..2026-09 with daily turnover {g}: {cnt[g + ':all']}, with 1h cache {cnt[g + ':have']} ({cnt[g + ':have'] / max(1, cnt[g + ':all']):.1%})"
        )
    h1, q = load_1h_qv(a.s1_cache, a.data_dir, BTC)
    i = int(np.searchsorted(h1.t, s1.ms("2024-03-01T00:00")))
    emit(
        f"BTC 1h: {len(h1)} bars {iso(int(h1.t[0]))}..{iso(int(h1.t[-1]))}; 2024-03-01 00:00 close {h1.c[i]:.1f} (known ~62.4k); 24h quote turnover then ${sp.trailing_turnover(int(h1.t[i]), h1.t, q) / 1e9:.1f}B"
    )
    gaps = int(np.sum(np.diff(h1.t) > HOUR_MS))
    emit(f"BTC 1h gaps > 1h: {gaps}")
    prepare(a, out)
    t0 = time.time()
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        r = run_symbol((a.setup, s, str(a.s1_cache), str(a.data_dir), str(out)))
        emit(f"timing {s}: n={r['n']} {r['sec']:.1f}s rss {r['rss']:.0f} MB {r['why']}")
    emit(
        f"3 symbols in {time.time() - t0:.0f}s -> about {(time.time() - t0) / 3 * len(syms) / a.workers / 60:.0f} min for {len(syms)} on {a.workers} workers"
    )


# ---------------------------------------------------------------- report


def load_rows(out: Path, part: str = "rows") -> dict[str, np.ndarray]:
    rows: list[dict[str, Any]] = []
    for p in sorted((out / "cands").glob("*.json")):
        rows.extend(json.loads(p.read_text()).get(part, []))
    if OUT.get("tf"):  # --tf: one timeframe's rows only (S17: 1d verdict and 4h side row, each its own chain)
        rows = [r for r in rows if r["tf"] == OUT["tf"]]
    keys = rows[0].keys()
    T = {k: np.array([r[k] for r in rows], dtype=object) for k in keys}
    for k in ("t_in", "B_x"):
        T[k] = T[k].astype(np.int64)
    for k in ("B", "Bx", "B15", "L", "L15", "rr", "stop_pct", "cost", "T", "T_x", "D", "K", "K_c"):
        if k in T:
            T[k] = np.array([math.nan if x is None else float(x) for x in T[k]])
    for k in ("filled", "late_placed"):
        if k in T:
            T[k] = T[k].astype(bool)
    T["turn"] = np.array([math.nan if x is None else float(x) for x in T["turn"]])
    T["score"] = T["score"].astype(np.int64)
    T["top30"] = T["top30"].astype(bool)
    T["week"] = T["t_in"] // sp.WEEK_MS
    T["year"] = np.array([datetime.fromtimestamp(t / 1000, UTC).year for t in T["t_in"]])
    T["order"] = np.array([(0 if tf == "4h" else 1) * 2 + (0 if sd == "long" else 1) for tf, sd in zip(T["tf"], T["side"], strict=True)])
    return T


def demo_rank(T: dict[str, np.ndarray]) -> np.ndarray:
    """The demo's order among candidates of one bar (``_select_and_enter``): higher score, then higher R:R after fees;
    then the larger 24h turnover (assumed order of the demo's list). With the target at exactly 2R, higher R:R means
    a wider stop; the S38 verdict sample has no same-bar R:R ties, so the turnover key never decides (review
    2026-10-30: verdict robust to the within-bar order, OOS -0.044 .. +0.006 over 200 random orders)."""
    o = np.lexsort((-np.nan_to_num(T["turn"], nan=-1.0), -T["rr"], -T["score"]))
    r = np.empty(o.size, dtype=float)
    r[o] = np.arange(o.size)
    return r


def chain(T: dict[str, np.ndarray], m: np.ndarray, cap: int | None = None) -> np.ndarray:
    idx = np.flatnonzero(m)
    syms = [str(x) for x in T["sym"][idx]]
    if cap is not None:
        keep = sp.busy_chain_capped(syms, T["t_in"][idx], T["B_x"][idx], demo_rank(T)[idx], cap)
        out = np.zeros(m.size, dtype=bool)
        out[idx[keep]] = True
        return out
    keep = sp.busy_chain(syms, T["t_in"][idx], T["B_x"][idx], T["order"][idx])
    out = np.zeros(m.size, dtype=bool)
    out[idx[keep]] = True
    return out


def ci(T: dict[str, np.ndarray], m: np.ndarray, key: str = "B") -> dict[str, float]:
    v = T[key][m]
    ok = np.isfinite(v)
    return sp.mean_ci(v[ok], T["week"][m][ok], n_boot=N_BOOT, seed=SEED)


def fmt(r: dict[str, float]) -> str:
    if r["n"] == 0:
        return "| 0 | — | — | — |"
    lo = "—" if math.isnan(r["lo"]) else f"{r['lo']:+.3f}"
    hi = "—" if math.isnan(r["hi"]) else f"{r['hi']:+.3f}"
    p = "—" if math.isnan(r["p"]) else f"{r['p']:.3f}"
    return f"| {int(r['n'])} | {r['m']:+.3f} | [{lo}; {hi}] | {p} |"


def stage_report(a: argparse.Namespace, out: Path) -> None:
    T = load_rows(out)
    floor = SETUPS[a.setup][2]
    period = T["t_in"] >= PASSPORT_MS
    demo = (T["rr"] >= MIN_RR) & (T["stop_pct"] <= MAX_STOP) & (T["score"] >= MIN_SCORE)
    turn5 = np.nan_to_num(T["turn"], nan=-1) >= floor
    cap = BAR_CAP.get(a.setup)
    P = chain(T, period & demo & turn5, cap)
    W = chain(T, period & demo)  # >= $1M (the candidates already are)
    A = chain(T, period & turn5)
    IS, OOS = T["t_in"] < OOS_MS, T["t_in"] >= OOS_MS
    elig = s1.eligible_syms(a.data_dir)
    qv = s1.daily_qv(a.s1_cache, a.data_dir, elig)
    top_n, wide, fl = top_n_of(a.setup), a.setup in WIDE, f"≥ ${floor / 1e6:g}M"
    top_days = top_n_days(qv, top_n)
    in_top = in_prev_day_top(T, top_days)
    # Correction after review (2026-10-30): the demo scans guard.universe(size=100, min_turnover_usd=5e6), i.e. the
    # top-100 by turnover AND >= $5M, not every coin >= $5M as the registration said. WIDE setups: top-250 and >= $1M,
    # registered before their run as the verdict sample (V); S3 keeps its registered >= $5M row as V.
    P100 = chain(T, period & demo & turn5 & in_top, cap)
    P100_nocap = chain(T, period & demo & turn5 & in_top) if cap else P100
    lim = a.setup in LIMIT
    if lim:
        # registration «… S12 v7 і S28»: the chains hold the coin from the placement (an unfilled limit until its
        # deadline); the statistics are on the filled trades; the side row is the top-250 >= $1M list
        top250 = top_n_days(qv, LIMIT_WIDE_TOP_N)
        G = chain(T, period & demo & in_prev_day_top(T, top250))
        limit_section(a, out, T, {"P100": P100, "P": P, "G": G, "A": A}, IS, OOS, period, demo, qv)
        F = T["filled"]
        P, W, A, P100, P100_nocap, G = (x & F for x in (P, W, A, P100, P100_nocap, G))
    wide = wide or lim  # the verdict is on the demo list (top-N) for both families
    V = P100 if wide else P  # verdict sample
    if not lim:
        G = P100 if wide else W  # base of the turnover table
    pl = f"усі {fl} + відбір демо (без рангу)" if wide else "основна"
    emit(
        f"# Паспорт {a.setup}: кандидатів {T['B'].size}; {pl if wide else f'основна вибірка (оборот {fl} + відбір демо)'} {int(P.sum())}; ≥ $1M {int(W.sum())}; усі сигнали {fl} {int(A.sum())}; топ-{top_n} і {fl} + відбір демо {int(P100.sum())}"
    )
    emit(
        f"сигнали за відбором демо (≥ ${floor / 1e6:g}M, 2021+, до ланцюга): R:R≥1.5 {int((period & turn5 & (T['rr'] >= MIN_RR)).sum())}, + стоп≤10 % {int((period & turn5 & (T['rr'] >= MIN_RR) & (T['stop_pct'] <= MAX_STOP)).sum())}, + бал≥2 {int((period & turn5 & demo).sum())} з {int((period & turn5).sum())}"
    )
    if a.setup == "S3":  # known number: demo-stop-management 22.10, S3 4h top-30 2020-02.., rule B: -0.045R, n 10712
        kn = chain(T, (T["tf"] == "4h") & T["top30"])
        emit(
            "звірка з 2026-10-22-demo-stop-management (S3 4h топ-30, усі сигнали, з 2020-02; там −0.045 [−0.077; −0.013], n 10712, зайнятість спільна з S14/S16) "
            + fmt(ci(T, kn))
        )
    if a.setup == "S14":  # known number: step 1 (2026-10-17-course-s14-ema), S14 4h long sweep, top-30, IS: 508 signals
        kn = (T["tf"] == "4h") & (T["side"] == "long") & T["top30"] & IS
        emit(
            "звірка з кроком 1 (2026-10-17-course-s14-ema: S14 4h лонг, топ-30, IS, усі сигнали без ланцюга; там n 508, "
            f"−0.080R з ринковими витратами і власним виходом — R не порівнюється): кандидатів тут {int(kn.sum())} "
            f"({int(kn.sum()) - 508:+d}, {int(kn.sum()) / 508 - 1:+.1%}); правило B " + fmt(ci(T, kn))
        )
    known = {"S38": 2609, "S39": 862, "S40": 1211}  # 2026-10-24-short-setups-demo, «Потужність»: 4h top-30 IS, guard
    if a.setup in known:
        kn = T["top30"] & IS & (T["rr"] >= MIN_RR) & (T["stop_pct"] <= MAX_STOP)
        k, ref = int(kn.sum()), known[a.setup]
        emit(
            f"звірка з кроком 1 (2026-10-24-short-setups-demo: 4h, топ-30, IS з 2020-02, сигнали з guard до правила однієї "
            f"позиції; там n {ref}; R не порівнюється): кандидатів тут {k} ({k - ref:+d}, {k / ref - 1:+.1%}); правило B " + fmt(ci(T, kn))
        )
    hdr = "| вибірка | n | E[R] net | 95 % CI (тижневі блоки) | p |\n|---|---|---|---|---|"
    emit("\n## Загалом (правило B; R на ризик угоди)\n" + hdr)
    vtag = " — вердикт" if wide else ""
    rows = [
        (f"{pl} 2021+", P, "B"),
        (f"{pl} IS (< 2025-03)", P & IS, "B"),
        ("**основна OOS (≥ 2025-03) — вердикт**" if not wide else f"{pl} OOS (≥ 2025-03)", P & OOS, "B"),
        (f"{pl} OOS, Bx (беззбиток і в барі тригера)", P & OOS, "Bx"),
        (f"{pl} 2021+, Bx", P, "Bx"),
        ("усі сигнали без відбору демо, 2021+", A, "B"),
        ("усі сигнали без відбору демо, OOS", A & OOS, "B"),
        *([] if wide else [("≥ $1M + відбір демо, 2021+", W, "B")]),
        (f"топ-{top_n} і {fl} + відбір демо (список демо), 2021+", P100, "B"),
        (f"топ-{top_n} і {fl}, IS", P100 & IS, "B"),
        (f"**топ-{top_n} і {fl}, OOS{vtag}**", P100 & OOS, "B"),
        (f"топ-{top_n} і {fl}, OOS, Bx", P100 & OOS, "Bx"),
        *(
            [
                (f"топ-{top_n} і {fl}, без ліміту {cap} на свічку (описово), 2021+", P100_nocap, "B"),
                (f"топ-{top_n} і {fl}, без ліміту {cap} на свічку (описово), OOS", P100_nocap & OOS, "B"),
            ]
            if cap
            else []
        ),
    ]
    for lab, m, k in rows:
        emit(f"| {lab} " + fmt(ci(T, m, k)))
    emit(
        f"угод топ-{top_n} за роками: "
        + ", ".join(f"{y} {int((P100 & (T['year'] == y)).sum())}" for y in sorted(set(T["year"][P100].tolist())))
    )
    if cap:
        bars = Counter(T["t_in"][P100_nocap].tolist())
        emit(
            f"ліміт {cap} на свічку: угод без ліміту {int(P100_nocap.sum())}, з лімітом {int(P100.sum())}; свічок з > {cap} угодами "
            f"{sum(1 for v in bars.values() if v > cap)} з {len(bars)}, максимум {max(bars.values(), default=0)} на свічку"
        )
    ex = Counter(T["B_k"][V])
    emit(
        f"виходи ({'вибірка вердикту' if wide else 'основна'}): {dict(ex)}; середні витрати {np.nanmean(T['cost'][V]):.3f}R; частка R>0 {np.mean(T['B'][V] > 0):.1%}"
    )
    if wide and not lim:
        # the per-bar cap does not apply in the joint chain (registration), so compare with the uncapped own chain
        joint_chain(a, out, T, P100_nocap, qv)

    cells: list[tuple[str, str, np.ndarray, np.ndarray]] = []  # (table, label, sample mask, base)
    for y in sorted(set(T["year"][V].tolist())):
        cells.append(("рік", str(y), V & (T["year"] == y), V))
    if len(set(T["tf"][V].tolist())) > 1:  # (with --tf one timeframe: no empty cell in Holm)
        for tf in SETUPS[a.setup][1]:
            cells.append(("таймфрейм", tf, V & (T["tf"] == tf), V))
    if len(set(T["side"][V].tolist())) > 1:
        for sd in ("long", "short"):
            cells.append(("бік", sd, V & (T["side"] == sd), V))
    glab = (
        f"оборот (топ-{LIMIT_WIDE_TOP_N} і ≥ $1M + відбір демо, власний ланцюг; не вердикт)"
        if lim
        else f"оборот (топ-{top_n} і {fl}, вибірка вердикту)"
        if wide
        else "оборот (≥ $1M, власний ланцюг)"
    )
    for g in (sp.TOP30, sp.GE5M, sp.M1TO5):
        cells.append((glab, g, G & (T["grp"] == g), G))
    for st in (*cr.STATES, "none"):
        cells.append(("режим BTC", st, V & (T["regime"] == st), V))
    res = [ci(T, m) for _, _, m, _ in cells]
    ps = [r["p"] if np.isfinite(r["p"]) else 1.0 for r in res]
    adj = holm(ps)
    cur = None
    for (tab, lab, m, _), r, h in zip(cells, res, adj, strict=True):
        if tab != cur:
            emit(
                f"\n## За: {tab} (Холм на всі {len(cells)} клітинок)\n| група | n | тижнів | E[R] net | 95 % CI | p | p Холма | OOS: n | OOS E[R] [CI] |\n|---|---|---|---|---|---|---|---|---|"
            )
            cur = tab
        o = ci(T, m & OOS)
        os_ = f"{int(o['n'])} | {o['m']:+.3f} [{o['lo']:+.3f}; {o['hi']:+.3f}]" if o["n"] >= 2 else f"{int(o['n'])} | —"
        emit(f"| {lab} | {int(r['n'])} | {int(r['k'])} " + fmt(r)[fmt(r).index("|", 2) :] + f" {h:.3f} | {os_} |")
    for lab, mm in (("2021+", G), ("OOS", G & OOS)):
        d = sp.diff_ci(T["B"][mm], (T["grp"] == sp.M1TO5)[mm], (T["grp"] != sp.M1TO5)[mm], T["week"][mm], n_boot=N_BOOT, seed=SEED)
        emit(f"контраст 1to5m − (top30+ge5m), {lab}: {d['d']:+.3f} [{d['lo']:+.3f}; {d['hi']:+.3f}] p {d['p']:.3f}")
    if wide:  # registered for S38-S40 (descriptive): the contrast is mixed with time, so also within each year
        emit("контраст за роками (описово; 1to5m n / решта n): " + "; ".join(contrast_by_year(T, G)))

    emit(f"\n## Стрес ({'вибірка вердикту' if wide else 'основна вибірка'})\n" + hdr)
    late_ok = np.isfinite(T["L"])
    # Review remark 7 (docs/reviews/2026-10-30-passport-s12v7-s28.md): the late-entry rows below belong to the close
    # entry (no late entry = the price already beyond the stop / target or a hole). For a resting limit the late
    # placement is not placed or not filled instead, so its rows are only in the limit block (``limit_section``).
    late_lines: tuple[tuple[str, np.ndarray, str], ...] = (
        ()
        if lim
        else (
            ("вхід на свічку пізніше, 2021+", V, "L"),
            ("вхід на свічку пізніше, OOS", V & OOS, "L"),
            ("пізніше + ×1.5, OOS", V & OOS, "L15"),
            ("база OOS на тих самих угодах, що мають пізній вхід (відібрано за майбутнім)", V & OOS & late_ok, "B"),
        )
    )
    for lab, m, k in (
        ("база OOS", V & OOS, "B"),
        ("витрати ×1.5, 2021+", V, "B15"),
        ("витрати ×1.5, OOS", V & OOS, "B15"),
        *late_lines,
    ):
        emit(f"| {lab} " + fmt(ci(T, m, k)))
    if lim:
        emit("пізніше розміщення лімітки (не поставлено / не заповнено) — у блоці «Лімітка: модель «дотик» і пізніше розміщення»")
    else:
        emit(f"без пізнього входу (ціна вже за стопом / ціллю або дірка): {int((V & ~late_ok).sum())} з {int(V.sum())}")
        emit(
            "\nціна запізнення (пізній − база), два знаменники; демо теж не входить, якщо ціна вже за стопом / ціллю\n"
            "| період | на сигнал (немає пізнього входу = 0R) | на угоду, де пізній вхід можливий | база пропущених: n, E[R] |\n|---|---|---|---|"
        )
        late0 = np.where(late_ok, T["L"], 0.0) - T["B"]
        late_rows = [("2021+", P), ("OOS", P & OOS), (f"топ-{top_n}, 2021+", P100), (f"топ-{top_n}, OOS", P100 & OOS)]
        for lab, m in late_rows[2:] if wide else late_rows:
            r = sp.mean_ci(late0[m], T["week"][m], n_boot=N_BOOT, seed=SEED)
            s = sp.mean_ci((T["L"] - T["B"])[m & late_ok], T["week"][m & late_ok], n_boot=N_BOOT, seed=SEED)
            nb = ci(T, m & ~late_ok)
            emit(
                f"| {lab} | {r['m']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}] | {s['m']:+.3f} [{s['lo']:+.3f}; {s['hi']:+.3f}] | {int(nb['n'])}, {nb['m']:+.3f} |"
            )

    emit("\n## Смуга R для n = 30 (5-й / 50-й / 95-й перцентиль середнього R)")
    for lab, m in (
        (f"OOS топ-{top_n} (список демо, еталон пункту 6)", P100 & OOS),
        (f"2021+ топ-{top_n}", P100),
        (f"OOS топ-{top_n}, Bx", P100 & OOS),
        (f"OOS {pl}", P & OOS),
        (f"2021+ {pl}", P),
        (f"OOS {pl}, Bx", P & OOS),
    ):
        k = "Bx" if "Bx" in lab else "B"
        idx = np.flatnonzero(m)
        idx = idx[np.argsort(T["t_in"][idx], kind="stable")]
        v = T[k][idx]
        v = v[np.isfinite(v)]
        b = sp.r_band(v, 30, reps=N_BOOT, seed=SEED)
        emit(
            f"{lab}: i.i.d. {b['iid_p5']:+.3f} / {b['iid_p50']:+.3f} / {b['iid_p95']:+.3f}; ковзні {int(b['windows'])} вікон {b['roll_p5']:+.3f} / {b['roll_p50']:+.3f} / {b['roll_p95']:+.3f}"
        )

    head = (
        "Описово (зареєстровано до прогону, без Холма): за балом демо, вибірка вердикту"
        if wide
        else "Розвідувально (не зареєстровано, додано після перших результатів): за балом демо, основна вибірка"
    )
    emit(f"\n## {head}\n| бал | n | E[R] net | 95 % CI | p | OOS: n | OOS E[R] [CI] |\n|---|---|---|---|---|---|---|")
    for sc in sorted(set(T["score"][V].tolist())):
        m = V & (T["score"] == sc)
        o = ci(T, m & OOS)
        emit(f"| {sc} " + fmt(ci(T, m)) + f" {int(o['n'])} | {o['m']:+.3f} [{o['lo']:+.3f}; {o['hi']:+.3f}] |")

    emit("\n## Без найбільших внесків")
    for lab, m in (("OOS", V & OOS), ("2021+", V)):
        v = T["B"][m]
        top = np.argsort(-v)[:5]
        keep = np.ones(v.size, dtype=bool)
        keep[top] = False
        r = sp.mean_ci(v[keep], T["week"][m][keep], n_boot=N_BOOT, seed=SEED)
        emit(f"{lab} без 5 найкращих угод: {r['m']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}]")
    per_asset(T, V, IS, OOS)
    coverage(a, qv, top_days, T, V, floor, top_n)
    if a.setup in SHORT_FAMILY:
        short_family(out, qv)


def limit_section(
    a: argparse.Namespace,
    out: Path,
    T: dict[str, np.ndarray],
    M: dict[str, np.ndarray],
    IS: np.ndarray,
    OOS: np.ndarray,
    period: np.ndarray,
    demo: np.ndarray,
    qv: dict[str, list[tuple[int, float]]],
) -> None:
    """Registered rows of a resting-limit setup (S12 v7, S28) on the placements (chains already applied): fill rate,
    R per placement (unfilled = 0R), the touch fill model, the late placement, the placebo and the known number."""
    F = T["filled"]
    r0 = np.nan_to_num(T["B"], nan=0.0)
    emit("\n## Лімітка: заповнення і R на розміщення (незаповнена = 0R; ланцюг тримає монету від розміщення)")
    emit(
        "| вибірка | період | розміщень | заповнено | частка | E[R] на заповнену [CI] | E[R] на розміщення [CI] | R за рік |\n|---|---|---|---|---|---|---|---|"
    )
    names = {
        "P100": "топ-100 і ≥ $5M + відбір (вердикт)",
        "P": "усі ≥ $5M + відбір",
        "G": "топ-250 і ≥ $1M + відбір",
        "A": "усі сигнали ≥ $5M",
    }
    for key, lab in names.items():
        for plab, pm in (("2021+", np.ones_like(F)), ("IS", IS), ("OOS", OOS)):
            m = M[key] & pm
            n, nf = int(m.sum()), int((m & F).sum())
            if n == 0:
                continue
            rf = ci(T, m & F)
            rp = sp.mean_ci(r0[m], T["week"][m], n_boot=N_BOOT, seed=SEED)
            yrs = (int(T["t_in"][m].max()) - int(T["t_in"][m].min())) / (365.25 * DAY_MS) or 1.0
            emit(
                f"| {lab} | {plab} | {n} | {nf} | {nf / n:.1%} | {rf['m']:+.3f} [{rf['lo']:+.3f}; {rf['hi']:+.3f}] | "
                f"{rp['m']:+.3f} [{rp['lo']:+.3f}; {rp['hi']:+.3f}] | {r0[m].sum() / yrs:+.1f} |"
            )
    V = M["P100"]
    emit("\n## Лімітка: модель «дотик» і пізніше розміщення (вибірка вердикту, розміщення ланцюга основної моделі)")
    hdr = "| рядок | n | E[R] net | 95 % CI | p |\n|---|---|---|---|---|"
    emit(hdr)
    tf_ = np.isfinite(T["T"])
    lr = np.nan_to_num(T["L"], nan=0.0)
    for lab, m, v in (
        ("«дотик» (лоу ≤ ліміту), заповнені, 2021+", V & tf_, T["T"]),
        ("«дотик», заповнені, OOS", V & OOS & tf_, T["T"]),
        ("«дотик», на розміщення, OOS", V & OOS, np.nan_to_num(T["T"], nan=0.0)),
        ("пізніше на свічку, заповнені, OOS", V & OOS & np.isfinite(T["L"]), T["L"]),
        ("пізніше на свічку, на розміщення (не поставлено / не заповнено = 0R), OOS", V & OOS, lr),
        ("база на розміщення, OOS", V & OOS, r0),
        ("пізніше + витрати ×1.5, заповнені, OOS", V & OOS & np.isfinite(T["L15"]), T["L15"]),
    ):
        emit(f"| {lab} " + fmt(sp.mean_ci(v[m], T["week"][m], n_boot=N_BOOT, seed=SEED)))
    emit(
        f"частка заповнення OOS: тут {(V & OOS & F).sum() / max(1, (V & OOS).sum()):.1%}, «дотик» {(V & OOS & tf_).sum() / max(1, (V & OOS).sum()):.1%}; "
        f"пізнє розміщення не поставлено (ціна вже на рівні / дірка) {int((V & OOS & ~T['late_placed']).sum())} з {int((V & OOS).sum())}"
    )
    if a.setup in ("S17", "S23"):
        emit("\n## Утримання `demo_trader.MAX_HOLD_MS` замість власного (описово, не вердикт; підказка для підключення)")
        emit(hdr)
        F = T["filled"]
        for lab, m, k in (
            ("власне утримання, заповнені, 2021+", V & F, "B"),
            ("MAX_HOLD_MS (7 діб 4h / 30 діб 1d), заповнені, 2021+", V & F, "D"),
            ("власне утримання, заповнені, OOS", V & F & OOS, "B"),
            ("MAX_HOLD_MS, заповнені, OOS", V & F & OOS, "D"),
        ):
            emit(f"| {lab} " + fmt(ci(T, m, k)))
        d = (T["D"] - T["B"])[V & F]
        r = sp.mean_ci(d[np.isfinite(d)], T["week"][V & F][np.isfinite(d)], n_boot=N_BOOT, seed=SEED)
        emit(f"MAX_HOLD_MS − власне, попарно, 2021+: {r['m']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}]")
        emit("\n## Угод і сума R за рік (вибірка вердикту; заповнені / розміщення, R на розміщення = сума R заповнених)")
        emit("| рік | розміщень | заповнено | сума R | сума R ×1.5 | E[R] на заповнену |\n|---|---|---|---|---|---|")
        for y in sorted(set(T["year"][V].tolist())):
            m = V & (T["year"] == y)
            mf = m & F
            emit(
                f"| {y} | {int(m.sum())} | {int(mf.sum())} | {np.nansum(T['B'][mf]):+.1f} | {np.nansum(T['B15'][mf]):+.1f} | "
                f"{(np.nanmean(T['B'][mf]) if mf.any() else math.nan):+.3f} |"
            )

    emit("\n## Плацебо: випадкова лімітка тієї самої монети, боку й геометрії (зареєстровано)")
    Q = load_rows(out, "placebo")
    q_top = in_prev_day_top(Q, top_n_days(qv, TOP_N))
    QP = chain(Q, (Q["t_in"] >= PASSPORT_MS) & q_top) & Q["filled"]
    q_place = chain(Q, (Q["t_in"] >= PASSPORT_MS) & q_top)
    q_oos = Q["t_in"] >= OOS_MS
    emit(
        "| період | сетап: n, E[R] | плацебо: n, E[R] | сетап − плацебо [95 % CI] | p | заповнення сетап / плацебо |\n|---|---|---|---|---|---|"
    )
    for lab, mt, mq in (("2021+", V & F, QP), ("OOS", V & F & OOS, QP & q_oos)):
        vals = np.concatenate([T["B"][mt], Q["B"][mq]])
        wk = np.concatenate([T["week"][mt], Q["week"][mq]])
        ga = np.concatenate([np.ones(int(mt.sum()), bool), np.zeros(int(mq.sum()), bool)])
        d = sp.diff_ci(vals, ga, ~ga, wk, n_boot=N_BOOT, seed=SEED)
        pm_t = V & (OOS if lab == "OOS" else np.ones_like(F))
        pm_q = q_place & (q_oos if lab == "OOS" else np.ones_like(q_oos))
        emit(
            f"| {lab} | {int(mt.sum())}, {np.mean(T['B'][mt]):+.3f} | {int(mq.sum())}, {np.mean(Q['B'][mq]):+.3f} | "
            f"{d['d']:+.3f} [{d['lo']:+.3f}; {d['hi']:+.3f}] | {d['p']:.3f} | "
            f"{(pm_t & F).sum() / max(1, pm_t.sum()):.1%} / {(pm_q & Q['filled']).sum() / max(1, pm_q.sum()):.1%} |"
        )

    emit("\n## Відоме число")
    guard = (T["rr"] >= MIN_RR) & (T["stop_pct"] <= MAX_STOP) & T["top30"]
    if a.setup == "S12":  # 2026-10-26-prelimit-entries lim_mid: touch, top-30, guard, chain per symbol + side, busy from fills
        idx = np.flatnonzero(guard)
        idx = idx[np.lexsort((T["order"][idx], T["t_in"][idx]))]
        busy: dict[tuple[str, str], int] = {}
        keep = []
        for k in idx:
            key = (str(T["sym"][k]), str(T["side"][k]))
            if int(T["t_in"][k]) < busy.get(key, -1):
                continue
            if np.isfinite(T["T"][k]):
                keep.append(k)
                busy[key] = int(T["T_x"][k])
        kk = np.zeros(F.size, bool)
        kk[np.array(keep, dtype=np.int64)] = True
        r = sp.mean_ci(T["T"][kk], T["t_in"][kk] // DAY_MS, n_boot=N_BOOT, seed=SEED)
        emit(
            f"звірка з 2026-10-26-prelimit-entries (S12 lim_mid, «дотик», топ-30, guard, з 2020-02; там n 20 284, +0.185 [+0.155; +0.214], OOS +0.150): "
            f"тут n {int(r['n'])} ({int(r['n']) - 20284:+d}, {r['n'] / 20284 - 1:+.1%}), {r['m']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}], OOS {np.mean(T['T'][kk & OOS]):+.3f}"
        )
    elif a.setup == "S17":  # step 5 (2026-10-17-course-step5-s17): top-30, IS, every event, touch, no BE, 20 bars
        ref = {"1d": (1026, -0.007), "4h": (7386, 0.036)}
        for tf in sorted(set(T["tf"].tolist())):
            m = T["top30"] & IS & (T["tf"] == tf) & np.isfinite(T["K"])
            r = sp.mean_ci(T["K"][m], T["week"][m], n_boot=N_BOOT, seed=SEED)
            n0, m0 = ref[tf]
            emit(
                f"звірка з кроком 5 (S17 {tf}, топ-30, IS, усі події без ланцюга, «дотик», без беззбитку; там n {n0}, {m0:+.3f}R): "
                f"тут n {int(r['n'])} ({int(r['n']) - n0:+d}, {r['n'] / n0 - 1:+.1%}), {r['m']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}] "
                f"({r['m'] - m0:+.3f}R; межа реєстрації n ± 5 %, E[R] ± 0.03R)"
            )
    elif a.setup == "S23":
        known_s23(a, T)
    else:  # step 4 row B (2026-10-17-course-step4-fvg-ob): 4h IS 4 137 trades, own exits (R not compared)
        m = T["top30"] & IS & np.isfinite(T["T"])
        emit(
            f"звірка з кроком 4, рядок B (S28 4h IS, там 4 137 угод з власними виходами; R не порівнюється): заповнених «дотиком» кандидатів топ-30 IS "
            f"тут {int(m.sum())} ({int(m.sum()) - 4137:+d}, {m.sum() / 4137 - 1:+.1%}), без ланцюга; з guard {int((m & guard).sum())}"
        )

    emit("\n## Смуга R на 30 розміщень (незаповнені = 0R; демо рахує й «missed»)")
    for lab, m in (("OOS топ-100 (вердикт)", V & OOS), ("2021+ топ-100", V)):
        idx = np.flatnonzero(m)
        idx = idx[np.argsort(T["t_in"][idx], kind="stable")]
        b = sp.r_band(r0[idx], 30, reps=N_BOOT, seed=SEED)
        emit(
            f"{lab}: i.i.d. {b['iid_p5']:+.3f} / {b['iid_p50']:+.3f} / {b['iid_p95']:+.3f}; ковзні {int(b['windows'])} вікон {b['roll_p5']:+.3f} / {b['roll_p50']:+.3f} / {b['roll_p95']:+.3f}"
        )
    emit(f"rss {rss_mb():.0f} MB")


def known_s23(a: argparse.Namespace, T: dict[str, np.ndarray]) -> None:
    """Known number of S23 (step 2 of 2026-10-12-sfp-oos-and-15m, registration «… S17 і S23»): the backtest's own
    signal set, ``walk_forward_sfp`` with its one-position skip on the 20 coins of ``verify_lectures.TOP20`` from
    2024-10-12, matched to the ``s23_all`` candidates by coin, bar and side; R per signal from the touch, no-BE field
    with the backtest's fee (0.1 % round trip over the risk in percent), an unfilled limit = 0R."""
    from verify_lectures import TOP20

    from tradedesk.analytics.sfp_backtest import walk_forward_sfp

    key = {(str(s), int(t), str(d)): k for k, (s, t, d) in enumerate(zip(T["sym"], T["t_in"], T["side"], strict=True))}
    hit: list[int] = []
    miss: Counter[str] = Counter()
    n_sig = 0
    for base in TOP20:
        sym = f"{base}USDT"
        h1, _ = load_1h_qv(a.s1_cache, a.data_dir, sym)
        if len(h1) == 0:
            miss["no_1h"] += 1
            continue
        b4 = resample(h1, 4)
        i0 = int(np.searchsorted(b4.t, KNOWN_FROM_MS))
        rows = ss._ohlc_rows(b4, i0, len(b4))
        sigs = walk_forward_sfp(rows, window=ss.S23_WINDOW, max_hold_bars=60, fee_pct=0.1, max_width_pct=15.0, sweep_wick_ratio=0.5)
        for g in sigs:
            t_in = int(g["ts"]) + 4 * HOUR_MS
            if t_in >= END["4h"]:
                continue
            n_sig += 1
            k = key.get((sym, t_in, str(g["direction"])))
            if k is None:
                miss["unmatched"] += 1
            else:
                hit.append(k)
    idx = np.array(hit, dtype=np.int64)
    g = T["K"][idx] + T["K_c"][idx]  # gross R (our risk unit)
    r = np.where(np.isfinite(g), g - 0.001 / T["stop_pct"][idx], 0.0)
    rs = sp.mean_ci(r, T["week"][idx], n_boot=N_BOOT, seed=SEED)
    filled = int(np.isfinite(g).sum())
    rf = sp.mean_ci(r[np.isfinite(g)], T["week"][idx][np.isfinite(g)], n_boot=N_BOOT, seed=SEED)
    emit(
        f"звірка з кроком 2 SFP (S23 4h, TOP20, з 2024-10-12, пропуск поки відкрита 4h-угода; там 850 сигналів, −0.142R на сигнал "
        f"[−0.24; −0.05], 444 угоди, −0.272R на угоду): тут сигналів {n_sig} (зіставлено {idx.size}, {dict(miss)}), "
        f"на сигнал {rs['m']:+.3f} [{rs['lo']:+.3f}; {rs['hi']:+.3f}] ({rs['m'] + 0.142:+.3f}R), заповнено {filled}, "
        f"на угоду {rf['m']:+.3f} [{rf['lo']:+.3f}; {rf['hi']:+.3f}]; межа реєстрації n 790 … 870, ± 0.05R"
    )
    mine = sp.mean_ci(np.nan_to_num(T["K"][idx], nan=0.0), T["week"][idx], n_boot=N_BOOT, seed=SEED)
    emit(f"  те саме з нашими витратами Fees() (на сигнал): {mine['m']:+.3f} [{mine['lo']:+.3f}; {mine['hi']:+.3f}]")


def contrast_by_year(T: dict[str, np.ndarray], G: np.ndarray) -> list[str]:
    out = []
    for y in sorted(set(T["year"][G].tolist())):
        mm = G & (T["year"] == y)
        g1 = (T["grp"] == sp.M1TO5)[mm]
        if g1.sum() < 2 or (~g1).sum() < 2:
            out.append(f"{y} — ({int(g1.sum())} / {int((~g1).sum())})")
            continue
        d = sp.diff_ci(T["B"][mm], g1, ~g1, T["week"][mm], n_boot=N_BOOT, seed=SEED)
        out.append(f"{y} {d['d']:+.3f} [{d['lo']:+.3f}; {d['hi']:+.3f}] ({int(g1.sum())} / {int((~g1).sum())})")
    return out


def short_family(out: Path, qv: dict[str, list[tuple[int, float]]]) -> None:
    """Registered for S38-S40 (descriptive): the turnover contrast of the three verdict samples pooled (week clusters),
    and Holm on the six registered contrasts (3 setups x 2021+ / OOS)."""
    if any(not (out.parent / st / "cands").is_dir() for st in SHORT_FAMILY):
        emit("\nS38 + S39 + S40 разом: немає кандидатів усіх трьох — рядок пропущено")
        return
    days = top_n_days(qv, WIDE_TOP_N)
    cols: dict[str, list[np.ndarray]] = defaultdict(list)
    ps, labs = [], []
    emit("\n## Оборот $1–5M: S38 + S39 + S40 (описово; вибірки вердикту)")
    for st in SHORT_FAMILY:
        U = load_rows(out.parent / st)
        V = chain(U, demo_list(U, st, days), BAR_CAP.get(st))
        for lab, mm in (("2021+", V), ("OOS", V & (U["t_in"] >= OOS_MS))):
            d = sp.diff_ci(U["B"][mm], (U["grp"] == sp.M1TO5)[mm], (U["grp"] != sp.M1TO5)[mm], U["week"][mm], n_boot=N_BOOT, seed=SEED)
            ps.append(d["p"] if np.isfinite(d["p"]) else 1.0)
            labs.append(f"{st} {lab} {d['d']:+.3f} [{d['lo']:+.3f}; {d['hi']:+.3f}] p {d['p']:.3f}")
        for k in ("B", "week", "t_in", "year"):
            cols[k].append(U[k][V])
        cols["g1"].append(U["grp"][V] == sp.M1TO5)
        emit(
            f"{st}: угод групи 1to5m за роками "
            + ", ".join(f"{y} {int(((U['year'] == y) & V & (U['grp'] == sp.M1TO5)).sum())}" for y in sorted(set(U["year"][V].tolist())))
        )
    for lab, h in zip(labs, holm(ps), strict=True):
        emit(f"контраст {lab}; p Холма (6 контрастів) {h:.3f}")
    J = {k: np.concatenate(v) for k, v in cols.items()}
    for lab, mm in (("2021+", np.ones(J["B"].size, dtype=bool)), ("OOS", J["t_in"] >= OOS_MS)):
        d = sp.diff_ci(J["B"][mm], J["g1"][mm], ~J["g1"][mm], J["week"][mm], n_boot=N_BOOT, seed=SEED)
        emit(
            f"разом, {lab}: 1to5m n {int(J['g1'][mm].sum())}, решта n {int((~J['g1'][mm]).sum())}; контраст {d['d']:+.3f} [{d['lo']:+.3f}; {d['hi']:+.3f}] p {d['p']:.3f}"
        )
    emit(
        "разом, контраст за роками: "
        + "; ".join(
            contrast_by_year(
                {"B": J["B"], "grp": np.where(J["g1"], sp.M1TO5, "x"), "week": J["week"], "year": J["year"]},
                np.ones(J["B"].size, dtype=bool),
            )
        )
    )


def top_n_days(qv: dict[str, list[tuple[int, float]]], n: int) -> dict[int, set[str]]:
    """Per UTC day (open ms): the n eligible coins with the largest Binance daily quote volume that day."""
    by_day: dict[int, list[tuple[float, str]]] = defaultdict(list)
    for s, rows in qv.items():
        for t, v in rows:
            by_day[int(t)].append((v, s))
    return {t: {s for _, s in sorted(xs, reverse=True)[:n]} for t, xs in by_day.items()}


def in_prev_day_top(T: dict[str, np.ndarray], top_days: dict[int, set[str]]) -> np.ndarray:
    """Causal: the coin was in the top list of the last UTC day closed at or before t_in."""
    prev = (T["t_in"] // DAY_MS) * DAY_MS - DAY_MS
    return np.array([str(s) in top_days.get(int(d), ()) for d, s in zip(prev, T["sym"], strict=True)])


def demo_list(T: dict[str, np.ndarray], setup: str, top_days: dict[int, set[str]]) -> np.ndarray:
    """Candidates of ``setup`` the demo would take before the occupancy chain: 2021+, its demo list (top-N by the last
    closed day's Binance volume and the 24h turnover floor) and the demo's selection."""
    floor = SETUPS[setup][2]
    return (
        (T["t_in"] >= PASSPORT_MS)
        & (T["rr"] >= MIN_RR)
        & (T["stop_pct"] <= MAX_STOP)
        & (T["score"] >= MIN_SCORE)
        & (np.nan_to_num(T["turn"], nan=-1) >= floor)
        & in_prev_day_top(T, top_days)
    )


def joint_chain(
    a: argparse.Namespace, out: Path, T: dict[str, np.ndarray], own: np.ndarray, qv: dict[str, list[tuple[int, float]]]
) -> None:
    """Descriptive row (registered for S14 / S16 before their run): one open trade per coin shared by S14, S16 and S3,
    as the demo's guard refuses a second position in a coin; each setup on its own demo list with the demo's
    selection; ties at the same entry time: higher score first, then the demo_trader.SETUPS order, 4h before 1d, long
    before short. For S38-S40 the chain has all six setups with a passport (registration «… S38, S39 і S40»; S38's
    per-bar cap is not applied here, so `own` is its uncapped chain; S38 then occupies more coins than the capped demo
    would, and the crowding-out of S39 / S40 is an upper bound). Other demo setups (S12, S28) and the daily slots are not modelled."""
    order = JOINT.get(a.setup, DEMO_ORDER)
    name = " + ".join(order)
    for st in order:
        d = out.parent / st / "cands"
        if not d.is_dir() or not any(d.glob("*.json")):
            emit(f"\nспільний ланцюг {name}: немає кандидатів {st} — рядок пропущено (порахувати після прогону всіх)")
            return
    tops: dict[int, dict[int, set[str]]] = {}
    cols: dict[str, list[np.ndarray]] = defaultdict(list)
    for r, st in enumerate(order):
        U = T if st == a.setup else load_rows(out.parent / st)
        n = top_n_of(st)
        if n not in tops:
            tops[n] = top_n_days(qv, n)
        m = demo_list(U, st, tops[n])
        cols["sym"].append(U["sym"][m].astype(str))
        cols["t_in"].append(U["t_in"][m])
        cols["B_x"].append(U["B_x"][m])
        cols["order"].append(-U["score"][m] * 100 + r * 10 + U["order"][m])
        cols["B"].append(U["B"][m])
        cols["week"].append(U["week"][m])
        cols["mine"].append(np.full(int(m.sum()), st == a.setup))
    J = {k: np.concatenate(v) for k, v in cols.items()}
    keep = sp.busy_chain(J["sym"].tolist(), J["t_in"], J["B_x"], J["order"])
    mine = J["mine"] & keep
    oos = J["t_in"] >= OOS_MS
    own_lab = "у власному ланцюгі без ліміту на свічку" if BAR_CAP.get(a.setup) else "у власному ланцюгі"
    emit(f"\nспільний ланцюг {name} (описово): угод {a.setup} {int(mine.sum())} з {int(own.sum())} {own_lab}")
    for lab, m in (("2021+", mine), ("OOS", mine & oos)):
        r = sp.mean_ci(J["B"][m], J["week"][m], n_boot=N_BOOT, seed=SEED)
        emit(f"| {a.setup}, спільний ланцюг, {lab} | {int(r['n'])} | {r['m']:+.3f} | [{r['lo']:+.3f}; {r['hi']:+.3f}] | {r['p']:.3f} |")


def coverage(
    a: argparse.Namespace,
    qv: dict[str, list[tuple[int, float]]],
    top_days: dict[int, set[str]],
    T: dict[str, np.ndarray],
    P: np.ndarray,
    floor: float = 5e6,
    top_n: int = TOP_N,
) -> None:
    """Share of universe coin-days with >= 20 cached 1h bars, by period, for >= floor and for top-N & >= floor."""
    fl = f"≥ ${floor / 1e6:g}M"
    have = {p.name for p in (a.s1_cache / "k1h").iterdir() if p.is_dir()}
    cnt: Counter[str] = Counter()
    miss: Counter[str] = Counter()
    for s, rows in qv.items():
        hours: dict[int, int] = {}
        if s in have:
            h1, _ = load_1h_qv(a.s1_cache, a.data_dir, s)
            u, c = np.unique(h1.t // DAY_MS, return_counts=True)
            hours = dict(zip(u.tolist(), c.tolist(), strict=True))
        for t, v in rows:
            if not (PASSPORT_MS <= t < END["4h"]) or v < floor:
                continue
            ok = hours.get(int(t) // DAY_MS, 0) >= 20
            per = "OOS" if t >= OOS_MS else "IS"
            top = s in top_days.get(int(t), ())
            uni = ["ge"] + (["top"] if top else []) + (["top_1to5m"] if top and v < 5e6 else [])
            for u_ in uni:
                for k in (per, s1.month_of(int(t))[:4]):
                    cnt[f"{u_}:{k}:all"] += 1
                    cnt[f"{u_}:{k}:ok"] += int(ok)
            if not ok and per == "OOS" and top:
                miss[s] += 1
    emit(f"\n## Покриття 1h-кешем (монето-дні з ≥ 20 годинними свічками, 2021-01..2026-09); rss {rss_mb():.0f} MB")
    ks = ("IS", "OOS", "2021", "2022", "2023", "2024", "2025", "2026")
    emit("| всесвіт | " + " | ".join(ks) + " |\n|---|" + "---|" * len(ks))
    unis = [("ge", f"усі {fl} (зареєстрована)"), ("top", f"топ-{top_n} і {fl} (список демо)")]
    if floor < 5e6:
        unis.append(("top_1to5m", f"топ-{top_n}, денний оборот $1–5M"))
    for u_, lab in unis:
        cells = [f"{cnt[f'{u_}:{k}:ok'] / max(1, cnt[f'{u_}:{k}:all']):.1%} ({cnt[f'{u_}:{k}:all']})" for k in ks]
        emit(f"| {lab} | " + " | ".join(cells) + " |")
    emit(f"монети топ-{top_n} OOS без покриття (днів): " + ", ".join(f"{s} {n}" for s, n in miss.most_common(15)))
    for old, new in (("RNDRUSDT", "RENDERUSDT"), ("MATICUSDT", "POLUSDT")):
        emit(f"перейменування {old} -> {new}: 1h-кеш {old} {'є' if old in have else 'нема'}, {new} {'є' if new in have else 'нема'}")
    OOS = T["t_in"] >= OOS_MS
    is_syms = set(T["sym"][P & ~OOS].tolist())
    new = np.array([str(s) not in is_syms for s in T["sym"]])
    emit("OOS, монети з угодами в IS " + fmt(ci(T, P & OOS & ~new)) + " ; монети без угод в IS (нові) " + fmt(ci(T, P & OOS & new)))


def per_asset(T: dict[str, np.ndarray], P: np.ndarray, IS: np.ndarray, OOS: np.ndarray) -> None:
    emit("\n## За монетами (5a)")
    syms = sorted(set(T["sym"][P].tolist()))
    means, ses, rows = [], [], []
    for s in syms:
        v = T["B"][P & (T["sym"] == s)]
        if v.size >= 10:
            means.append(v.mean())
            ses.append(v.std(ddof=1) / math.sqrt(v.size))
            rows.append((s, v.size, v.mean()))
    q, df, i2 = cochran_q(np.array(means), np.array(ses))
    emit(f"монет з ≥ 10 угод: {len(rows)}; Cochran Q {q:.1f}, df {df}, p {chi2_sf(q, df):.3f}, I² {i2:.2f}")
    rows.sort(key=lambda x: x[2])
    emit("найгірші 5: " + ", ".join(f"{s} n={n} {m:+.2f}" for s, n, m in rows[:5]))
    emit("найкращі 5: " + ", ".join(f"{s} n={n} {m:+.2f}" for s, n, m in rows[-5:]))
    is_rank = []
    for s in syms:
        v = T["B"][P & IS & (T["sym"] == s)]
        if v.size >= 10:
            is_rank.append((v.mean(), s))
    is_rank.sort(reverse=True)
    top = {s for _, s in is_rank[:10]}
    ranked = {s for _, s in is_rank}
    mt = P & OOS & np.isin(T["sym"], list(top))
    mr = P & OOS & np.isin(T["sym"], list(ranked - top))
    d = sp.diff_ci(T["B"], mt, mr, T["week"], n_boot=N_BOOT, seed=SEED)
    emit(f"топ-10 монет за IS (з ≥ 10 угод IS, з {len(ranked)}): {sorted(top)}")
    emit(
        "OOS топ-10 "
        + fmt(ci(T, mt))
        + " ; решта "
        + fmt(ci(T, mr))
        + f" ; різниця {d['d']:+.3f} [{d['lo']:+.3f}; {d['hi']:+.3f}] p {d['p']:.3f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("setup", choices=sorted(SETUPS))
    ap.add_argument("--stage", choices=("sanity", "run", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--regime-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/cycle_regime")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--tf", choices=("4h", "1d"), default=None, help="report: one timeframe only, its own chain (S17)")
    a = ap.parse_args()
    out = a.cache_dir / a.setup
    out.mkdir(parents=True, exist_ok=True)
    syms = syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    OUT["tf"] = a.tf if a.stage == "report" else None
    sfx = f"_{a.tf}" if OUT["tf"] else ""
    with (out / f"{a.stage}{sfx}.txt").open("w") as fh:
        OUT["fh"] = fh
        emit(
            f"verify_setup_passport {a.setup} --stage {a.stage}{' --tf ' + a.tf if OUT['tf'] else ''} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC"
        )
        if a.stage == "sanity":
            stage_sanity(a, out, syms)
        elif a.stage == "run":
            stage_run(a, out, syms)
        else:
            stage_report(a, out)
        emit(f"peak rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
