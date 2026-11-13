"""Checkpoint 2026-11-30 report: the state of every forward test at a moment, next to its registered backtest band, with
one label «рано» / «узгоджується» / «ні» (docs/CHECKPOINT-2026-11-30.md, section «Критерій „узгоджується з бектестом“»).

Pure functions over rows already read from the collector DB (dicts with the tables' column names, JSON columns parsed);
no I/O, the moment is a parameter (``now_ms``, UTC ms) and nothing after it is used. Bands and rules are copied from the
CHECKPOINT section, frozen on 2026-11-02 before any forward result was opened (``scripts/verify_band_n.py`` reproduces
the demo and A v2 bands from the passport cache). «узгоджується» is not evidence of an edge: almost every reference
passport is itself ``inconclusive`` around 0R or below.

Embargoed forwards expose counters only: Polymarket v1 (verdict not before 2027-02-23 18:20 UTC) and v2-g (2027-03-07
04:20 UTC) copy a fixed whitelist of counts out of ``pm_gap_report.weekly_descriptive`` / ``weekly_v2g`` (no mean, CI,
SD, edge, P&L or Brier; no verdict function is ever called), and the condor P&L (part (b), T0 + 52 weeks) is not read at
all. Condor part (a) shows only the count of valid Fridays until its registered decision moment. The paper engine
``assistant_paper`` has no band in CHECKPOINT: counters and the job's state only (no P&L), labelled «лише лічильники».
So are the idea cards with status ``forward``, judged by plans tagged ``idea:<slug>``: plan counts per card, no R.
One section outside the table, ``s12_stop_rule``: the owner's S12 stop rule (Q-20261109-1) with n, mean R and the 95 %
CI clustered by signal day, for v9 and for the v4 entry (v4 + v9). Each demo row with fee data also gets a descriptive
note (item 341): mean ``fee_gap_r`` of ``demo_execution`` and the taker-entry share; it changes no n, mean or label.
Each demo row with n >= 30 notes the 95 % CI of its mean R clustered by the signal's UTC day (``_r_ci``, item 343), as
the CHECKPOINT rule registers it: descriptive only, the label stays the band's (below 30 the few days make it noise).

Carry criteria (e) and (ye) (item 365) come from what the engine already writes, without network: (e) universe purity
per entry from its candidate rows over the holding period (the registered collision metric, |median leg price gap| >
0.005), its entry cycle (the candidate's funding interval; the feed's exclusion journal, which cannot fire for an
entered symbol today) and its open ``side`` rows (the ``1000…`` multiplier of A's spot leg; the leg mids against the
developer's coarse 1.5x bound, outside the registration); TradFi is not checked independently; (ye) capacity per entry
from the open ``side`` rows (leg notional <= book depth within ±0.5 % of mid on every leg) and, for the Bybit leg only,
the largest 2x position of the nearest dated ladder snapshot before the entry (``docs/paper/ladder/``, registration
amendment (2): Binance's ladder is signed, so its leg is checked by depth only). The venue metadata at the checkpoint
that the registration names for (e) is network, so (e) checks the engine against its own records, not the venue.

What this module does not compute (the label says so in its notes): carry criterion (a) (re-run of the backtest code)
and B's liquidation part of (g) (1m mark prices, network); the demo's secondary metric ``r_demo_bt`` and its clustered
CI (``scripts/demo_vs_backtest.py``); the A v2 delisting cross-check.
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .assistant import PAPER_MAX_AGE_HOURS
from .carry_bench import band_check, realized_funding_apr
from .carry_report import cost_realism
from .carry_v2 import a_v2_shadow
from .catalyst_scalp import wilson_ci
from .condor_forward import EARLIEST_T0, ENTRY_CUTOFF_HOUR_UTC, condor_cost, cost_fridays, cost_report, friday_entry
from .demo_execution import trade_metrics
from .demo_pnl_check import pnl_mismatches
from .liquidation_flush import cluster_bootstrap_ci
from .pm_gap_report import T0_TS, T0_V2G_TS, verdict_ts, verdict_ts_v2g, weekly_descriptive, weekly_v2g
from .s12_demo_audit import PASSPORT_OF, PASSPORTS, V4_BAND_SLIDING_N100, traded_version
from .setup_signals import TF_MS
from .setup_versions import EPOCHS, _setup, applies, version_of
from .stuck_demo import stuck_trades

DAY_MS = 86_400_000
H8_MS = 8 * 3_600_000
EARLY, OK, NO = "рано", "узгоджується", "ні"
OUTSIDE = "поза скриптом"  # the registered one-time verdict is another script's job
COUNTS_ONLY = "лише лічильники"  # no band in CHECKPOINT: the forward is shown by its counters and state only
LABELS = (EARLY, OK, NO, OUTSIDE, COUNTS_ONLY)


def _num(x: object) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def ua_trades(n: int) -> str:
    """«N угода / угоди / угод»: the Ukrainian count form of «угода» (1, 21 → угода; 2-4, 22-24 → угоди; 0, 5-20 → угод)."""
    n10, n100 = abs(n) % 10, abs(n) % 100
    if n10 == 1 and n100 != 11:
        word = "угода"
    elif 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        word = "угоди"
    else:
        word = "угод"
    return f"{n} {word}"


def _ms(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp() * 1000)


def _row(key: str, group: str, name: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"key": key, "group": group, "name": name, "n": None, "metric": None, "metric_name": "", "band": None}
    base |= {"verdict": EARLY, "side": None, "embargo": False, "counters": {}, "criteria": [], "notes": []}
    return base | kw


# ----------------------------------------------------------------------------------------------------------- demo

Band = tuple[tuple[float, float], tuple[float, float]]  # (i.i.d. p5, p95), (rolling p5, p95) of the mean R of n trades


@dataclass(frozen=True)
class DemoSpec:
    setup: str
    name: str
    versions: frozenset[str] | None  # setup_versions tags of the current version; None = every version (S3)
    bands: Mapping[int, Band]  # n -> band (CHECKPOINT table and «Смуги n 50 / 100»)
    timeframe: str | None = None  # S17: the 1d chain only
    fill_share: float | None = None  # passport OOS fill share of a limit setup
    source: str = ""


MIN_DEMO = 30
S3_FINAL_N = 100  # Q-20261030-3: the decision to drop S3 only on 100 trades
S28_FLOOR_30 = -0.438  # Q-20261030-2: i.i.d. p5 of n 30, only on the check at the first 30 filled trades
OLD_BE_VERSIONS = frozenset({"v1", "v2"})  # break-even at +30 % ROE (before v3): S3 descriptive split only
# An ``orphaned`` trade (filled, its close never found) closed at its initial stop: P&L = -risk_usd, since the demo's
# risk already holds both taker fees (``demo_trader._risk``). A scenario only; stop slippage would make it worse.
ORPHAN_STOP_R = -1.0
ORPHAN_FLAT_R = 0.0  # break-even scenario: an orphan closed flat (0R); not an upper bound (it may have won); a scenario too

DEMO: tuple[DemoSpec, ...] = (
    DemoSpec(
        "S3", "Демо S3 v3 (4h + 1d)", None,
        {30: ((-0.473, 0.361), (-0.719, 0.652)), 50: ((-0.388, 0.265), (-0.545, 0.494)), 100: ((-0.299, 0.157), (-0.418, 0.307))},
        source="verification/2026-10-30-passport-s3.md",
    ),
    DemoSpec(
        "S12", "Демо S12 v4 (вхід за закриттям свічки дотику, з 31.10)", frozenset({"v9"}),
        {30: ((-0.440, 0.496), (-0.827, 1.095)), 100: ((-0.259, 0.259), (-0.524, 0.652))},
        source="verification/2026-11-01-s12-modules.md; n 100: verification/2026-11-11-s12-v4-band-n100.md",
    ),
    DemoSpec(
        "S28", "Демо S28 (4h, лонг, лімітка)", frozenset({"v8"}),
        {30: ((-0.438, 0.247), (-0.667, 0.397)), 50: ((-0.367, 0.174), (-0.528, 0.223)), 100: ((-0.290, 0.091), (-0.350, 0.044))},
        fill_share=0.61, source="verification/2026-10-30-passport-s12v7-s28.md",
    ),
    DemoSpec(
        "S17", "Демо S17 1d (лімітка)", frozenset({"v10"}),
        {30: ((-0.335, 0.403), (-0.404, 0.452)), 50: ((-0.247, 0.307), (-0.326, 0.348)), 100: ((-0.169, 0.229), (-0.217, 0.214))},
        timeframe="1d", fill_share=0.888, source="verification/2026-10-31-passport-s17-s23.md",
    ),
    DemoSpec(
        "S14", "Демо S14 (4h + 1d)", frozenset({"v3"}),
        {30: ((-0.455, 0.408), (-0.830, 1.031)), 50: ((-0.366, 0.310), (-0.672, 0.691)), 100: ((-0.280, 0.196), (-0.491, 0.447))},
        source="verification/2026-10-30-passport-s14-s16.md",
    ),
    DemoSpec(
        "S16", "Демо S16 (4h + 1d)", frozenset({"v3"}),
        {30: ((-0.447, 0.443), (-0.684, 0.745)), 50: ((-0.361, 0.328), (-0.570, 0.586)), 100: ((-0.252, 0.225), (-0.405, 0.468))},
        source="verification/2026-10-30-passport-s14-s16.md",
    ),
    DemoSpec(
        "S38", "Демо S38 (шорт)", frozenset({"v5"}),
        {30: ((-0.372, 0.335), (-0.535, 0.576)), 50: ((-0.296, 0.245), (-0.468, 0.374)), 100: ((-0.220, 0.163), (-0.373, 0.333))},
        source="verification/2026-10-30-passport-s38-s39-s40.md",
    ),
    DemoSpec(
        "S39", "Демо S39 (шорт)", frozenset({"v5"}),
        {30: ((-0.327, 0.346), (-0.837, 1.010)), 50: ((-0.256, 0.268), (-0.801, 0.857)), 100: ((-0.181, 0.188), (-0.681, 0.550))},
        source="verification/2026-10-30-passport-s38-s39-s40.md",
    ),
    DemoSpec(
        "S40", "Демо S40 (лонг)", frozenset({"v6"}),
        {30: ((-0.338, 0.388), (-0.898, 1.492)), 50: ((-0.262, 0.300), (-0.839, 1.250)), 100: ((-0.185, 0.218), (-0.672, 0.738))},
        source="verification/2026-10-30-passport-s38-s39-s40.md",
    ),
)  # fmt: skip


def decision_band(bands: Mapping[int, Band], n: int) -> dict[str, Any]:
    """The band of the largest computed n' <= n (the smallest one when n is below all of them, for display) and its
    decision band: per end the wider of i.i.d. and rolling (CHECKPOINT, «Загальне правило демо»)."""
    keys = sorted(bands)
    ref = max((k for k in keys if k <= n), default=keys[0])
    (il, ih), (rl, rh) = bands[ref]
    return {"n_ref": ref, "iid": (il, ih), "rolling": (rl, rh), "decision": (min(il, rl), max(ih, rh))}


def _versioned(row: Mapping[str, Any], now_ms: int) -> str | None:
    """The trade's version tag: its own ``version`` column when it names an epoch of its setup, else the epoch at its
    fill (an unfilled placement: at its signal bar's close, the earliest moment it could have been placed). The setup
    name is stripped as in ``version_stats`` so both give one trade the same tag."""
    setup = _setup(row.get("setup"))
    tag = row.get("version")
    if isinstance(tag, str) and any(e.tag == tag and applies(e, setup) for e in EPOCHS):
        return tag
    t = _num(row.get("opened_ts"))
    if t is None:
        sig, tf = _num(row.get("signal_ts")), TF_MS.get(str(row.get("timeframe")))
        t = None if sig is None or tf is None else sig + tf
    e = version_of(setup, t, now_ms=now_ms)
    return None if e is None else e.tag


def _band_verdict(mean: float, band: Mapping[str, Any]) -> tuple[str, str | None, bool]:
    """(label, 'нижче' | 'вище' | None, outside the i.i.d. band) of a mean against a decision band."""
    lo, hi = band["decision"]
    il, ih = band["iid"]
    if mean < lo:
        return NO, "нижче", True
    if mean > hi:
        return NO, "вище", True
    return OK, None, not il <= mean <= ih


def _closed_r(rows: Sequence[Mapping[str, Any]], now_ms: int) -> list[Mapping[str, Any]]:
    out = [
        r
        for r in rows
        if r.get("status") == "closed"
        and (c := _num(r.get("closed_ts"))) is not None
        and c <= now_ms
        and _num(r.get("r_multiple")) is not None
    ]
    return sorted(out, key=lambda r: (_num(r.get("opened_ts")) or 0.0, str(r.get("link_id"))))


def demo_fee_note(closed: Sequence[Mapping[str, Any]], now_ms: int) -> str | None:
    """Item 341: the demo's fees against the backtest fee model on the row's own sample (closed trades of the current
    version), descriptive only. ``fee_gap_r`` per trade comes from ``demo_execution.trade_metrics`` (+ = cheaper than
    the model) over the trades whose ``fees_usd``, ``risk_usd``, ``qty``, ``fill`` and ``exit`` are known; the taker
    share counts the entries whose kind is recorded. None when no trade has its fees: the row then has no note."""
    ms = [m for t in closed if (m := trade_metrics(t, now_ms)) is not None and m["fee_gap_r"] is not None]
    if not ms:
        return None
    gap = round(statistics.fmean(float(m["fee_gap_r"]) for m in ms), 3) + 0.0  # + 0.0: no "-0.000" in the text
    kinds = [m["entry_kind"] for m in ms if m["entry_kind"] in ("maker", "taker", "mixed")]
    if kinds:
        taker = sum(1 for k in kinds if k == "taker")
        mixed = sum(1 for k in kinds if k == "mixed")
        share = f"тейкерних входів {taker} з {len(kinds)} ({_pc(taker / len(kinds))})" + (f", змішаних {mixed}" if mixed else "")
    else:
        share = "тип входу не записано (рядки до 22.10)"
    return (
        f"комісії демо проти моделі бектесту (угод із відомими комісіями {len(ms)}): середній fee_gap_r {gap:+.3f}R "
        f"(+ = дешевше за модель), {share}; паспорт з фактичними комісіями ≈ OOS + fee_gap_r = OOS {gap:+.3f}R на угоду; "
        "лише описово: вибірку, n, середній R і вердикт не змінено"
    )


def demo_forward(spec: DemoSpec, trades: Sequence[Mapping[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    """Rows of one demo setup: mean demo R (``r_multiple``: venue P&L with fees / risk from the fill) of the closed
    trades of its current version against the band of the largest n' <= n; for a limit setup also the fill share.
    The ``orphaned`` trades of the current version (filled by ``now_ms``, no R) stay out of the sample; a note shows
    the mean and the label with each of them closed at its stop (``ORPHAN_STOP_R``), so an «узгоджується» resting on them
    is visible (review of item 326, remark 1)."""
    mine = [t for t in trades if _setup(t.get("setup")) == spec.setup and (spec.timeframe is None or t.get("timeframe") == spec.timeframe)]
    ver = {id(t): _versioned(t, now_ms) for t in mine}
    cur = [t for t in mine if spec.versions is None or ver[id(t)] in spec.versions]
    closed = _closed_r(cur, now_ms)
    other = len(_closed_r(mine, now_ms)) - len(closed)
    rs = [float(t["r_multiple"]) for t in closed]
    n = len(rs)
    mean = statistics.fmean(rs) if rs else None
    band = decision_band(spec.bands, n)
    row = _row(f"demo:{spec.setup}", "demo", spec.name, n=n, metric=mean, metric_name="середній R демо", band=band)
    notes: list[str] = row["notes"]
    if other:
        notes.append(f"закритих угод інших версій (не входять): {other}")
    if mean is None or n < MIN_DEMO:
        notes.append(f"рано: {n} з {MIN_DEMO} закритих угод")
    else:
        label, side, edge = _band_verdict(mean, band)
        row |= {"verdict": label, "side": side}
        if label == OK and edge:
            notes.append("поза i.i.d.-смугою, але в смузі рішення: на межі, не аргумент")
    # CHECKPOINT, «Загальне правило демо», «Метрика»: the clustered CI is descriptive, the label is the band's. Only from
    # MIN_DEMO on: below it a bootstrap over 2-6 signal days is degenerate (n 2 on 2 days gave [-1.032; -1.019] live)
    if n >= MIN_DEMO:
        s = _r_ci(closed)
        ci = f"[{s['ci'][0]:+.3f}; {s['ci'][1]:+.3f}]" if s["ci"] else "— (усі угоди одного дня сигналу)"
        # _r_ci gives a row without signal_ts its own «day»: more days, a narrower CI (review of item 343, remark 1)
        no_sig = sum(_num(t.get("signal_ts")) is None for t in closed)
        notes.append(
            f"95 % CI середнього R кластерами за UTC-днем сигналу: {ci}, днів {s['days']} (бутстреп {S12_CI_BOOT}, "
            f"seed {S12_CI_SEED}); лише описово: вердикт — за смугою рішення; днів мало, тож CI може бути занижений"
            + (f"; угод без часу сигналу {no_sig}: кожна рахується окремим днем і звужує CI" if no_sig else "")
        )
    # dated by the fill, a row without it by its signal bar (as the report's orphan note)
    at = [
        next((v for k in ("opened_ts", "signal_ts") if (v := _num(t.get(k))) is not None), None)
        for t in cur
        if t.get("status") == "orphaned"
    ]
    orphans = sum(1 for o in at if o is not None and o <= now_ms)
    if orphans:
        m2 = statistics.fmean([*rs, *[ORPHAN_STOP_R] * orphans])
        text = (
            f"orphaned цієї версії (поза вибіркою): {orphans}; якщо кожну закрито за стопом ({ORPHAN_STOP_R:+.0f}R, "
            f"проковзування дало б гірше): n {n} → {n + orphans}, середній R {_f(mean, 3, sign=True)} → {m2:+.3f}"
        )
        if n + orphans >= MIN_DEMO:
            label2, side2, _ = _band_verdict(m2, decision_band(spec.bands, n + orphans))
            text += f", вердикт {row['verdict']} → {label2}" + (f" ({side2})" if side2 else "")
        # break-even scenario: each orphan closed flat (0R); not an upper bound, an orphan may have closed at a win
        m0 = statistics.fmean([*rs, *[ORPHAN_FLAT_R] * orphans])
        text += f"; сценарій беззбитку «орфани = 0R»: середній R {m0:+.3f}"
        if n + orphans >= MIN_DEMO:
            label0, side0, _ = _band_verdict(m0, decision_band(spec.bands, n + orphans))
            text += f", вердикт {label0}" + (f" ({side0})" if side0 else "")
        notes.append(text + "; лише сценарій, закриття перевірити за журналом біржі")
    if spec.setup == "S3":
        old = sum(1 for t in closed if ver[id(t)] in OLD_BE_VERSIONS)
        notes.append(f"з них за старим правилом беззбитку (v1 / v2, +30 % ROE): {old}, лише описово")
        if row["verdict"] == NO and n < S3_FINAL_N:
            notes.append(f"між 30 і 99 угодами «ні» — лише позначка; рішення про зняття — тільки на {S3_FINAL_N} угодах (Q-20261030-3)")
        elif row["verdict"] == NO and side == "нижче":
            notes.append("на ≥ 100 угодах нижче смуги рішення → зняти S3 (Q-20261030-3)")
    if spec.setup == "S28":
        # the first 30 filled trades by entry time (review of item 1, minor 1), closed or not; checked once all are closed
        filled = sorted(
            (t for t in cur if t.get("status") != "missed" and (o := _num(t.get("opened_ts"))) is not None and o <= now_ms),
            key=lambda t: (_num(t.get("opened_ts")) or 0.0, str(t.get("link_id"))),
        )[:MIN_DEMO]
        closed_ids = {id(t) for t in closed}
        still = sum(1 for t in filled if id(t) not in closed_ids)
        if len(filled) == MIN_DEMO and still:
            notes.append(f"звірка на перших 30 заповнених: серед них ще відкрито (або без R) {still}, звірка — коли закриються всі")
        elif len(filled) == MIN_DEMO:
            m30 = statistics.fmean(float(t["r_multiple"]) for t in filled)
            hi30 = decision_band(spec.bands, MIN_DEMO)["decision"][1]
            if m30 < S28_FLOOR_30:
                what = f"нижче {S28_FLOOR_30:+.3f} → зняти S28 і повідомити власника (Q-20261030-2)"
            elif m30 > hi30:
                what = f"вище {hi30:+.3f} → як у загальному правилі (окрема картка, не доказ переваги)"
            else:
                what = "у межах"
            notes.append(f"звірка на перших 30 заповнених угодах за часом входу: {m30:+.3f}, {what}")
    if fee := demo_fee_note(closed, now_ms):
        notes.append(fee)
    if row["verdict"] == NO:
        notes.append("«ні» → спершу розбір виконання (CHECKPOINT, «Що робимо при „ні“»), версію за одним «ні» не міняємо")
    out = [row]
    if spec.fill_share is not None:
        out.append(fill_forward(spec, cur, now_ms))
    return out


def fill_forward(spec: DemoSpec, trades: Sequence[Mapping[str, Any]], now_ms: int) -> dict[str, Any]:
    """Fill share of a limit setup over placements whose deadline (``rest_until``) has passed: «ні» when the 95 % Wilson
    CI on >= 30 such placements does not contain the passport share. A ``closing`` or ``orphaned`` trade is filled (the
    demo trader orphans only a filled trade whose position and close it can no longer find); a ``pending`` one past its deadline (not reconciled
    yet) stays out of the share and is counted in a note."""

    def past(t: Mapping[str, Any]) -> bool:
        return (ru := _num(t.get("rest_until"))) is not None and ru <= now_ms

    done = [t for t in trades if t.get("status") in ("open", "closing", "closed", "orphaned", "missed") and past(t)]
    stale = sum(1 for t in trades if t.get("status") == "pending" and past(t))
    filled = sum(1 for t in done if (o := _num(t.get("opened_ts"))) is not None and o <= now_ms)
    n = len(done)
    p = spec.fill_share or 0.0
    lo, hi = wilson_ci(filled, n)
    row = _row(
        f"demo:{spec.setup}:fill", "demo", f"{spec.name}: частка заповнення", n=n, metric=filled / n if n else None,
        metric_name="частка заповнених розміщень", band={"passport": p, "wilson": (lo, hi)},
    )  # fmt: skip
    if n < MIN_DEMO:
        row["notes"].append(f"рано: {n} з {MIN_DEMO} розміщень, чий строк минув")
    elif lo <= p <= hi:
        row["verdict"] = OK
    else:
        row |= {"verdict": NO, "side": "нижче" if hi < p else "вище"}
        if hi < p:
            row["notes"].append("post-only лімітка заповнюється гірше за модель «пройшла крізь рівень»")
    if stale:
        row["notes"].append(f"pending з минулим строком (ще не звірені, поза часткою): {stale}")
    return row


# --------------------------------------------------------------------------------------------- S12 stop rule
# Owner decision Q-20261109-1, option 1 (docs/verification/2026-11-09-s12-demo-vs-passport.md, «Пропозиція власнику»):
# S12 v9 stays on the demo as an execution control. The version of a trade is ``s12_demo_audit.traded_version``, as in
# that report; v4 and v9 share the v4 entry and the v4 base (``PASSPORT_OF``).
S12_STOP_N = 30  # v9 closed trades: an alarm, not an edge test
S12_STOP_FLOOR = PASSPORTS["v4"].band_sliding[0]  # -0.827: rolling p5 of the v4 base for n = 30
S12_DROP_N = 100  # closed trades with the v4 entry (v4 + v9)
# -0.524: rolling p5 of the v4 base for n = 100 (``r_band`` on the base, item 318; replaced the 09.11 estimate -0.50)
S12_DROP_FLOOR = V4_BAND_SLIDING_N100[0]
S12_CI_SEED, S12_CI_BOOT = 20261030, 10_000  # the report's clustered bootstrap
S12_HOLD, S12_STOP, S12_DROP = "тримати (перевагу не доведено)", "зупинити й розібрати виконання", "зняти"


def _r_ci(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """n, mean R and the 95 % CI clustered by the UTC day of the signal bar (None with fewer than 2 days)."""
    rs = [float(t["r_multiple"]) for t in rows]
    if not rs:
        return {"n": 0, "mean": None, "ci": None, "days": 0}
    days = []
    for t in rows:
        sig = _num(t.get("signal_ts"))
        days.append(datetime.fromtimestamp(sig / 1000, UTC).date().isoformat() if sig is not None else f"?{t.get('link_id')}")
    ci = cluster_bootstrap_ci(rs, days, n_boot=S12_CI_BOOT, seed=S12_CI_SEED) if len(rs) >= 2 else None
    return {"n": len(rs), "mean": statistics.fmean(rs), "ci": ci, "days": len(set(days))}


def s12_stop_rule(trades: Sequence[Mapping[str, Any]], now_ms: int) -> dict[str, Any]:
    """The owner's S12 stop rule on the closed demo trades at ``now_ms``: v9 (n >= 30, mean below the rolling p5 of the
    v4 base for n 30 -> stop and review the execution) and the v4 entry, v4 + v9 (n >= 100, mean below the rolling p5
    of the v4 base for n 100 -> drop). Below its n a check is «рано», no verdict; otherwise «тримати (перевагу не доведено)». R per trade risk."""
    closed = _closed_r([t for t in trades if _setup(t.get("setup")) == "S12"], now_ms)
    ver = [traded_version(t, now_ms=now_ms)[0] for t in closed]
    v9 = [t for t, v in zip(closed, ver, strict=True) if v == "v9"]
    v4e = [t for t, v in zip(closed, ver, strict=True) if PASSPORT_OF.get(v) == "v4"]
    checks = []
    for name, rows, n_min, floor, hit in (
        ("v9", v9, S12_STOP_N, S12_STOP_FLOOR, S12_STOP),
        ("вхід v4 (v4 + v9)", v4e, S12_DROP_N, S12_DROP_FLOOR, S12_DROP),
    ):
        s = _r_ci(rows)
        if s["n"] < n_min:
            verdict = EARLY
        else:
            verdict = hit if s["mean"] < floor else S12_HOLD
        checks.append({"name": name, **s, "n_min": n_min, "floor": floor, "if_below": hit, "verdict": verdict})
    got = {c["verdict"] for c in checks}
    overall = next((v for v in (S12_DROP, S12_STOP, S12_HOLD) if v in got), EARLY)
    return {"checks": checks, "verdict": overall, "other": len(closed) - len(v4e)}


def s12_stop_rule_lines(block: Mapping[str, Any]) -> list[str]:
    """The Ukrainian section of the report for ``s12_stop_rule``."""
    L = ["## S12: правило зняття (Q-20261109-1, варіант 1)", "", f"- вердикт: {block['verdict']}"]
    for c in block["checks"]:
        ci = f"[{c['ci'][0]:+.3f}; {c['ci'][1]:+.3f}]" if c.get("ci") else "—"
        state = f"рано ({c['n']} з {c['n_min']}), без вердикту" if c["verdict"] == EARLY else c["verdict"]
        L.append(
            f"- {c['name']}: n {c['n']}, днів {c['days']}, середній R {_f(c['mean'], 3, sign=True)}, 95 % CI {ci}; "
            f"правило: n ≥ {c['n_min']} і середній R < {c['floor']:+.3f} → {c['if_below']}; зараз: {state}"
        )
    if block.get("other"):
        L.append(f"- закритих S12 з іншим входом (v7 чи версія невідома, не входять): {block['other']}")
    L += [
        f"- поріг v9 {S12_STOP_FLOOR:+.3f} — нижня межа ковзної смуги бази v4 для n 30; поріг {S12_DROP_FLOOR:+.3f} для n 100 — "
        "нижня межа ковзної смуги тієї самої бази для n 100 (`r_band`, звіт 11.11)",
        f"- поріг n {S12_STOP_N} — сигнал «зупинити й розібрати», а не статистичний тест: середнє перевіряється щодня "
        "на зростаючому n, тож хибна тривога буває частіше за 5 %",
        "- R — P&L біржі з комісіями / ризик угоди (не на капітал); CI — бутстреп кластерами за UTC-днем сигнальної "
        f"свічки ({S12_CI_BOOT}, seed {S12_CI_SEED}); днів мало, тож CI може бути занижений. «Тримати» не означає перевагу: "
        "−0.01R від нуля демо не відрізнить",
        "",
    ]
    return L


# ---------------------------------------------------------------------------------------------------------- carry

COST_RATIO_MAX = 1.5  # (b): mean actual <= 1.5 x mean model (taker + 0.0005) per leg side
DEPTH_SHORT_MAX = 0.10  # (b): at most 10 % of the sides without enough recorded depth
MIN_SIDES = 20
MIN_DAYS = 7.0  # (v): a paper trade held >= 7 days
MIN_V_TRADES = 10
V_PASS_SHARE = 0.80
DEFER_SHARE_LIMIT = 0.20  # registration (4): above it a reliability warning printed next to (d); no verdict depends on it
MIN_G_TRADES = 10
RUNUP_FROM, RUNUP_TO = 0.25, 0.50  # (g) A: the perp move against the short from < 0.25 to >= 0.50 between two cycles
MIN_CYCLES = 30
CYCLE_PASS = 0.95
CARRY_NAMES = {"A": "Папір carry A (cc-paper)", "B": "Папір carry B (pp-paper)"}
PASS, FAIL, UNVERIFIABLE, NOT_HERE = "пройдено", "не пройдено", "◯", "не рахується тут"


def _crit(cid: str, status: str, text: str) -> dict[str, str]:
    return {"id": cid, "status": status, "text": text}


# (e) universe purity and (ye) capacity (registration of the paper carry; amendment (2) for the ladder)
MIN_E_ENTRIES = 1
MIN_YE_ENTRIES = 10
YE_PASS_SHARE = 0.90
DEPTH_KEY = "0.5%"  # ``depth_usd`` of a side row (carry_rules.leg_execution): USD within ±0.5 % of mid, per book side
BYBIT_VENUE = "bybit"  # the engine's second venue (collector.carry_paper_engine.BYBIT; analytics imports no collector)
# The feed's exclusion reasons an entered symbol must never carry (``carry_paper_feed``): ``gap`` = too few common bars
# to check the collision, so it counts as a collision too.
E_EXCLUSIONS = {"tradfi": "tradfi", "collision": "collision", "gap": "collision"}
E_PREFIX = re.compile(r"^(1000000|10000|1000)([A-Z].*)$")  # the feed's 1000x prefix rule (carry_paper_feed.MULT)
E_INTERVAL_MAX_H = 8.0  # the longest regular funding interval (carry_paper_feed.RELOAD_BACK_MS)
# The registered collision threshold: |median(price ratio of the legs) - 1| > 0.005, as the backtest that registered it
# defines it (scripts/verify_carry_perp_perp.py, ``abs(statistics.median(ratios) - 1.0) > 0.005``; the feed's
# carry_paper_feed.COLLISION_GAP), on the engine's candidate rows of the position from its entry cycle to its close (or
# the report date). The window is ours: the registration names none; the backtest took the whole history (>= 20 bars).
E_COLLISION_GAP = 0.005
E_GAP_MIN_ROWS = 3  # fewer candidate rows over the holding period: the collision part of the entry is unchecked
# Developer's coarse addition outside the registration: leg mids (A's spot times its multiplier) more than 1.5x apart at
# entry, i.e. another asset or an unscaled multiplier (B enters at most 2 % apart, max_entry_gap)
E_PRICE_RATIO_MAX = 1.5
E_NAMES = {
    "gap": f"|медіана розриву цін ніг| за утримання > {E_COLLISION_GAP * 100:g} % (колізія тікерів за метрикою реєстрації)",
    "tradfi": "TradFi-перп у журналі виключень фіду (tradfi)",
    "collision": "колізія в журналі виключень фіду (collision / gap)",
    "interval": "інтервал funding поза (0; 8] год",
    "multiplier": "множник 1000… спотової ноги не за префіксом перпа",
    "price": f"mid ніг на вході розходяться більш ніж у {E_PRICE_RATIO_MAX:g} раза (груба межа розробника, поза реєстрацією)",
}


def _carry_entries(
    pos: Sequence[Mapping[str, Any]], side_rows: Sequence[Mapping[str, Any]], cycles: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Per position: its symbol, entry time, open ``side`` payloads, entry cycle (the cycle of its open side rows, else
    the cycle whose ``decision_ts`` is the position's ``opened_ts``; None when neither is recorded) and ``held``: its
    candidate rows (``cp_cycle.candidates``, written every cycle while it is open) from the entry cycle (else
    ``opened_ts``) to ``closed_ts`` (``pos`` as of the report date, so an open one runs to the last cycle)."""
    opens: dict[str, list[Mapping[str, Any]]] = {}
    for x in side_rows:
        if (x.get("payload") or {}).get("event") == "open":
            opens.setdefault(str(x.get("trade_id")), []).append(x)
    by_ts = {int(c["cycle_ts"]): c for c in cycles}
    by_decision = {int(d): c for c in cycles if (d := _num((c.get("payload") or {}).get("decision_ts"))) is not None}
    out = []
    for q in pos:
        rows = opens.get(str(q["trade_id"]), [])
        cyc = by_ts.get(int(rows[0]["cycle_ts"])) if rows else None
        cyc = cyc if cyc is not None else by_decision.get(int(q["opened_ts"]))
        sym = str(q.get("symbol"))
        start = min(int(q["opened_ts"]), int(cyc["cycle_ts"])) if cyc is not None else int(q["opened_ts"])
        end = int(q["closed_ts"]) if q.get("status") == "closed" and q.get("closed_ts") is not None else None
        held = [
            c
            for x in cycles
            if start <= int(x["cycle_ts"]) and (end is None or int(x["cycle_ts"]) <= end)
            for c in (x.get("payload") or {}).get("candidates") or []
            if isinstance(c, Mapping) and c.get("symbol") == sym
        ]
        out.append(
            {"symbol": sym, "opened_ts": int(q["opened_ts"]), "legs": [x.get("payload") or {} for x in rows], "cycle": cyc, "held": held}
        )
    return out


def _held_gap(strategy: str, rows: Sequence[Mapping[str, Any]]) -> float | None:
    """|median price gap| between the legs over the candidate rows of a holding period (the gap signed, as the backtest's
    ``abs(median(ratios) - 1)``): B ``gap`` (price_a / price_b - 1, else from the two prices); A ``perp`` / ``spot`` - 1
    (``spot`` already times the multiplier, carry_paper_feed). None below ``E_GAP_MIN_ROWS`` rows with both prices."""
    gaps = []
    for r in rows:
        if strategy == "B":
            g = _num(r.get("gap"))
            if g is None and (pa := _num(r.get("price_a"))) and (pb := _num(r.get("price_b"))) and pa > 0 and pb > 0:
                g = pa / pb - 1.0
        else:
            s, p = _num(r.get("spot")), _num(r.get("perp"))
            g = p / s - 1.0 if s and p and s > 0 and p > 0 else None
        if g is not None:
            gaps.append(g)
    return abs(statistics.median(gaps)) if len(gaps) >= E_GAP_MIN_ROWS else None


def _leg_ratio(legs: Sequence[Mapping[str, Any]]) -> float | None:
    """Ratio of the two leg mids at entry, each times its contract multiplier; None without both books."""
    mids = []
    for leg in legs:
        mid, mult = _num(leg.get("mid")), _num(leg.get("contract_multiplier"))
        if mid is None or mid <= 0:
            return None
        mids.append(mid * (mult if mult and mult > 0 else 1.0))
    return mids[0] / mids[1] if len(mids) == 2 else None


def carry_universe(strategy: str, entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Criterion (e) on the entries (``_carry_entries``): 0 entries into a ticker collision (the registered metric: |median
    price gap| between the legs > 0.005 over the position's candidate rows from entry to close, ``_held_gap``, at least
    ``E_GAP_MIN_ROWS`` rows; for A the spot x multiplier vs perp gap, so basis counts too), an irregular funding interval
    (outside (0, 8] h; an unknown one is only counted) or an unscaled ``1000…`` multiplier; >= 1 checked entry, else ◯.
    Also counted as violations: the symbol in its entry cycle's exclusion journal as ``tradfi`` / ``collision`` / ``gap``
    (a guard against future feed changes: today an excluded symbol never reaches the engine, so it cannot fire) and
    the developer's coarse leg-mid ratio > 1.5x at entry (outside the registration). TradFi is not checked independently
    (``underlyingType`` at the checkpoint is network). An entry without a violation is checked when its entry cycle holds
    its candidate row, the exclusion list is complete (``excluded`` is capped at 500) and the gap has enough rows."""
    fails: dict[str, list[str]] = {}
    checked = no_cand = few_rows = no_price = no_interval = 0
    widest: float | None = None
    worst_gap: float | None = None
    for e in entries:
        p = (e["cycle"] or {}).get("payload") or {}
        sym = e["symbol"]
        cand = next((c for c in p.get("candidates") or [] if isinstance(c, Mapping) and c.get("symbol") == sym), None)
        excl = [x for x in p.get("excluded") or [] if isinstance(x, Mapping)]
        counts = p.get("excluded_counts") or {}
        cut = sum(v for v in counts.values() if isinstance(v, int)) > len(excl) if isinstance(counts, Mapping) else False
        why = {E_EXCLUSIONS[str(x.get("reason"))] for x in excl if x.get("symbol") == sym and str(x.get("reason")) in E_EXCLUSIONS}
        med = _held_gap(strategy, e.get("held") or [])
        if med is not None:
            worst_gap = max(worst_gap or 0.0, med)
            if med > E_COLLISION_GAP:
                why.add("gap")
        ivs = (cand or {}).get("interval_hours")
        ivs = ivs if isinstance(ivs, list) else [ivs]
        hours = [_num(v) for v in ivs] if cand is not None else []
        if any(h is None for h in hours):
            no_interval += 1  # unknown: the engine's APR is per time (sum of payments / window), so this is descriptive
        if any(h is not None and not 0 < h <= E_INTERVAL_MAX_H for h in hours):
            why.add("interval")
        if strategy == "A" and (spot := next((x for x in e["legs"] if x.get("leg") == "spot"), None)) is not None:
            base = sym.split("/")[0]
            m = E_PREFIX.match(base)
            mult, spot_base = (int(m.group(1)), m.group(2)) if m else (1, base)
            if _num(spot.get("contract_multiplier")) != mult or str(spot.get("symbol")).split("/")[0] != spot_base:
                why.add("multiplier")
        ratio = _leg_ratio(e["legs"])
        if ratio is None:
            no_price += 1
        else:
            widest = max(widest or 0.0, abs(ratio - 1.0))
            if not 1 / E_PRICE_RATIO_MAX <= ratio <= E_PRICE_RATIO_MAX:
                why.add("price")
        for w in why:
            fails.setdefault(w, []).append(sym)
        if why:  # a violation counts whatever else is missing (an excluded symbol has no candidate row)
            checked += 1
        elif cand is None or cut:
            no_cand += 1
        elif med is None:
            few_rows += 1
        else:
            checked += 1
    txt = f"перевірено входів {checked} з {len(entries)}"
    txt += ", порушень: " + ("; ".join(f"{E_NAMES[k]} — {', '.join(v)}" for k, v in sorted(fails.items())) if fails else "0")
    txt += (
        f"; не перевірено: без рядка кандидата в циклі входу чи з обрізаним списком виключень {no_cand}, "
        f"з < {E_GAP_MIN_ROWS} рядків кандидата за утримання {few_rows}; найбільша |медіана розриву цін ніг| за утримання "
        f"{'—' if worst_gap is None else f'{worst_gap * 100:.2f} %'} (поріг реєстрації 0.5 %; "
        f"{'A — spot×множник проти перпа, тобто разом із базисом' if strategy == 'A' else 'B — mid Binance проти Bybit'}); "
        f"інтервал невідомий {no_interval} (описово); без цін обох ніг на вході {no_price}; "
        f"поза реєстрацією — груба межа розробника {E_PRICE_RATIO_MAX:g}x для mid ніг на вході, найбільший розрив "
        f"{'—' if widest is None else f'{widest * 100:.1f} %'}; TradFi незалежно НЕ перевірено: виключений фідом символ "
        "до рушія не потрапляє, тож журнал виключень у циклі входу провалитися не може (лише запобіжник на зміни фіду), "
        "а underlyingType на чекпоінт — мережа"
    )
    if checked < MIN_E_ENTRIES:
        return _crit("е", UNVERIFIABLE, txt + f"; мінімум {MIN_E_ENTRIES} вхід")
    return _crit("е", FAIL if fails else PASS, txt) | {"symbols": sorted({s for v in fails.values() for s in v})}


def _bybit_id(symbol: str) -> str:
    """ccxt unified ``ERA/USDT:USDT`` -> Bybit's ``ERAUSDT`` (the key of a ladder snapshot)."""
    return symbol.split(":")[0].replace("/", "")


def carry_capacity(entries: Sequence[Mapping[str, Any]], ladder: Sequence[Mapping[str, Any]], now_ms: int) -> dict[str, Any]:
    """Criterion (ye) on the entries (``_carry_entries``): the share of entries where every leg's notional fits into the
    book depth within ±0.5 % of mid on its side (``depth_usd``, the open side rows) and the Bybit leg's notional is
    <= the largest 2x position of the nearest ladder snapshot taken before the entry (the first one for entries before
    it). ``ladder``: ``{date, taken_ms, limits: {bybit id: max 2x position USD or None}}``; snapshots after ``now_ms``
    are not used. Binance legs: depth only (amendment (2)). >= 90 % -> passed; < 10 entries with data -> ◯."""
    snaps = sorted((s for s in ladder if int(s["taken_ms"]) <= now_ms), key=lambda s: int(s["taken_ms"]))
    ok = thin = over = no_data = before_first = 0
    used: dict[str, int] = {}
    for e in entries:
        legs = e["legs"]
        has_data = len(legs) >= 2  # both legs' open side rows
        depth_ok = limit_ok = True
        snap_of: tuple[str, bool] | None = None  # (snapshot date, entry before the first snapshot)
        for leg in legs if has_data else ():
            need = _num(leg.get("notional_usd"))
            band = (leg.get("depth_usd") or {}).get(DEPTH_KEY)
            depth = _num(band.get("ask" if leg.get("side") == "buy" else "bid")) if isinstance(band, Mapping) else None
            if need is None or depth is None:
                has_data = False
                break
            depth_ok = depth_ok and need <= depth
            if leg.get("venue") == BYBIT_VENUE:
                prior = [s for s in snaps if int(s["taken_ms"]) <= e["opened_ts"]]
                snap = prior[-1] if prior else (snaps[0] if snaps else None)
                limits = (snap or {}).get("limits") or {}
                key = _bybit_id(str(leg.get("symbol") or e["symbol"]))
                if snap is None or key not in limits:
                    has_data = False
                    break
                snap_of = (str(snap["date"]), not prior)
                lim = _num(limits[key])  # None: no tier of the ladder allows 2x
                limit_ok = limit_ok and lim is not None and need <= lim
        if not has_data:
            no_data += 1
            continue
        if snap_of is not None:
            used[snap_of[0]] = used.get(snap_of[0], 0) + 1
            before_first += snap_of[1]
        ok += depth_ok and limit_ok
        thin += not depth_ok
        over += not limit_ok
    n = len(entries) - no_data  # entries with data (one both thin and over limit is one entry)
    txt = (
        f"входів з даними {n} з {len(entries)}, номінал вміщується {ok} ({_pc(ok / n if n else None)}; поріг 90 %); "
        f"книга ±0.5 % від mid мілкіша за номінал {thin}, понад ліміт Bybit для 2x {over}; без даних {no_data}; "
        "Binance-нога — лише глибина (ladder Binance підписаний, поправка (2)); глибина — не більше 50 рівнів книги, "
        "тож на дрібному кроці ціни занижена (консервативно)"
    )
    if used:
        txt += "; ліміт Bybit-ноги — за знімком ladder до входу: " + ", ".join(f"{d} — {k}" for d, k in sorted(used.items()))
        if before_first:
            txt += f" (з них до першого знімка, за першим: {before_first})"
    elif not snaps:
        txt += "; знімків ladder Bybit до дати звіту немає"
    if n < MIN_YE_ENTRIES:
        return _crit("є", UNVERIFIABLE, txt + f"; мінімум {MIN_YE_ENTRIES} входів")
    return _crit("є", PASS if ok / n >= YE_PASS_SHARE else FAIL, txt)


def ladder_note(ladder: Sequence[Mapping[str, Any]], now_ms: int) -> str | None:
    """The row note on skipped weekly ladder snapshots: entries between two snapshots more than a week apart are
    checked against the earlier one, and entries after a last snapshot older than a week at ``now_ms`` against it.
    None when neither happens (or there is no snapshot: the criterion's text says so)."""
    snaps = sorted((s for s in ladder if int(s["taken_ms"]) <= now_ms), key=lambda s: int(s["taken_ms"]))
    gaps = [(a, b) for a, b in zip(snaps, snaps[1:], strict=False) if int(b["taken_ms"]) - int(a["taken_ms"]) > 7 * DAY_MS]
    parts = [f"входи до {b['date']} (дата першого нового знімка) звіряються зі знімком ladder {a['date']}" for a, b in gaps]
    if gaps:
        parts[-1] += ": щотижневі знімки, яких вимагала реєстрація, пропущено, їх не відновити; зміни ladder між знімками не видно"
    if snaps and now_ms - int(snaps[-1]["taken_ms"]) > 7 * DAY_MS:
        parts.append(
            f"останній знімок ladder {snaps[-1]['date']} старший за тиждень на дату звіту: пізніші входи звіряються з ним; "
            "реєстрація вимагає знімка на чекпоінт (scripts/paper_ladder_snapshot.py того ж дня)"
        )
    return "(є): " + "; ".join(parts) if parts else None


def _asof(positions: Sequence[Mapping[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    """Positions opened by ``now_ms``; one closed later is open at ``now_ms``."""
    out = []
    for p in positions:
        if int(p["opened_ts"]) > now_ms:
            continue
        q = dict(p)
        if q.get("status") == "closed" and (q.get("closed_ts") is None or int(q["closed_ts"]) > now_ms):
            q |= {"status": "open", "closed_ts": None}
        out.append(q)
    return out


def _by_trade(logs: Sequence[Mapping[str, Any]], now_ms: int) -> dict[str, list[Mapping[str, Any]]]:
    out: dict[str, list[Mapping[str, Any]]] = {}
    for x in sorted((x for x in logs if int(x["cycle_ts"]) <= now_ms), key=lambda x: int(x["cycle_ts"])):
        out.setdefault(str(x.get("trade_id")), []).append(x)
    return out


def carry_forward(
    strategy: str,
    positions: Sequence[Mapping[str, Any]],
    logs: Mapping[str, Sequence[Mapping[str, Any]]],
    cycles: Sequence[Mapping[str, Any]],
    bench: Mapping[str, Any],
    now_ms: int,
    *,
    ladder: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Criteria (b), (v), (g), (d), (e), (ye) of the paper carry registration (IDEA-20261017-forward-paper-carry) on the
    engine's records: ``positions`` (cp_position rows, ``state`` / ``trade`` parsed), ``logs`` kind -> cp_log rows
    (``side``, ``mark``, ``close``, ``event``; ``payload`` parsed), ``cycles`` (cp_cycle rows), ``bench`` the frozen
    bench of the strategy (``docs/paper/bench.json`` -> ``<strategy>.bench``), ``ladder`` the dated Bybit ladder
    snapshots for (ye) (``carry_capacity``). Any computed criterion failed -> «ні» (for B a failed (v) is exempt:
    registration, «Як читати (в) для B»); any ◯ -> «рано»; else «узгоджується»."""
    pos = _asof(positions, now_ms)
    side_rows = [x for x in logs.get("side", ()) if int(x["cycle_ts"]) <= now_ms]
    marks = _by_trade(logs.get("mark", ()), now_ms)
    closes = _by_trade(logs.get("close", ()), now_ms)
    deferred = {
        str(x.get("trade_id"))
        for x in logs.get("event", ())
        if int(x["cycle_ts"]) <= now_ms and (x.get("payload") or {}).get("type") == "funding_payment_missing"
    }
    crit = []

    # (b) costs per executed leg side
    cr = cost_realism([x["payload"] for x in side_rows])
    ratio, short = cr.get("ratio"), cr.get("share_depth_short")
    no_book = cr.get("sides", 0) - cr.get("with_book", 0)  # sides without actual_cost: in neither the ratio nor the depth share
    txt = f"сторін з книгою {cr.get('with_book', 0)} з {cr.get('sides', 0)}; фактична / модельна {_f(ratio, 2)} (≤ 1.5), без глибини {_pc(short)} (≤ 10 %), без книги {no_book}"
    if cr.get("with_book", 0) < MIN_SIDES or ratio is None:
        crit.append(_crit("б", UNVERIFIABLE, txt + f"; мінімум {MIN_SIDES} сторін"))
    else:
        ok = ratio <= COST_RATIO_MAX and (short or 0.0) <= DEPTH_SHORT_MAX
        crit.append(_crit("б", PASS if ok else FAIL, txt))

    # (v) realized funding APR of trades held >= 7 days, deferred ones excluded
    entry_key = "entry_apr" if strategy == "A" else "entry_spread"
    units, no_bucket = [], 0
    for p in pos:
        tid = str(p["trade_id"])
        if tid in deferred:
            continue
        if p["status"] == "closed" and isinstance(p.get("trade"), Mapping):
            tr = p["trade"]
            o, c, funding, entry = int(tr["open_ts"]), int(tr["close_ts"]), _num(tr.get("funding")), _num(tr.get(entry_key))
        else:
            ms = marks.get(tid)
            if not ms:
                continue
            o, c = int(p["opened_ts"]), int(ms[-1]["cycle_ts"])
            f_usd = _num((ms[-1].get("payload") or {}).get("funding_usd"))
            funding = None if f_usd is None else f_usd / float(p["slot_notional"])
            entry = _num((p.get("state") or {}).get(entry_key))
        if funding is None or entry is None or (c - o) / DAY_MS < MIN_DAYS:
            continue
        apr = realized_funding_apr(funding, o, c)
        hit = None if apr is None else band_check(bench, entry, apr)["in_band"]
        if hit is None:
            no_bucket += 1
        else:
            units.append(hit)
    n_def = len(deferred & {str(p["trade_id"]) for p in pos})
    share_def = n_def / len(pos) if pos else None
    inside = sum(units)
    txt = f"угод з ≥ 7 днями (без відкладених) {len(units)}, у смузі бенчу {inside} ({_pc(inside / len(units) if units else None)}; поріг 80 %)"
    txt += f"; відкладених {n_def} з {len(pos)}" + (f"; без кошика {no_bucket}" if no_bucket else "")
    if len(units) < MIN_V_TRADES:  # registration (4): (v) is ◯ only when < 10 trades are left after the deferred ones
        crit.append(_crit("в", UNVERIFIABLE, txt + f"; мінімум {MIN_V_TRADES} угод без відкладених"))
    else:
        ok = inside / len(units) >= V_PASS_SHARE
        if not ok and strategy == "B":
            txt += "; для B провал (в) читається як нестаціонарність funding, а не розбіжність рушія (не дає «ні»)"
        crit.append(_crit("в", PASS if ok else FAIL, txt))

    # (g) stop / liquidation earlier than the model. The engine never records a forced close (its exit reasons are only
    # 'signal' / 'stop'), so a labelled stand-in from its flags is counted instead: a trade whose symbol was flagged
    # ``open_position_data_unavailable`` (perp or spot gone from the feed) in a cycle while it was open, or that has a
    # ``funding_gap_confirmed`` event. It is not a confirmed delisting.
    cyc = [c for c in cycles if int(c["cycle_ts"]) <= now_ms]
    vanished: set[str] = set()
    for c in cyc:
        ts = int(c["cycle_ts"])
        gone = {
            str(f.get("symbol"))
            for f in (c.get("payload") or {}).get("flags") or []
            if isinstance(f, Mapping) and f.get("issue") == "open_position_data_unavailable"
        }
        vanished |= {
            str(p["trade_id"])
            for p in pos
            if str(p.get("symbol")) in gone and int(p["opened_ts"]) < ts and (p.get("closed_ts") is None or int(p["closed_ts"]) > ts)
        }
    gap = {
        str(x.get("trade_id"))
        for x in logs.get("event", ())
        if int(x["cycle_ts"]) <= now_ms and (x.get("payload") or {}).get("type") == "funding_gap_confirmed"
    } & {str(p["trade_id"]) for p in pos}
    proxy = (
        f"проксі примусового закриття: {ua_trades(len(vanished | gap))} за прапорцями рушія (open_position_data_unavailable "
        f"{len(vanished)}, funding_gap_confirmed {len(gap)}); підтвердження делістингу — окремо"
    )
    marked = [p for p in pos if marks.get(str(p["trade_id"])) or p["status"] == "closed"]
    if strategy == "A":
        events, big = 0, 0
        for p in marked:
            tid = str(p["trade_id"])
            seq = [_num((m.get("payload") or {}).get("max_runup")) for m in marks.get(tid, [])]
            seq += [_num((m.get("payload") or {}).get("max_runup")) for m in closes.get(tid, [])]
            prev = 0.0
            for v in (x for x in seq if x is not None):
                events += prev < RUNUP_FROM and v >= RUNUP_TO
                prev = v
            big += prev >= RUNUP_TO
        txt = (
            f"подій (хід перпа проти шорта з < 0.25 до ≥ 0.50 між сусідніми записаними 8h-циклами) {events} на {len(marked)} угодах; "
            f"частка угод з ходом ≥ 0.50 {_pc(big / len(marked) if marked else None)} (бектест 29 %, описово); "
            f"{proxy} (описово)"
        )
        if events:
            crit.append(_crit("г", FAIL, txt))
        elif len(marked) < MIN_G_TRADES:
            crit.append(_crit("г", UNVERIFIABLE, txt + f"; «пройдено» лише з {MIN_G_TRADES} угод"))
        else:
            crit.append(_crit("г", PASS, txt))
    else:
        txt = (
            f"примусове закриття біржею з записів рушія не визначається (◯, поки проксі не підтверджено); {proxy}; "
            f"угод {len(marked)}; ліквідації за 1m mark цим скриптом не рахуються (потрібна мережа)"
        )
        crit.append(_crit("г", UNVERIFIABLE, txt))

    # (d) engine reliability
    good = sum(
        1 for c in cyc if c.get("status") == "done" and (c.get("payload") or {}).get("on_time") and (c.get("payload") or {}).get("complete")
    )
    on_time = sum(1 for c in cyc if c.get("status") == "done" and (c.get("payload") or {}).get("on_time"))
    # incomplete cycles by issue / detail of their flags (the engine's `complete` = no flags); `funding_history_incomplete`
    # is our own fetch cut short (page limit, 429 / 418), kept apart from the other details; `funding_rows_dropped` is the
    # venue's rows without a usable timestamp (a data defect, not our fetch); `funding_rows_gap_confirmed` is such a hole
    # confirmed unfillable by the feed (that cycle is incomplete too, later ones carry no flag for it)
    issues: dict[str, int] = {}
    for c in cyc:
        p = c.get("payload") or {}
        if c.get("status") == "done" and not p.get("complete"):
            keys = {
                str(f.get("issue")) + (f" / {f['detail']}" if isinstance(f.get("detail"), str) else "")
                for f in p.get("flags") or []
                if isinstance(f, Mapping)
            }
            for k in sorted(keys or {"?"}):
                issues[k] = issues.get(k, 0) + 1
    txt = (
        f"циклів {len(cyc)}, вчасно (≤ 30 хв) і з повними даними {good} ({_pc(good / len(cyc) if cyc else None)}; поріг 95 %); "
        f"лише вчасно {on_time}"
    )
    if issues:
        txt += "; неповні за прапорцями (issue / detail): " + ", ".join(f"{k} {v}" for k, v in sorted(issues.items()))
    # registration (d): every skip is journaled with its reason; descriptive, the verdict does not depend on it
    skipped = [c for c in cyc if c.get("status") == "skipped"]
    no_reason = sum(1 for c in skipped if not str(c.get("reason") or "").strip())
    txt += f"; пропущених циклів {len(skipped)}, з них без причини в журналі {no_reason}"
    txt += f"; частка відкладених угод {_pc(share_def)} ({n_def} з {len(pos)})"
    if share_def is not None and share_def > DEFER_SHARE_LIMIT:
        txt += " — > 20 %: ознака проблеми надійності (реєстрація, п. 4; вердикт від неї не залежить)"
    if len(cyc) < MIN_CYCLES:
        crit.append(_crit("д", UNVERIFIABLE, txt + f"; мінімум {MIN_CYCLES} циклів"))
    else:
        crit.append(_crit("д", PASS if good / len(cyc) >= CYCLE_PASS else FAIL, txt))

    # (e) universe purity and (ye) capacity on the entries, from the engine's own records (no network)
    entries = _carry_entries(pos, side_rows, cyc)
    e_crit = carry_universe(strategy, entries)
    crit.append({k: v for k, v in e_crit.items() if k != "symbols"})
    crit.append(carry_capacity(entries, ladder, now_ms))

    crit.append(_crit("а", NOT_HERE, f"входів {len(pos)}; еквівалентність рішень — окремою перевіркою (повторний прогін коду бектесту)"))
    fails = [c for c in crit if c["status"] == FAIL and not (strategy == "B" and c["id"] == "в")]
    waits = [c for c in crit if c["status"] == UNVERIFIABLE]
    row = _row(f"carry:{strategy}", "carry", CARRY_NAMES[strategy], n=len(pos), metric_name="критерії (б)–(є) реєстрації", criteria=crit)
    if strategy == "B" and (note := ladder_note(ladder, now_ms)):  # only B has a Bybit leg
        row["notes"].append(note)
    if e_crit["status"] == PASS:
        row["notes"].append("(е) перевірено частково: TradFi — лише логіка самого фіду, незалежно не перевірено")
    if fails:
        row["verdict"] = NO
        row["notes"].append("не пройдено: " + ", ".join(f"({c['id']})" for c in fails))
        if any(c["id"] in ("б", "г") for c in fails):
            row["notes"].append("(б) або (г) системно → стратегія нереалістична на наших розмірах (критерій «вбити»)")
        if any(c["id"] == "в" for c in fails):
            row["notes"].append("(в) → розбір funding-даних і рушія, не «вбити»")
        if any(c["id"] == "е" for c in fails):
            row["notes"].append(f"(е) → баг рушія (реєстрація): розібрати входи {', '.join(e_crit.get('symbols', []))}")
        if any(c["id"] == "є" for c in fails):
            row["notes"].append(
                "(є) → номінал ноги не вміщується в книгу ±0.5 % чи ліміт Bybit на > 10 % входів: розбір розміру ноги й всесвіту"
            )
        if any(c["id"] == "д" for c in fails) and cyc and on_time / len(cyc) >= CYCLE_PASS:
            row["notes"].append(
                "(д) не пройдено лише за «з повними даними» реєстрації: вчасно ≥ 95 %, а неповні цикли — це прапорці рушія "
                "(розбивка за issue / detail вище; funding_history_incomplete — наш запит обрізано лімітом сторінок або 429 / 418; "
                "funding_rows_dropped — майданчик віддав рядки без придатного часу; funding_rows_gap_confirmed — така дірка "
                "не зникла за кілька циклів, її визнано непоправною, цей цикл теж неповний, далі прапорця немає). "
                "Правило записано 17.10 до старту рушія; інше прочитання тепер — лише позначений exploratory-варіант "
                "(CHECKPOINT, вступ до розділу)"
            )
    elif waits:
        row["notes"].append("◯ у " + ", ".join(f"({c['id']})" for c in waits) + ": за реєстрацією це «узгоджено частково», папір іде далі")
    else:
        row["verdict"] = OK
    stop = next((c for c in cyc if (c.get("payload") or {}).get("entries_stopped")), None)
    if stop is not None:  # the engine opens nothing new (kill criterion); held positions close by the exit rule
        row["notes"].append(
            f"нові входи зупинено з циклу {_ymd_s(int(stop['cycle_ts']) // 1000)} ({stop['payload']['entries_stopped']}); "
            "відкриті позиції закриваються за правилом виходу"
        )
    row["notes"].append("P&L — лише описово (MDE: ≈ 40 угод A, ≈ 330 угод B); його тут не показано")
    return row


A_V2_BANDS: Mapping[int, tuple[float, float]] = {10: (-1.9, 22.0), 16: (0.5, 19.6), 20: (1.5, 18.5)}  # mean Δ, pp of slot
A_V2_MIN = 10
A_V2_SINCE_MS = _ms(2026, 11, 2)  # (2) counts events after the criterion was written
A_V2_KNOWN = frozenset({"MITO", "VELODROME"})  # seen on the live DB slice of 31.10, before the criterion


def _base(symbol: str) -> str:
    return str(symbol).split("/")[0].upper()


def vanish_independent(payment_ts: Sequence[int], open_ts: int, end_ts: int, n_cycles: int = 2) -> int | None:
    """First cycle ``open_ts + k * 8h`` (<= ``end_ts``) closing ``n_cycles`` consecutive 8h windows after the entry with
    no recorded funding payment, else None. Written apart from ``carry_v2`` / ``carry_delisting`` so that it checks the
    shadow's code rather than repeating it (CHECKPOINT, A v2 (1))."""
    paid = {-(-(int(t) - open_ts) // H8_MS) for t in payment_ts if int(t) > open_ts}  # window index k: (k-1, k] x 8h
    run = 0
    k = 1
    while open_ts + k * H8_MS <= end_ts:
        run = 0 if k in paid else run + 1
        if run >= n_cycles:
            return open_ts + k * H8_MS
        k += 1
    return None


def a_v2_forward(
    positions: Sequence[Mapping[str, Any]],
    funding_logs: Sequence[Mapping[str, Any]],
    mark_logs: Sequence[Mapping[str, Any]],
    now_ms: int,
    *,
    side_cost: float,
    decision_delay_ms: int,
) -> list[dict[str, Any]]:
    """A v2 (the shadow of book A with the leg-vanish exit): (1) the shadow fires exactly where the raw funding records
    of A show >= 2 cycles without a payment (independent scan); (2) exploratory: mean Δ = P&L(A v2) − P&L(A) per event,
    pp of slot notional, on events after 2026-11-02 whose both exits are final, against the bootstrap band of the
    largest n' <= n (10 / 16 / 20)."""
    pos = _asof(positions, now_ms)
    funding: dict[str, list[int]] = {}
    for x in funding_logs:
        ts = (x.get("payload") or {}).get("payment_ts")
        if int(x["cycle_ts"]) <= now_ms and ts is not None and int(ts) <= now_ms:
            funding.setdefault(str(x["trade_id"]), []).append(int(ts))
    marks: dict[str, list[Mapping[str, Any]]] = {}
    for x in mark_logs:
        if int(x["cycle_ts"]) <= now_ms:
            marks.setdefault(str(x["trade_id"]), []).append(x)
    v = a_v2_shadow(pos, funding, marks, now_ms=now_ms, side_cost=side_cost, decision_delay_ms=decision_delay_ms)
    shadow = {r["trade_id"]: r for r in v["positions"]}

    missed = extra = 0
    events = 0
    for p in pos:
        tid = str(p["trade_id"])
        end = int(p["closed_ts"]) if p["status"] == "closed" else now_ms
        ind = vanish_independent(funding.get(tid, []), int(p["opened_ts"]), end)
        fired = shadow[tid]["rule_ts"]
        events += ind is not None
        if ind is not None and fired != ind:
            missed += 1
        elif ind is None and fired is not None:
            extra += 1
    r1 = _row("carry:A2:1", "carry", "Папір carry A v2: спрацювання правила (перевірка незалежно від коду тіні)", n=events)
    r1["metric_name"] = "пропусків / виходів без зникнення funding"
    r1["metric"] = None
    r1["counters"] = {"подій зникнення funding": events, "пропусків": missed, "зайвих виходів": extra}
    if missed or extra:
        r1["verdict"] = NO
        r1["notes"].append("розбіжність із записами funding → баг тіні, виправити")
    elif events:
        r1["verdict"] = OK
    else:
        r1["notes"].append("рано: подій зникнення funding ще не було (перевірка — з першої події)")
    r1["notes"].append("збіг виходів із фактом делістингу на біржі — окремо, описово (тут не рахується)")

    final = [r for r in v["positions"] if r["v2_status"] == "rule_exit" and r["v1_status"] == "closed" and r["v2_pnl_usd"] is not None]
    slot = {str(p["trade_id"]): float(p["slot_notional"]) for p in pos}

    def delta(r: Mapping[str, Any]) -> float:
        return (float(r["v2_pnl_usd"]) - float(r["v1_pnl_usd"])) / slot[r["trade_id"]] * 100.0

    main = [delta(r) for r in final if int(r["rule_ts"]) >= A_V2_SINCE_MS and _base(r["symbol"]) not in A_V2_KNOWN]
    every = [delta(r) for r in final]
    pending = sum(1 for r in v["positions"] if r["rule_ts"] is not None) - len(final)
    n = len(main)
    mean = statistics.fmean(main) if main else None
    ref = max((k for k in A_V2_BANDS if k <= n), default=min(A_V2_BANDS))
    lo, hi = A_V2_BANDS[ref]
    r2 = _row(
        "carry:A2:2", "carry", "Папір carry A v2: Δ = P&L(A v2) − P&L(A), exploratory", n=n, metric=mean,
        metric_name="середнє Δ на подію, п.п. номіналу слота", band={"n_ref": ref, "decision": (lo, hi), "unit": "pp"},
    )  # fmt: skip
    r2["counters"] = {"подій без остаточного Δ": pending, "з відомими до критерію (MITO, VELODROME)": len(every)}
    if every:
        r2["counters"]["середнє Δ з відомими, п.п. (описово)"] = round(statistics.fmean(every), 1)
    if mean is None or n < A_V2_MIN:
        r2["notes"].append(f"рано: {n} з {A_V2_MIN} подій після 02.11 (бектест дав 16 подій за 2 роки)")
    elif lo <= mean <= hi:
        r2["verdict"] = OK
    else:
        r2 |= {"verdict": NO, "side": "нижче" if mean < lo else "вище"}
        r2["notes"].append("exploratory: розбір, рішень за цим не ухвалюємо")
    r2["notes"].append("критерій записано після MITO і VELODROME, тож лише exploratory; решта критеріїв A v2 — спільні з A")
    return [r1, r2]


# ----------------------------------------------------------------------------------------------------- polymarket

PM_V1_NAME = "Папір Polymarket v1 (заморожений)"
PM_V2G_NAME = "Папір Polymarket v2-g (поправка H8)"
PM_MIN_NOTE = "мінімум для вердикту: ≥ 20 днів розв'язання і ≥ 10 тижневих кластерів"


def _pm_row(key: str, name: str, gate_s: int, now_ms: int, counters: dict[str, Any]) -> dict[str, Any]:
    when = datetime.fromtimestamp(gate_s, UTC).strftime("%Y-%m-%d %H:%M UTC")
    row = _row(key, "polymarket", name, n=counters.get("позицій"), embargo=True, counters=counters)
    row["metric_name"] = "чистий CLV +24 год — під забороною підглядання"
    if now_ms // 1000 < gate_s:
        row["notes"].append(f"заборона підглядання до {when}: лише лічильники; {PM_MIN_NOTE}")
    else:
        row["verdict"] = OUTSIDE
        row["notes"].append(f"вердикт один раз — scripts/pm_gap_paper_report.py (дата вердикту {when}); цей скрипт результатів не рахує")
    return row


def polymarket_forward(rows: Sequence[Mapping[str, Any]], outcomes: Sequence[Mapping[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    """Polymarket v1 and v2-g: counters only (a fixed whitelist; never a mean, CI, SD, edge, P&L or Brier)."""
    now_s = now_ms // 1000
    v1: dict[str, Any] = {"позицій": 0}
    if rows and now_s >= T0_TS:
        d = weekly_descriptive(rows, outcomes, now_s, T0_TS)
        p, src = d["positions"], d["sources"]
        shares = src.get("close_shares") or {}
        v1 = {
            "позицій": p["n"],
            "тижнів входу (кластери)": p["entry_weeks"],
            "днів розв'язання": p["resolution_days"],
            "з них уже минуло": p["resolution_days_ended"],
            "частки close / late / missing": " / ".join(_pc(shares.get(k)) for k in ("close", "late", "missing")),
            "частка missing на +24 год": _pc(src.get("h24_share_missing")),
        }
    v2: dict[str, Any] = {"позицій": 0}
    if rows and now_s >= T0_V2G_TS:
        d = weekly_v2g(rows, outcomes, now_s, T0_V2G_TS, v1_t0=T0_TS)
        p, src = d["positions"], d["sources"]
        v2 = {
            "позицій": p["n"],
            "тижнів входу (кластери)": p["entry_weeks"],
            "днів розв'язання": p["resolution_days"],
            "частка n/a на +24 год": _pc(src.get("h24_share_na")),
            "частка missing на +24 год": _pc(src.get("h24_share_missing")),
        }
    return [
        _pm_row("pm:v1", PM_V1_NAME, verdict_ts(T0_TS), now_ms, v1),
        _pm_row("pm:v2g", PM_V2G_NAME, verdict_ts_v2g(T0_V2G_TS, T0_TS), now_ms, v2),
    ]


# --------------------------------------------------------------------------------------------------------- condor

CONDOR_A_FINAL = date(2026, 12, 31)  # (a) final not before 2026-12-31 (before it only an interim look)
CONDOR_WEEKS_B = 52
CONDOR_MAP = {
    "conservative": (OK, "модель консервативна (−0.18 % пункту 7 — песимістична межа)"),
    "consistent": (OK, "модель узгоджена"),
    "optimistic": (NO, "модель оптимістична → бектести кондора на кеші беруть mark ∓ 1.5·R вп (R — верхня межа CI)"),
    "◯": (EARLY, "◯: < 6 валідних п'ятниць з 8, не подовжувати"),
}


def _cut(d: date, hour: int = ENTRY_CUTOFF_HOUR_UTC) -> int:
    return _ms(d.year, d.month, d.day, hour)


def condor_final_ms(t0: date) -> int:
    """The moment part (a) may be decided: after the last buy-back snapshot (Thursday T0 + 7 weeks + 6 days, 10:00 UTC)
    and not before 2026-12-31."""
    return max(_cut(cost_fridays(t0)[-1] + timedelta(days=6)), _ms(CONDOR_A_FINAL.year, CONDOR_A_FINAL.month, CONDOR_A_FINAL.day))


def condor_forward(t0: date | None, rows: Sequence[Mapping[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    """Condor H1 / H4: (a) costs actual / model, the BTC entry (a1) as primary; before its decision moment only the count
    of valid Fridays. (b) P&L: counters only until T0 + 52 weeks; never read here."""
    a = _row("condor:a", "condor", "Кондор: витрати (а), факт / модель", metric_name="R = Σ фактичних / Σ модельних, (а1) вхід BTC")
    b = _row("condor:b", "condor", "Кондор: P&L H4 (б)", embargo=True, metric_name="P&L — під забороною підглядання")
    if now_ms < _cut(EARLIEST_T0) or t0 is None:
        for r in (a, b):
            r["counters"] = {"п'ятниць від T0": 0}
            r["notes"].append(f"T0 ще не зафіксовано (не раніше {EARLIEST_T0}); знімки до T0 не читаються")
        return [a, b]
    fridays = cost_fridays(t0)
    done = [f for f in fridays if _cut(f) <= now_ms]
    final_at = condor_final_ms(t0)
    if now_ms < final_at:
        entry = [r for r in rows if not str(r.get("role", "")).startswith("hold_")]
        valid = 0
        for f in done:
            legs, _ = friday_entry(entry, f, "BTC")
            valid += legs is not None and condor_cost(legs, "open") is not None
        a |= {"n": valid, "counters": {"п'ятниць з 8, що минули": len(done), "валідних входів BTC": valid}}
        a["notes"].append(f"рано: рішення після {datetime.fromtimestamp(final_at / 1000, UTC):%Y-%m-%d %H:%M} UTC; до того лише лічильники")
    else:
        rep = cost_report(rows, fridays, "BTC")
        e = rep["entry"]
        label, what = CONDOR_MAP.get(e["verdict"], (EARLY, str(e["verdict"])))
        a |= {"n": e["n_valid"], "metric": _num(e["R"]), "band": {"ci": tuple(e["ci"])}, "verdict": label}
        a["notes"].append(what)
        a["notes"].append(f"(а2) викуп у четвер (друга): {rep.get('exit', {}).get('verdict', '—')}")
    weeks = sum(1 for k in range(CONDOR_WEEKS_B) if _cut(t0 + timedelta(days=7 * k)) <= now_ms)
    gate = _ms(*(t0 + timedelta(weeks=CONDOR_WEEKS_B)).timetuple()[:3], 8)
    b |= {"n": weeks, "counters": {f"п'ятниць з {CONDOR_WEEKS_B}": weeks}}
    if now_ms < gate:
        b["notes"].append(
            f"заборона підглядання до {datetime.fromtimestamp(gate / 1000, UTC):%Y-%m-%d %H:%M} UTC (T0 + 52 тижні): лише лічильники"
        )
    else:
        b["verdict"] = OUTSIDE
        b["notes"].append("вердикт один раз — окремим скриптом за реєстрацією 8j")
    return [a, b]


# ------------------------------------------------------------------------------------------------ assistant_paper

ASSISTANT_NAME = "Папір assistant_paper (опціонні рекомендації методу)"
GAP_MIN_S = 86_400  # an uncovered stretch longer than a day = at least one daily decision without a standing trade


def _uncovered(spans: Sequence[tuple[int, int]], start: int, end: int) -> list[int]:
    """Lengths (s) of the stretches of [start, end] covered by no span [opened, expiry)."""
    out, cur = [], start
    for o, e in sorted(spans):
        if o > cur:
            out.append(min(o, end) - cur)
        cur = max(cur, e)
        if cur >= end:
            return out
    return [*out, end - cur]


def assistant_paper_forward(rows: Sequence[Mapping[str, Any]], state: Mapping[str, Any] | None, now_ms: int) -> dict[str, Any]:
    """The collector job ``assistant_paper`` (``rec_paper`` rows, ``state`` = its parsed state file or None): counters
    and state only, no P&L. ``rows`` need ``opened_ts``, ``expiry_ts`` (unix s), ``status``, ``source``, ``trigger``.
    CHECKPOINT registers no band for it, so there is no verdict either."""
    now_s = now_ms // 1000
    mine = [r for r in rows if (o := _num(r.get("opened_ts"))) is not None and o <= now_s and _num(r.get("expiry_ts")) is not None]
    meth = [r for r in mine if r.get("source") == "method"]
    chain = sum(1 for r in meth if r.get("trigger") in (None, "chain"))
    expired = [r for r in meth if int(r["expiry_ts"]) <= now_s]
    unsettled = sum(1 for r in expired if r.get("status") != "settled")
    c: dict[str, Any] = {
        "угод методу": len(meth),
        "з них ланцюг (chain, порівнянні з H1) / за тригером (regime, event)": f"{chain} / {len(meth) - chain}",
        "ручних (manual, поза форвардом)": len(mine) - len(meth),
        "чинних (до експірації)": len(meth) - len(expired),
        "експірованих / з них ще не розраховано": f"{len(expired)} / {unsettled}",
    }
    row = _row("assistant:paper", "assistant", ASSISTANT_NAME, n=len(meth), verdict=COUNTS_ONLY, counters=c)
    row["metric_name"] = "лише лічильники й стан"
    notes: list[str] = row["notes"]
    if meth:
        first, last = min(int(r["opened_ts"]) for r in meth), max(int(r["opened_ts"]) for r in meth)
        c["перша / остання угода методу"] = f"{_ymd_s(first)} / {_ymd_s(last)}"
        gaps = [g for g in _uncovered([(int(r["opened_ts"]), int(r["expiry_ts"])) for r in meth], first, now_s) if g > GAP_MIN_S]
        c["проміжків без чинної угоди методу > 1 доби"] = f"{len(gaps)} (разом {sum(gaps) / 86_400:.1f} дн.)"
    if unsettled:
        notes.append("експіровані угоди розраховує лише виклик assistant_paper_status (завдання їх не закриває)")
    asof = _asof_utc(state)
    if asof is None:
        notes.append("файлу стану завдання немає або він нечитабельний: денних рішень не видно (завдання вимкнене або не запускалось)")
    elif asof > now_ms:
        notes.append("файл стану новіший за дату звіту: його рішення не показано")
    else:
        st = state or {}
        age = (now_ms - asof) / 3_600_000
        c["останнє денне рішення (файл стану)"] = f"{_ymd_s(asof // 1000)}, {st.get('state')} / {st.get('action')}, {age:.0f} год тому"
        if age > PAPER_MAX_AGE_HOURS:
            notes.append(f"останнє рішення старше {PAPER_MAX_AGE_HOURS:.0f} год: завдання, схоже, стоїть")
    notes.append(
        "смуги в CHECKPOINT немає, вердикту теж; метод не показав переваги (H1, звіт 18.10), тож це гіпотеза. "
        "P&L тут не друкується (лише assistant_paper_status). Проміжок без угоди буває й законним: метод того дня нічого не обрав"
    )
    return row


def _ymd_s(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _asof_utc(state: Mapping[str, Any] | None) -> int | None:
    """The state file's ``asof_utc`` in UTC ms, or None when absent or unreadable."""
    if not isinstance(state, Mapping) or not isinstance(state.get("asof_utc"), str):
        return None
    try:
        return int(datetime.strptime(state["asof_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp() * 1000)
    except ValueError:
        return None


# ------------------------------------------------------------------------------------- forward cards (idea: tags)

IDEAS_NAME = "Картки forward: плани за тегами idea:"
IDEAS_FIRST_READ = 30  # docs/HANDOFF.md, «Режим „план угоди“»: the first reading of plan results after ≈ 30 «enter» plans
_STATUS = re.compile(r"^Статус:\s*([a-z_-]+)", re.MULTILINE)
_IDEA_TAG = re.compile(r"`(idea:[a-z0-9][a-z0-9-]*)`")
_CARD_FILE = re.compile(r"^IDEA-\d{8}-(.+)\.md$")


def forward_cards(texts: Mapping[str, str]) -> list[dict[str, Any]]:
    """The idea cards whose first status line is ``Статус: forward`` (``texts``: file name -> card text), sorted by
    slug, each with its tag set: ``idea:<slug>`` (the file name without ``IDEA-YYYYMMDD-``, HANDOFF «Вердикт критика
    forward») plus every literal `idea:…` tag the card names (some cards register a shorter one)."""
    out = []
    for fname, text in texts.items():
        m, st = _CARD_FILE.match(fname), _STATUS.search(text)
        if m is None or st is None or st.group(1) != "forward":
            continue
        out.append({"slug": m.group(1), "tags": frozenset({f"idea:{m.group(1)}", *_IDEA_TAG.findall(text)})})
    return sorted(out, key=lambda c: c["slug"])


def _tags(x: Any) -> set[str]:
    return {t for t in x if isinstance(t, str)} if isinstance(x, (list, tuple, set, frozenset)) else set()


def idea_plans_forward(cards: Sequence[Mapping[str, Any]], plans: Sequence[Mapping[str, Any]], now_ms: int) -> dict[str, Any]:
    """The cards with status ``forward`` are judged by plans tagged ``idea:<slug>`` (``trade_plan``): counters only, one
    line per card, so the report names every such card even with no plan. ``plans`` need ``created_ts``,
    ``resolved_ts`` (unix s), ``verdict``, ``status`` and ``tags`` (parsed list); no R / outcome is read."""
    now_s = now_ms // 1000
    mine = [(p, _tags(p.get("tags"))) for p in plans if (c := _num(p.get("created_ts"))) is not None and c <= now_s]
    known = frozenset().union(*(c["tags"] for c in cards)) if cards else frozenset()
    counters: dict[str, Any] = {}
    for card in cards:
        hit = [p for p, t in mine if t & card["tags"]]
        enter = sum(1 for p in hit if p.get("verdict") == "enter")
        done = sum(1 for p in hit if p.get("status") == "resolved" and (r := _num(p.get("resolved_ts"))) is not None and r <= now_s)
        counters[card["slug"]] = f"планів {len(hit)}, «входити» {enter}, оцінено {done}"
    other = sum(1 for _, t in mine if any(x.startswith("idea:") for x in t - known) and not t & known)
    if other:
        counters["планів з тегом idea: без картки forward"] = other
    hit_any = [p for p, t in mine if t & known]  # a plan tagged for two cards counts once here
    enter_all = sum(1 for p in hit_any if p.get("verdict") == "enter")
    row = _row("ideas:plans", "ideas", IDEAS_NAME, n=len(hit_any), verdict=COUNTS_ONLY, counters=counters)
    row["metric_name"] = f"лише лічильники, карток {len(cards)}"
    notes: list[str] = row["notes"]
    if not cards:
        notes.append("карток зі статусом forward не знайдено (каталог docs/ideas не прочитано?)")
    got = "набрано" if enter_all >= IDEAS_FIRST_READ else "не набрано"
    notes.append(
        f"мінімум для першого читання результатів планів: ≈ {IDEAS_FIRST_READ} планів «входити» (HANDOFF, режим «план угоди»); "
        f"за тегами карток forward {enter_all} — {got}"
    )
    notes.append(
        "картки форвард-паперу carry і Polymarket мають і власні рядки вище; тут лише їхні плани. "
        "Дата вердикту й мінімум n кожної картки — у її розділі «Форвард» (правила різні, тут не дублюються). R планів тут "
        "не читається (лише plan_review). Власні журнали колекторів карток (знімки HL, CoinGecko, option_xvenue тощо) "
        "scripts/checkpoint_report.py рахує окремим рядком лише кількістю спостережень: події в них визначає крок аналітика картки"
    )
    return row


# ---------------------------------------------------------------------------------------------------- the report


def build(inputs: Mapping[str, Any], now_ms: int) -> dict[str, Any]:
    """Every forward at ``now_ms``. ``inputs``: ``demo_trades`` (demo_trade rows), ``carry`` {strategy: {positions,
    logs, cycles}}, ``bench`` {strategy: bench}, ``side_cost_a``, ``decision_delay_ms``, ``pm_rows``, ``pm_outcomes``,
    ``condor_t0`` (date or None), ``condor_rows``, ``rec_paper`` (rec_paper rows, no P&L columns needed),
    ``paper_state`` (the assistant_paper state file, parsed, or None), ``idea_cards`` (``forward_cards`` output),
    ``plans`` (trade_plan rows without R / outcome, ``tags`` parsed), ``ladder`` (dated Bybit ladder snapshots for
    carry (ye), see ``carry_capacity``), ``errors`` (strings from the loader)."""
    out: list[dict[str, Any]] = []
    trades = inputs.get("demo_trades") or []
    for spec in DEMO:
        out += demo_forward(spec, trades, now_ms)
    carry = inputs.get("carry") or {}
    for s in ("A", "B"):
        c = carry.get(s) or {}
        out.append(
            carry_forward(
                s,
                c.get("positions", []),
                c.get("logs", {}),
                c.get("cycles", []),
                (inputs.get("bench") or {}).get(s, {"buckets": []}),
                now_ms,
                ladder=inputs.get("ladder") or (),
            )
        )
    a = carry.get("A") or {}
    out += a_v2_forward(
        a.get("positions", []), (a.get("logs") or {}).get("funding", []), (a.get("logs") or {}).get("mark", []), now_ms,
        side_cost=float(inputs.get("side_cost_a") or 0.0), decision_delay_ms=int(inputs.get("decision_delay_ms") or 0),
    )  # fmt: skip
    out += polymarket_forward(inputs.get("pm_rows") or [], inputs.get("pm_outcomes") or [], now_ms)
    out += condor_forward(inputs.get("condor_t0"), inputs.get("condor_rows") or [], now_ms)
    out.append(assistant_paper_forward(inputs.get("rec_paper") or [], inputs.get("paper_state"), now_ms))
    out.append(idea_plans_forward(inputs.get("idea_cards") or [], inputs.get("plans") or [], now_ms))
    demo_no = [r for r in out if r["group"] == "demo" and not r["key"].endswith(":fill") and r["verdict"] == NO]
    notes = list(inputs.get("errors") or [])
    for side in ("нижче", "вище"):
        k = sum(1 for r in demo_no if r["side"] == side)
        if k >= 3:
            notes.append(f"«ні» {side} смуги дають {k} сетапів демо: ознака системної причини (виконання, витрати)")
    stuck = stuck_trades(trades, now_ms)
    if stuck:
        notes.append(
            f"завислі демо-угоди (open довше за строк утримання + 1 доба): {len(stuck)} — "
            + ", ".join(f"{t['setup']} {t['symbol']}" for t in stuck)
            + "; вони не входять у закриті вибірки, тож n демо неповне, доки їх не закрито"
        )
    bad_pnl = pnl_mismatches(trades)
    if bad_pnl:
        notes.append(
            f"Угоди з розбіжним P&L: {len(bad_pnl)} — "
            + ", ".join(f"{t['setup']} {t['symbol']} ({t['recorded']:+.2f} проти {t['expected']:+.2f} USD)" for t in bad_pnl)
            + "; P&L у БД не збігається з (вихід − вхід)·qty − комісії, схоже на часткове закриття: ціну й причину виходу перевірити"
        )
    return {"now_ms": now_ms, "forwards": out, "notes": notes, "s12_stop_rule": s12_stop_rule(trades, now_ms)}


# ---------------------------------------------------------------------------------------------------------- text


def _f(x: Any, nd: int = 3, sign: bool = False) -> str:
    v = _num(x)
    return "—" if v is None else (f"{v:+.{nd}f}" if sign else f"{v:.{nd}f}")


def _pc(x: Any) -> str:
    v = _num(x)
    return "—" if v is None else f"{v * 100:.0f} %"


def _band_text(r: Mapping[str, Any]) -> str:
    b = r.get("band")
    if not isinstance(b, Mapping):
        return "—"
    if "iid" in b:
        (dl, dh), (il, ih) = b["decision"], b["iid"]
        return f"n′ {b['n_ref']}: рішення {dl:+.3f} … {dh:+.3f} (i.i.d. {il:+.3f} … {ih:+.3f})"
    if "passport" in b:
        lo, hi = b["wilson"]
        return f"паспорт {b['passport'] * 100:.1f} %; 95 % CI демо {_pc(lo)} … {_pc(hi)}"
    if b.get("unit") == "pp":
        lo, hi = b["decision"]
        return f"n′ {b['n_ref']}: {lo:+.1f} … {hi:+.1f} п.п."
    if "ci" in b:
        lo, hi = b["ci"]
        return f"95 % CI {_f(lo, 2)} … {_f(hi, 2)} відносно 1"
    return "—"


def _metric_text(r: Mapping[str, Any]) -> str:
    if r["embargo"]:
        return "закрито"
    m = r.get("metric")
    if m is None:
        return "—"
    if r["key"].endswith(":fill"):
        return _pc(m)
    if r["key"] == "carry:A2:2":
        return f"{m:+.1f} п.п."
    if r["key"] == "condor:a":
        return _f(m, 2)
    return _f(m, 3, sign=True)


def render(rep: Mapping[str, Any]) -> str:
    """The Ukrainian text for the owner: one table row per forward, then the details of each."""
    now = datetime.fromtimestamp(rep["now_ms"] / 1000, UTC)
    L = [
        f"# Контрольна точка 30.11: стан форвардів на {now:%Y-%m-%d %H:%M} UTC",
        "",
        "Критерії й смуги — docs/CHECKPOINT-2026-11-30.md, розділ «Критерій „узгоджується з бектестом“». «Узгоджується» "
        "не означає «заробляє»: майже всі еталони самі без переваги. Форварди під забороною підглядання показано лише "
        "лічильниками. БД прочитано лише на читання, без мережі.",
        "",
        "| Форвард | n | Метрика | Смуга бектесту | Вердикт |",
        "|---|---|---|---|---|",
    ]
    for r in rep["forwards"]:
        verdict = r["verdict"] + (f" ({r['side']})" if r.get("side") else "")
        n = "—" if r.get("n") is None else str(r["n"])
        L.append(f"| {r['name']} | {n} | {_metric_text(r)} | {_band_text(r)} | {verdict} |")
    L += ["", "## Подробиці", ""]
    for r in rep["forwards"]:
        lines = [f"- {k}: {v}" for k, v in r["counters"].items()]
        lines += [f"- ({c['id']}) {c['status']}: {c['text']}" for c in r["criteria"]]
        lines += [f"- {x}" for x in r["notes"]]
        if lines:
            L += [f"**{r['name']}** ({r['metric_name']})", *lines, ""]
    if rep.get("s12_stop_rule"):
        L += s12_stop_rule_lines(rep["s12_stop_rule"])
    L += [
        "Чого смуги демо не моделюють: денних слотів демо і того, що демо бере з кандидатів не випадкову підмножину. "
        "Сетапів 9: навіть за поведінки точно як у паспорті ймовірність, що хоч один вийде за свою i.i.d.-смугу, ≈ 60 %, "
        "тож одне «ні» — привід для розбору, а не висновок. Друга метрика демо (r_demo_bt) і проковзування — "
        "scripts/demo_vs_backtest.py.",
    ]
    L += [f"Примітка: {x}" for x in rep.get("notes") or []]
    return "\n".join(L) + "\n"
