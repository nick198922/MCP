"""S38 demo vs its passport (IDEA-20261030-demo-setups-depth, items 6 and 9; the registration goes into the card and is
committed in its own run before ``--stage run``, owner rule 03.11).

    uv run python scripts/verify_s38_demo_vs_passport.py --stage smoke [--cutoff YYYY-MM-DDTHH:MM]  # counts, no outcome
    uv run python scripts/verify_s38_demo_vs_passport.py --stage run --cutoff YYYY-MM-DDTHH:MM      # the registered run

The closed S38 demo trades (``demo_trade``, read over a ``mode=ro`` connection: nothing is written) closed before
``--cutoff`` are grouped by their ``version`` tag (untagged: the epoch of the open time, ``s12_demo_audit.traded_version``
works for any setup) and set against the S38 passport (``docs/verification/2026-10-30-passport-s38-s39-s40.md``). Per
trade: ``analytics/demo_vs_backtest.replay`` with the live S38 detector on Bybit candles (``scripts/demo_vs_backtest.py``
paged fetch, cached through the S12 script's ``cached`` / ``cached_day``), the entry day's Bybit turnover (the passport
does not exclude a handicap up to about 0.35R for shorts on $1-5M coins, so that group gets its own row; the row
"без $1–5M" is every other S38 trade: >= $5M + < $1M + unknown turnover), fees and exit
slippage against the backtest convention (``s12_demo_audit.exec_costs``), orphaned trades (filled, no close found) with
the scenario «each closed at the initial stop, -1R», missed entries and the shadow plans (``analytics/demo_shadow``).
CI: bootstrap clustered by the UTC day of the signal bar (``liquidation_flush.cluster_bootstrap_ci`` through the S12
script's ``stat``). R is per trade risk, not on capital. The passport bands are defined for n = 30: a group with fewer
trades gets no band verdict (✓ / ≈ / ✗), only the neutral line "n < 30: смуга паспорта не застосовна".

Not modelled here: the demo's cap of 2 S38 entries per 4h bar. The passport models it (``BAR_CAP``); the per-trade
replay takes each closed trade alone and cannot see which signals the cap (or the daily slots) turned away.
The smoke stage prints counts and data availability only, never a mean R or any other outcome of the comparison.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import common_helpers as cmn  # scripts/common_helpers.py (the script's directory is on sys.path)
import demo_vs_backtest as dvb  # scripts/demo_vs_backtest.py: read-only loaders, paged venue fetch, turnover
import verify_s12_demo_vs_passport as v12  # loader by status, candle caches, clustered stat and its format

from tradedesk.analytics import demo_shadow
from tradedesk.analytics import s12_demo_audit as audit
from tradedesk.analytics.demo_vs_backtest import TURNOVER_GROUPS, Replay
from tradedesk.config import Settings
from tradedesk.core.envelope import Builder
from tradedesk.runtime import Runtime

SETUP = "S38"
DEFAULT_CACHE = "~/.cache/tradedesk-agents/s38-demo-vs-passport"
BAR_CAP = 2  # the demo's limit of S38 entries per 4h signal bar (setup_versions v5)
BAND_N = 30  # the passport bands are defined for 30 trades: no band verdict for a smaller group
PASSPORT_DOC = "docs/verification/2026-10-30-passport-s38-s39-s40.md"
# S38 passport, verdict sample (top-250 and >= $1M, demo filters, cap of 2 per bar, rule B, OOS >= 2025-03): line 15 of
# PASSPORT_DOC (OOS -0.032 [-0.144; +0.084], n 1012; the n = 30 bands 5 / 50 / 95: i.i.d. -0.372 / -0.035 / +0.335,
# sliding -0.535 / -0.086 / +0.576); the same OOS mean and CI on lines 10 and 27.
PASSPORT = audit.Passport(
    "пробій рейнджу вниз і ретест, шорт, 4h; вхід за закриттям сигнальної свічки, ліміт 2 входи на свічку",
    -0.032,
    (-0.144, 0.084),
    (-0.372, -0.035, 0.335),
    (-0.535, -0.086, 0.576),
    None,
    f"{PASSPORT_DOC}:15",
)
PASSPORT_OF = {"v5": "S38"}  # the only S38 version so far (setup_versions v5); another tag has no passport here
# Turnover rows of the same report (line 38: OOS by group; line 9 / 99: the handicap for shorts on $1-5M coins)
TURNOVER_REF = {
    "1to5m": "паспорт OOS −0.162 (n 110, рядок 38); контраст $1–5M − решта OOS −0.146 [−0.395; +0.110] (рядок 9); гандикап шортів до ≈ 0.35R не виключено (рядки 9, 99)",
    "ge5m": "паспорт OOS −0.031 (ge5m), top30 +0.057 (рядок 38)",
}


def open_sources(venue: str, cache: Path) -> tuple[Any, Builder, dvb.Fetch, dvb.DayFetch]:
    """(runtime to close, builder with the failures, cached candle fetch, cached Bybit daily fetch); tests replace it."""
    rt = Runtime(dataclasses.replace(Settings.from_env(), db_path=""))
    b = Builder()
    return rt, b, v12.cached(dvb.venue_fetch(rt, b, venue), cache, venue), v12.cached_day(dvb.bybit_day_fetch(rt, b), cache)


def classify(rows: list[dict[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        v, src = audit.traded_version(r, now_ms=now_ms)
        out.append({**r, "ver": v, "ver_src": src, "passport": PASSPORT_OF.get(v)})
    return out


def ver_name(ver: str, src: str) -> str:
    return f"{ver} (тег)" if src == "column" else f"{ver} (без тегу, за {src})"


async def replay_with_turnover(rows: list[dict[str, Any]], fetch: dvb.Fetch, day: dvb.DayFetch, now_ms: int) -> list[Replay]:
    reps = await dvb.replay_all(rows, fetch, now_ms)
    return await dvb.add_turnover(rows, reps, day, now_ms)


def replay_rows(rows: list[dict[str, Any]], venue: str, cache: Path, now_ms: int) -> tuple[list[Replay], Builder]:
    rt, b, fetch, day = open_sources(venue, cache)

    async def go() -> list[Replay]:
        try:
            return await replay_with_turnover(rows, fetch, day, now_ms)
        finally:
            await rt.aclose()

    return asyncio.run(go()), b


def per_bar(rows: list[dict[str, Any]]) -> Counter[int]:
    """{entries on one 4h signal bar: number of bars} over filled trades (closed, open, orphaned)."""
    return Counter(Counter(int(r["signal_ts"]) for r in rows).values())


def smoke(db: str, cutoff_ms: int, cache: Path, venue: str, now_ms: int | None = None) -> int:
    """Counts, version tags, units and data availability only: no mean, sum, share or verdict of any R."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    g = v12.load_s12(db, cutoff_ms, SETUP)
    closed = classify(g["closed"], cutoff_ms)
    print(f"S38, cutoff {datetime.fromtimestamp(cutoff_ms / 1000, UTC):%Y-%m-%d %H:%M} UTC")
    print(
        f"closed < cutoff: {len(closed)}; missed {len(g['missed'])}; open {len(g['open'])}; "
        f"orphaned {len(g['orphaned'])}; other {len(g['other'])}"
    )
    print(f"versions (closed): {sorted(Counter((r['ver'], r['ver_src']) for r in closed).items())}")
    for st in ("missed", "open", "orphaned"):
        print(f"versions ({st}): {sorted(Counter((r['ver'], r['ver_src']) for r in classify(g[st], cutoff_ms)).items())}")
    print(f"tags without a passport here (closed): {sorted({r['ver'] for r in closed if r['passport'] is None})}")
    if closed:
        days = sorted({cmn.ymd_ms(int(r["signal_ts"])) for r in closed})
        print(f"signal days {len(days)} ({days[0]} .. {days[-1]}); symbols {len({r['symbol'] for r in closed})}")
    print(f"timeframes {dict(Counter(r['timeframe'] for r in closed))}; venues {dict(Counter(r['venue'] for r in closed))}")
    print(f"sides {dict(Counter(r['side'] for r in closed))}; entry kinds {dict(Counter(str(r.get('entry_kind')) for r in closed))}")
    filled = closed + g["open"] + g["orphaned"]
    pb = per_bar(filled)
    print(
        f"filled entries per 4h signal bar (closed + open + orphaned): {sorted(pb.items())}; bars above the cap of {BAR_CAP}: "
        f"{sum(v for k, v in pb.items() if k > BAR_CAP)} (the cap is not modelled in the per-trade replay)"
    )
    miss = [
        k
        for k in ("qty", "risk_usd", "fees_usd", "fill", "exit", "exit_reason", "r_multiple", "pnl_usd", "opened_ts")
        if any(r.get(k) is None for r in closed)
    ]
    print(f"columns with a NULL in some closed row: {miss}")
    unit = [
        abs(float(r["pnl_usd"]) / float(r["risk_usd"]) - float(r["r_multiple"]))
        for r in closed
        if r.get("risk_usd") and r.get("pnl_usd") is not None and r.get("r_multiple") is not None
    ]
    print(f"max |pnl/risk - r_multiple| {max(unit, default=float('nan')):.2e} (units check)")
    print(f"orphaned without an open time: {sum(r.get('opened_ts') is None for r in g['orphaned'])}")
    t0 = time.monotonic()
    reps, b = replay_rows(closed, venue, cache, now)
    avail = Counter("candles and 1h path" if x.verdict not in ("no_data", "unsupported", "pending") else x.verdict for x in reps)
    print(f"replay data (no verdicts, no R): {dict(avail)}; {time.monotonic() - t0:.1f} s")
    print(f"entry-day turnover (Bybit daily candle): { {k: sum(x.turnover_group == k for x in reps) for k in TURNOVER_GROUPS} }")
    print(f"missing: {[f'{m.field}: {m.reason}' for m in b.missing[:5]]}; peak RSS {cmn.rss_mb():.0f} MB")
    return 0


def band_tail(s: dict[str, Any]) -> str:
    """`` -> <band verdict>`` for a group of at least BAND_N trades, else empty: the passport band is defined for n = 30."""
    return f" -> {audit.band_verdict(s['mean'], PASSPORT)}" if s.get("n", 0) >= BAND_N else ""


def passport_lines(s: dict[str, Any]) -> list[str]:
    """The comparison of one group's stat with the S38 passport (band of 30, rescaled band, demo minus OOS).

    The band verdict (✓ / ≈ / ✗) is printed only for n >= BAND_N; a smaller group gets a neutral line instead."""
    pp = PASSPORT
    lo, hi = audit.scaled_band(pp, s["n"])
    ci = s.get("ci")
    diff = f"{s['mean'] - pp.oos_mean:+.3f} [{ci[0] - pp.oos_mean:+.3f}; {ci[1] - pp.oos_mean:+.3f}]" if ci else "—"
    out = [
        f"    паспорт S38 OOS {pp.oos_mean:+.3f} [{pp.oos_ci[0]:+.3f}; {pp.oos_ci[1]:+.3f}]; смуга n=30 i.i.d. [{pp.band_iid[0]:+.3f}; "
        f"{pp.band_iid[2]:+.3f}], ковзна [{pp.band_sliding[0]:+.3f}; {pp.band_sliding[2]:+.3f}]{band_tail(s)}; "
        f"на n={s['n']} (описово) [{lo:+.3f}; {hi:+.3f}]; демо − OOS {diff}"
    ]
    if s["n"] < BAND_N:
        out.append(f"    n < {BAND_N}: смуга паспорта не застосовна")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", choices=("smoke", "run"), required=True)
    ap.add_argument("--db", default=dvb.DEFAULT_DB)
    ap.add_argument(
        "--cutoff", help="UTC YYYY-MM-DDTHH:MM; trades closed before it (required for run: the registered minute; smoke: this hour)"
    )
    ap.add_argument("--venue", default="bybit")
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE)
    ap.add_argument("--out-tag", default="s38", help="output names <tag>.txt / <tag>_rows.json in the cache dir")
    a = ap.parse_args(argv)
    if a.cutoff is None:
        if a.stage == "run":
            ap.error("--stage run needs --cutoff (the minute written in the registration)")
        cutoff_ms = int(time.time() * 1000) // v12.HOUR_MS * v12.HOUR_MS
    else:
        cutoff_ms = int(datetime.strptime(a.cutoff, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC).timestamp() * 1000)
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "smoke":
        return smoke(a.db, cutoff_ms, cache, a.venue)
    return run(a.db, cutoff_ms, cache, a.venue, a.out_tag)


def run(db: str, cutoff_ms: int, cache: Path, venue: str, tag: str = "s38", now_ms: int | None = None) -> int:
    """The registered comparison: prints the report and writes ``<tag>.txt`` and ``<tag>_rows.json`` to ``cache``."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    g = v12.load_s12(db, cutoff_ms, SETUP)
    closed = classify(g["closed"], cutoff_ms)
    missed = classify(g["missed"], cutoff_ms)
    opened = classify(g["open"], cutoff_ms)
    orph = classify(g["orphaned"], cutoff_ms)
    reps, b = replay_rows(closed, venue, cache, now)
    rows: list[dict[str, Any]] = []
    for r, rp in zip(closed, reps, strict=True):
        d = {**rp.as_dict(), **{k: r.get(k) for k in ("ver", "ver_src", "passport", "entry_kind", "exit_reason", "opened_ts", "closed_ts")}}
        d.update(audit.exec_costs(r))
        rows.append(d)
    stat, fmt = v12.stat, v12.fmt
    L: list[str] = []
    p = L.append
    p(
        f"# S38: демо проти паспорта; cutoff {datetime.fromtimestamp(cutoff_ms / 1000, UTC):%Y-%m-%d %H:%M} UTC; "
        f"повтор {datetime.fromtimestamp(now / 1000, UTC):%Y-%m-%d %H:%M} UTC, venue {venue}"
    )
    p(
        f"закритих {len(rows)}; версії {sorted(Counter((r['ver'], r['ver_src']) for r in rows).items())}; missed {len(missed)}; "
        f"open {len(opened)}; orphaned {len(orph)}; other {len(g['other'])}"
    )
    p(f"сигнальні дні: {len({cmn.ymd_ms(int(r['signal_ts'])) for r in rows})}; монет {len({r['symbol'] for r in rows})}")
    p(f"паспорт: {PASSPORT.source} ({PASSPORT.entry})")
    p("")
    p("## Пункт 6: R демо (од. демо, на ризик угоди, не на капітал) проти паспорта S38")
    groups: dict[str, Any] = {}
    for ver, src in sorted({(r["ver"], r["ver_src"]) for r in rows}):
        groups[ver_name(ver, src)] = lambda r, ver=ver, src=src: r["ver"] == ver and r["ver_src"] == src
    for ver in sorted({r["ver"] for r in rows}):
        if len({r["ver_src"] for r in rows if r["ver"] == ver}) > 1:
            groups[f"{ver} разом (тег + без тегу)"] = lambda r, ver=ver: r["ver"] == ver
    groups[f"усі {len(rows)}"] = lambda r: True
    for name, f in groups.items():
        sub = [r for r in rows if f(r)]
        s = stat(sub, "r_demo")
        p(f"- {name:<36} {fmt(s)}")
        pk = {r["passport"] for r in sub}
        if pk == {"S38"} and s.get("n"):
            L.extend(passport_lines(s))
        elif s.get("n"):
            p(f"    паспорта для версій {sorted(str(x) for x in pk - {'S38'})} тут немає: з паспортом S38 не звіряю")
    # orphaned (filled, no close found on the venue): not in the sample; scenario «each closed at the initial stop, −1R»,
    # as in docs/changes/2026-11-12-checkpoint-orphan-stop-scenario.md, descriptive only
    for name, f in groups.items():
        oo = [dict(r, r_demo=-1.0) for r in orph if f(r)]
        if not oo:
            p(f"- orphaned {name}: 0")
            continue
        s2 = stat([r for r in rows if f(r)] + oo, "r_demo")
        tail = ""
        if {r["passport"] for r in oo} == {"S38"} and s2.get("n"):
            tail = band_tail(s2) or f"; n < {BAND_N}: смуга паспорта не застосовна"
        p(f"- orphaned {name}: {len(oo)}; сценарій «закрито за стопом, −1R» (описово): {fmt(s2)}{tail}")
    p("")
    s38 = [r for r in rows if r["passport"] == "S38"]
    p("## Оборот монети в день входу (денна свічка Bybit, UTC), угоди з паспортом S38")
    for k, label in (("ge5m", "≥ $5M"), ("1to5m", "$1–5M"), ("lt1m", "< $1M"), ("unknown", "невідомий")):
        s = stat([r for r in s38 if r.get("turnover_group") == k], "r_demo")
        p(f"- оборот {label:<10} {fmt(s)}")
        if k in TURNOVER_REF:
            p(f"    {TURNOVER_REF[k]}")
    # "без $1–5M" = every S38 trade outside the $1-5M group: ge5m + lt1m + unknown turnover (no Bybit daily candle)
    no15 = stat([r for r in s38 if r.get("turnover_group") != "1to5m"], "r_demo")
    p(f"- без $1–5M (= ≥ $5M + < $1M + невідомий; описово)  {fmt(no15)}")
    if no15.get("n"):
        L.extend(passport_lines(no15))
    m15, m5 = (
        stat([r for r in s38 if r.get("turnover_group") == "1to5m"], "r_demo"),
        stat([r for r in s38 if r.get("turnover_group") == "ge5m"], "r_demo"),
    )
    if m15.get("n") and m5.get("n"):
        p(f"- різниця середніх $1–5M − ≥ $5M (точково, описово): {m15['mean'] - m5['mean']:+.3f}")
    p("")
    p("## Стійкість і розбивки (описово; R демо, угоди з паспортом S38)")
    top = sorted(s38, key=lambda r: r["r_demo"] or 0.0, reverse=True)
    for k in (3, 5):
        p(f"- без топ-{k}: {fmt(stat(top[k:], 'r_demo'))}")
    for k in ("maker", "taker"):
        p(f"- вхід {k:<6} {fmt(stat([r for r in s38 if r.get('entry_kind') == k], 'r_demo'))}")
    p(f"- угод на монету: {sorted(Counter(Counter(r['symbol'] for r in s38).values()).items())} (к-сть угод: монет)")
    pb = per_bar(rows + opened + orph)
    p(
        f"- входів на 4h-свічку сигналу (закриті + відкриті + orphaned): {sorted(pb.items())}; свічок понад ліміт {BAR_CAP}: "
        f"{sum(v for k, v in pb.items() if k > BAR_CAP)}"
    )
    p("")
    p("## Пункт 9: демо проти бектесту по угодах (угоди з паспортом S38)")
    p(f"вердикти повтору: {sorted(Counter((r['ver'], r['verdict']) for r in rows).items())}")
    p(f"причини розбіжностей: {dict(Counter(x for r in s38 for x in r.get('reasons') or []))}")
    p(f"вхід демо проти закриття свічки (entry_edge_r, + = демо краще) {fmt(stat(s38, 'entry_edge_r'))}")
    p(f"  з них план (plan_edge_r)                                    {fmt(stat(s38, 'plan_edge_r'))}")
    ex = [
        dict(r, exec_r=r["entry_edge_r"] - r["plan_edge_r"])
        for r in s38
        if r.get("entry_edge_r") is not None and r.get("plan_edge_r") is not None
    ]
    p(f"  виконання (entry − plan)                                    {fmt(stat(ex, 'exec_r'))}")
    for k in ("maker", "taker"):
        p(
            f"    виконання, вхід {k:<5}                                    {fmt(stat([r for r in ex if r.get('entry_kind') == k], 'exec_r'))}"
        )
    dmax = lambda key, rs: max((abs(r[key]) for r in rs if r.get(key) is not None), default=float("nan"))  # noqa: E731
    p(f"|Δ стопу| R макс {dmax('stop_diff_r', s38):.3f}; |Δ цілі| R макс {dmax('target_diff_r', s38):.3f}")
    p(f"r_diff (демо − бектест B, од. бектесту) {fmt(stat(s38, 'r_diff'))}")
    p(f"R бектесту B на тих самих угодах {fmt(stat(s38, 'r_bt'))}; Bx {fmt(stat(s38, 'r_bt_fast'))}")
    p(f"R бектесту від заповнення демо {fmt(stat(s38, 'r_bt_fill'))}")
    p(f"виходи демо / бектест: {sorted(Counter((str(r.get('demo_exit')), str(r.get('bt_exit'))) for r in s38).items())}")
    p(f"комісії демо, R {fmt(stat(s38, 'fee_demo_r'))}")
    p(f"комісії моделі бектесту, R {fmt(stat(s38, 'fee_model_r'))}; з фактичним видом входу {fmt(stat(s38, 'fee_entry_kind_r'))}")
    p(f"модель − демо (+ = демо заплатило менше) {fmt(stat(s38, 'fee_diff_r'))}")
    for k in ("stop", "breakeven", "target"):
        sub = [r for r in s38 if r.get("exit_reason") == k]
        p(f"проковзування виходу {k:<9} демо {fmt(stat(sub, 'exit_slip_r'))}")
        p(f"      модель {fmt(stat(sub, 'model_slip_r'))}; демо − модель {fmt(stat(sub, 'slip_diff_r'))}")
    p("")
    p("## Незаповнені входи (status missed) за версією; заповнено = закриті + відкриті + orphaned")
    for ver in sorted({r["ver"] for r in rows + opened + orph + missed}):
        nf = sum(r["ver"] == ver for r in rows + opened + orph)
        nm = sum(r["ver"] == ver for r in missed)
        share = f"{nf / (nf + nm):.0%}" if nf + nm else "—"
        p(f"- {ver}: заповнено {nf}, не заповнено {nm}, частка заповнення {share} (бектест вважає вхід завжди заповненим)")
    trades, aud, plans, ne_err = dvb.load_not_executed(db)
    trades = [t for t in trades if int(t.get("signal_ts") or 0) < cutoff_ms]
    aud = [x for x in aud if int(x.get("ts") or 0) < cutoff_ms]
    srows = [x for x in demo_shadow.match_rows(trades, aud, plans, now_ms=now) if x.setup == SETUP and x.decided_ts < cutoff_ms]
    p(f"## Невиконані сигнали S38 проти тіньових планів (R без беззбитку, од. демо; t-CI з demo_shadow){' — ' + ne_err if ne_err else ''}")
    if srows:
        sm = demo_shadow.summary(srows, now_ms=now)
        for gname, s in sm["groups"].items():
            ci = f"[{s['ci95_r'][0]:+.2f}; {s['ci95_r'][1]:+.2f}]" if s.get("ci95_r") else "[—]"
            mr = f"{s['mean_r']:+.3f}" if s.get("mean_r") is not None else "—"
            p(f"- {gname}: n {s['n']}, з R {s['n_r']}, відкрито {s['open']}, без плану {s['no_plan']}, середній {mr} {ci}")
            for rr in s.get("reasons", [])[:5]:
                mr2 = f"{rr['mean_r']:+.2f}" if rr.get("mean_r") is not None else "—"
                p(f"    · {rr['reason'][:60]}: n {rr['n']}, з R {rr['n_r']}, середній {mr2}")
    else:
        p("рядків немає")
    p("")
    p("## По угодах")
    p("дата | монета | версія | вхід | вихід демо/бт | R демо | R бт B | край входу R | комісії модель−демо R | вердикт | оборот")
    f2 = lambda x: "—" if x is None else f"{x:+.2f}"  # noqa: E731
    for r in rows:
        p(
            f"{cmn.ymd_ms(int(r['signal_ts']))} | {r['symbol'].split('/')[0]} | {ver_name(r['ver'], r['ver_src'])} | {r.get('entry_kind')} | "
            f"{r.get('demo_exit')}/{r.get('bt_exit')} | {f2(r.get('r_demo'))} | {f2(r.get('r_bt'))} | {f2(r.get('entry_edge_r'))} | "
            f"{f2(r.get('fee_diff_r'))} | {r.get('verdict')} {','.join(r.get('reasons') or [])} | {r.get('turnover_group')}"
        )
    p("")
    p(f"відкриті (не входять): {[(x['symbol'].split('/')[0], cmn.ymd_ms(int(x['signal_ts'])), x['ver']) for x in opened]}")
    p(f"orphaned (не входять, лише сценарій −1R): {[(x['symbol'].split('/')[0], cmn.ymd_ms(int(x['signal_ts'])), x['ver']) for x in orph]}")
    p(
        f"не змодельовано: ліміт демо {BAR_CAP} входи S38 на 4h-свічку (паспорт його моделює, {PASSPORT_DOC} рядки 5 і 11; повтор "
        "по угодах бере кожну угоду окремо і не бачить, які сигнали відкинув ліміт чи денні слоти)"
    )
    p("паспорт: свічки й оборот Binance, топ-250 і ≥ $1M, OOS з 2025-03; демо: Bybit, монети від $1M, оборот дня входу з Bybit")
    p(f"missing: {[f'{m.field}: {m.reason}' for m in b.missing[:10]]}; warnings {b.warnings[:5]}")
    p(f"seed {v12.SEED}; бутстреп {v12.N_BOOT} кластерами за днем сигналу; peak RSS {cmn.rss_mb():.0f} MB")
    text = "\n".join(L) + "\n"
    print(text, end="")
    (cache / f"{tag}.txt").write_text(text, encoding="utf-8")
    (cache / f"{tag}_rows.json").write_text(
        json.dumps({"generated_ms": now, "cutoff_ms": cutoff_ms, "rows": rows}, default=str, ensure_ascii=False), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
