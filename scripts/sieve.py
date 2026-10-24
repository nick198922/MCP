#!/usr/bin/env python3
"""Strategy sieve runner (IDEA-20261024-strategy-sieve, step 1): one spec from ``tradedesk.analytics.sieve_specs``
through the uniform test of ``analytics.strategy_sieve``, one row in the registry ``docs/sieve/registry.csv``.

  uv run python scripts/sieve.py s3_range_sweep --out ~/.cache/tradedesk-agents/sieve/s3.md
  uv run python scripts/sieve.py donchian_breakout --set n=55 --out ~/.cache/tradedesk-agents/sieve/donchian55.md
  uv run python scripts/sieve.py donchian_breakout --limit 5     # development: counts and causality only

Only spec modules of our package are imported (by name, after a static allow-list check; no paths, no foreign code).
No network: 1h bars and the point-in-time universe (BTC, ETH + top-30 Binance USD-M by volume each month, delisted
included) come from the step-1 cache (``--s1-cache``), listings and funding from the funding-crowding cache
(``--data-dir``); both are read, never written.

Every full-universe run is appended to the canonical registry (there is no option to skip it or to use another file);
the pass bar uses Holm over all its rows with at least 100 tests. The hidden period (>= 2026-03-01) is simulated and
shown only when the run passes IS and OOS and the strategy (its NAME or its spec content hash) has not opened it
before; the reveal is recorded in the same row. Everything is computed before the row is written and the report is
printed after it. The bootstrap size is ``strategy_sieve.N_BOOT`` (no option: it is covered by ``code_hash``).
``--limit N`` (at most 10 symbols) prints only signal and trade counts and the causality check: no E[R], no CI, no
verdict, no registry row.

Causality is checked in every worker, on every symbol and timeframe: stamps and entries of all signals, up to 13
signal times re-decided on the bars closing up to them, two cuts. A spec without a single checked signal is refused.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s1  # noqa: E402  (step-1 loaders, universe, membership)

from tradedesk.analytics import sieve_specs  # noqa: E402
from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars, resample  # noqa: E402
from tradedesk.analytics.strategy_sieve import (  # noqa: E402
    FUND_8H,
    HIDDEN_MS,
    IS_END_MS,
    MANAGEMENTS,
    MIN_EFFECT,
    N_BOOT,
    N_PLAN,
    PASS,
    PLACEBO_K,
    START_MS,
    Summary,
    causality_violations,
    holm_with_registry,
    mde_z,
    n_effective,
    p_threshold,
    revealed_before,
    simulate_signals,
    summarize,
    symbol_seed,
    verdict,
)

DEFAULT_REGISTRY = ROOT / "docs" / "sieve" / "registry.csv"  # the only registry (tests monkeypatch it)
LIMIT_MAX = 10
COLUMNS = [
    "run_id",
    "date",
    "commit",
    "code_hash",
    "spec_hash",
    "strategy",
    "params",
    "management",
    "timeframes",
    "symbols",
    "n_is",
    "dates_is",
    "mean_is",
    "lo_is",
    "hi_is",
    "p_is",
    "mde_is",
    "n_oos",
    "dates_oos",
    "mean_oos",
    "lo_oos",
    "hi_oos",
    "fund_is",
    "stress_is",
    "placebo_is",
    "placebo_oos",
    "vs_placebo_is",
    "vs_placebo_lo_is",
    "vs_placebo_hi_is",
    "p_vs_placebo_is",
    "vs_placebo_oos",
    "p_test",
    "n_registry",
    "n_effective",
    "p_holm",
    "outcome",
    "revealed",
    "n_hidden",
    "mean_hidden",
    "lo_hidden",
    "hi_hidden",
]
# causality check per symbol and timeframe: signal times re-decided on a prefix (random sample + the last ones)
CHECK_SAMPLE, CHECK_LAST = 10, 3
# the files a result depends on besides the spec: their hash makes a row verifiable against the merged commit
CODE_FILES = (
    "src/tradedesk/analytics/strategy_sieve.py",
    "src/tradedesk/analytics/sieve_specs/__init__.py",
    "src/tradedesk/analytics/stop_management.py",
    "src/tradedesk/analytics/trail_no_target.py",
    "src/tradedesk/analytics/liquidation_flush.py",
    "src/tradedesk/analytics/course_s14_ema.py",
    "scripts/sieve.py",
    "scripts/verify_course_s14_ema.py",
)
OUT: dict[str, Any] = {"fh": None}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def day_str(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def git_commit() -> str:
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "src/tradedesk/analytics", "scripts/sieve.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return head + ("+dirty" if dirty else "")


def code_hash() -> str:
    h = hashlib.sha256()
    for rel in CODE_FILES:
        p = ROOT / rel
        h.update(rel.encode() + b"\0" + (p.read_bytes() if p.exists() else b"") + b"\0")
    return h.hexdigest()[:12]


def parse_sets(items: list[str], defaults: dict[str, Any]) -> dict[str, Any]:
    out = dict(defaults)
    for it in items:
        k, sep, v = it.partition("=")
        if not sep or k not in defaults:
            raise SystemExit(f"--set {it!r}: expected KEY=VALUE with KEY in {sorted(defaults)}")
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def cut_hidden(b: Bars) -> Bars:
    """Bars closing at or before the start of the hidden period."""
    return b.slice(0, int(np.searchsorted(b.t + b.bar_ms, HIDDEN_MS, side="right"))) if len(b) else b


def tf_bars(h1: Bars, hours: int, include_hidden: bool) -> Bars:
    tb = h1 if hours == 1 else resample(h1, hours)
    # the spec never sees a bar closing after the hidden period starts
    return tb if include_hidden else cut_hidden(tb)


def load_funding(data_dir: Path, s: str) -> tuple[np.ndarray, np.ndarray]:
    """Funding settlements of ``s`` (times ms, rates per settlement) from the funding-crowding cache; empty if none
    (then the stress rate applies, ``strategy_sieve.funding_r``)."""
    d = data_dir / "funding" / s
    rows: list[list[float]] = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            rows.extend(s1.rd(p) or [])
    if not rows:
        return np.zeros(0, dtype=np.int64), np.zeros(0)
    a = np.array(rows, dtype=float)
    a = a[np.argsort(a[:, 0], kind="stable")]
    return a[:, 0].astype(np.int64), a[:, 2]


# ---------------------------------------------------------------- per symbol (worker)


def run_symbol(args: tuple[str, str, dict[str, Any], str, str, str, bool]) -> dict[str, Any]:
    s, spec_name, params, management, s1_cache, data_dir, include_hidden = args
    spec = sieve_specs.load(spec_name)
    s1c, dd = Path(s1_cache), Path(data_dir)
    member = s1.Membership(s1.members(s1c))
    h1 = s1.load_1h(s1c, dd, s)
    if not include_hidden:
        h1 = cut_hidden(h1)  # no hidden-period price reaches a trade either (it ends at the data end)
    end = s1.END_MS if include_hidden else HIDDEN_MS
    pairs = []
    n_sig = n_checked = 0
    bad: list[str] = []
    for hours in spec.TIMEFRAMES:
        tb = tf_bars(h1, int(hours), include_hidden)
        if len(tb) == 0:
            continue
        sigs = spec.signals(tb, **params)
        n_sig += len(sigs)
        # on every symbol and timeframe (rare signals may never occur on one liquid symbol): stamps and entries of all
        # signals, a sample of signal times re-decided on the bars closing up to them, and two cuts
        m = len(tb)
        cuts = (m // 2, int(m * 0.97))
        bad += [f"{hours}h: {b}" for b in causality_violations(spec.signals, tb, params, cuts, n_sample=CHECK_SAMPLE, n_last=CHECK_LAST)]
        n_checked += min(len({sg.t_ms for sg in sigs}), CHECK_SAMPLE + CHECK_LAST)
        pairs += [(tb.bar_ms, sg) for sg in sigs]
    trades = simulate_signals(
        h1,
        pairs,
        management,
        allowed=lambda t: START_MS <= t < end and member(s, t),
        rng=np.random.default_rng(symbol_seed(s)),
        funding=load_funding(dd, s),
    )
    return {
        "sym": s,
        "signals": n_sig,
        "checked": n_checked,
        "bad": [f"{s}: {b}" for b in bad[:3]],
        "n_bad": len(bad),
        "trades": [{"sym": s, **asdict(t)} for t in trades],
    }


def run_all(
    syms: list[str], spec_name: str, params: dict[str, Any], management: str, a: argparse.Namespace, include_hidden: bool
) -> dict[str, Any]:
    jobs = [(s, spec_name, params, management, str(a.s1_cache), str(a.data_dir), include_hidden) for s in syms]
    rows: list[dict[str, Any]] = []
    bad: list[str] = []
    n_bad = n_sig = n_checked = 0
    t0 = time.time()

    def consume(results: Any) -> None:
        nonlocal n_bad, n_sig, n_checked
        for k, r in enumerate(results, 1):
            rows.extend(r["trades"])
            n_sig += r["signals"]
            n_checked += r["checked"]
            bad.extend(r["bad"])
            n_bad += r["n_bad"]
            if k % 50 == 0 or k == len(jobs):
                emit(f"  {k}/{len(jobs)} symbols, {time.time() - t0:.0f}s, signals {n_sig}, trades {len(rows)}")

    if a.workers <= 1:
        consume(map(run_symbol, jobs))  # in-process (tests, debugging)
    else:
        with Pool(a.workers) as pool:
            consume(pool.imap_unordered(run_symbol, jobs))
    rows.sort(key=lambda r: (r["t_ms"], r["sym"]))
    return {"rows": rows, "signals": n_sig, "checked": n_checked, "bad": bad, "n_bad": n_bad}


# ---------------------------------------------------------------- report helpers


def summ(rows: list[dict[str, Any]], key: str = "r_net", n_boot: int = N_BOOT) -> Summary:
    # a trade and its placebo entries share the signal's day cluster (the placebo is a mean of several random days)
    return summarize([r[key] for r in rows], [r["day"] for r in rows], n_boot=n_boot)


def fs(x: Summary) -> str:
    if x.n == 0:
        return "—"
    if not math.isfinite(x.lo):
        return f"{x.mean:+.3f} [—]"
    return f"{x.mean:+.3f} [{x.lo:+.3f}; {x.hi:+.3f}]"


def row_line(label: str, x: Summary) -> str:
    p = "—" if not math.isfinite(x.p) else f"{x.p:.2g}"
    mde = "—" if not math.isfinite(x.mde) else f"{x.mde:.3f}"
    win = "—" if not math.isfinite(x.win) else f"{x.win:.1%}"
    return f"| {label} | {x.n} | {x.dates} | {fs(x)} | {win} | {p} | {mde} |"


def paired(rows: list[dict[str, Any]], a: str, b: str) -> list[dict[str, Any]]:
    return [{"day": r["day"], "d": r[a] - r[b]} for r in rows if math.isfinite(r[a]) and math.isfinite(r[b])]


def fmt(v: Any) -> str:
    if isinstance(v, float):
        return "" if not math.isfinite(v) else f"{v:.6g}"
    return str(v)


def read_registry(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        rd = csv.DictReader(fh)
        if rd.fieldnames and list(rd.fieldnames) != COLUMNS:
            raise SystemExit(f"{path}: unexpected header {rd.fieldnames}; expected {COLUMNS}")
        return list(rd)


def append_registry(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        if new:
            w.writeheader()
        w.writerow({k: fmt(row.get(k, "")) for k in COLUMNS})


def to_float(v: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return math.nan


def refuse(title: str, lines: list[str], total: int | None = None) -> int:
    emit(f"**СПЕЦИФІКАЦІЮ ВІДХИЛЕНО: {title}**")
    for b in lines[:10]:
        emit(f"- {b}")
    if total is not None and total > 10:
        emit(f"- … усього порушень {total}")
    emit("\nу реєстр не записано: специфікацію відхилено до результату")
    return 2


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", help="module name in tradedesk.analytics.sieve_specs, e.g. s3_range_sweep")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a spec parameter (JSON value)")
    ap.add_argument("--management", choices=MANAGEMENTS, help="default: the spec's MANAGEMENT")
    ap.add_argument("--out", type=Path, help="also write the printed report to this file")
    ap.add_argument("--limit", type=int, help=f"first N <= {LIMIT_MAX} symbols: counts and causality only, no result, no registry")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    a = ap.parse_args()
    if a.limit is not None and not 1 <= a.limit <= LIMIT_MAX:
        raise SystemExit(f"--limit must be 1..{LIMIT_MAX}; a larger run is a full run and is registered")
    a.s1_cache, a.data_dir = a.s1_cache.expanduser(), a.data_dir.expanduser()
    try:
        spec = sieve_specs.load(a.spec)
    except (ValueError, ImportError) as e:
        raise SystemExit(f"spec: {e}") from e
    params = parse_sets(a.set, dict(spec.PARAMS))
    management = a.management or spec.MANAGEMENT
    if a.out:
        a.out.expanduser().parent.mkdir(parents=True, exist_ok=True)
        OUT["fh"] = a.out.expanduser().open("w")
    try:
        return run(a, spec, params, management)
    finally:
        if OUT["fh"] is not None:
            OUT["fh"].close()
            OUT["fh"] = None


def run(a: argparse.Namespace, spec: Any, params: dict[str, Any], management: str) -> int:
    now = datetime.now(UTC)
    commit, chash = git_commit(), code_hash()
    params_s = json.dumps(params, sort_keys=True, separators=(",", ":"))
    emit(f"# Сито: {spec.NAME}")
    emit(
        f"- запуск {now.isoformat(timespec='seconds')}, коміт {commit}, код {chash}, специфікація {spec.SIEVE_HASH}; джерело правила: {spec.SOURCE}"
    )
    emit(f"- параметри {params_s}; ведення {management}; таймфрейми {', '.join(f'{h}h' for h in spec.TIMEFRAMES)}")
    emit(
        f"- періоди за часом входу: IS {day_str(START_MS)} … < {day_str(IS_END_MS)} ≤ OOS < {day_str(HIDDEN_MS)}; "
        "приховане — лише після проходу IS і OOS, один раз на стратегію"
    )
    emit(
        "- R net на ризик угоди (вхід на закритті 1h-свічки сигналу; мейкер вхід / ціль 0.02 %, тейкер стоп / час 0.055 % "
        f"+ 0.02 % проковзування; фандинг за історією Binance, без історії — стрес {FUND_8H:.2%} / 8 год проти позиції), не на капітал"
    )

    syms = sorted({x for v in s1.members(a.s1_cache).values() for x in v})
    if a.limit:
        syms = syms[: a.limit]
    # causality runs inside every worker, on every symbol and timeframe (run_symbol)
    res = run_all(syms, a.spec, params, management, a, include_hidden=False)
    if res["n_bad"]:
        return refuse(
            "сигнали залежать від майбутніх барів, штамп не на закритті бару або вхід не за ціною закриття", res["bad"], res["n_bad"]
        )
    if res["checked"] == 0:
        return refuse("причинність не перевірено: специфікація не дала жодного сигналу на жодному символі", [f"символів {len(syms)}"])
    emit(
        f"- причинність: {len(syms)} символів × {len(spec.TIMEFRAMES)} тф; перевірено {res['checked']} моментів сигналів "
        f"(до {CHECK_SAMPLE + CHECK_LAST} на символ і тф, кожен заново на барах до нього), 2 зрізи на ряд, штампи й входи "
        f"всіх {res['signals']} сигналів; порушень немає"
    )
    rows = res["rows"]
    if any(r["period"] == "hidden" for r in rows):
        raise SystemExit("internal error: hidden-period trade in a non-reveal run")
    if a.limit:
        per = Counter(r["period"] for r in rows)
        emit(f"\n## Налагодження ({len(syms)} символів)")
        emit(f"- сигналів {res['signals']}, угод {len(rows)} (IS {per.get('is', 0)}, OOS {per.get('oos', 0)}); штампи й входи в порядку")
        emit("- результат (E[R], CI, вердикт) у прогоні з --limit не показується; у реєстр не записано")
        return 0

    # everything (the hidden period included) is computed first, then the registry row is written, then the report is
    # printed: a crash in between shows nothing rather than a result without a row
    nb = N_BOOT  # fixed by the code (code_hash), so registry rows are comparable
    report: list[str] = []
    say = report.append
    IS = [r for r in rows if r["period"] == "is"]
    OOS = [r for r in rows if r["period"] == "oos"]
    s_is, s_oos = summ(IS, n_boot=nb), summ(OOS, n_boot=nb)
    p_is, p_oos = summ(IS, "placebo_r", nb), summ(OOS, "placebo_r", nb)
    vs: dict[str, Summary] = {}
    for lab, part in (("IS", IS), ("OOS", OOS)):
        d = paired(part, "r_net", "placebo_r")
        vs[lab] = summarize([x["d"] for x in d], [x["day"] for x in d], n_boot=nb)

    registry = DEFAULT_REGISTRY
    reg = read_registry(registry)
    n_reg = len(reg) + 1  # this run counts
    n_eff = n_effective(n_reg)
    p_test = max(s_is.p, vs["IS"].p) if math.isfinite(s_is.p) and math.isfinite(vs["IS"].p) else math.nan
    p_holm = holm_with_registry(p_test, [to_float(r.get("p_test", "")) for r in reg])
    outcome = verdict(s_is, s_oos, vs["IS"], vs["OOS"], p_holm, n_eff=n_eff)

    say(f"\n## Результат ({len(syms)} символів, сигналів {res['signals']}, угод {len(rows)})")
    say("| вибірка | n | дат | E[R] net [95 % CI] | R > 0 | p (E>0) | MDE 80 % (N=100) |")
    say("|---|---|---|---|---|---|---|")
    say(row_line("IS", s_is))
    say(row_line("OOS", s_oos))
    say(row_line(f"плацебо IS (середнє {PLACEBO_K} входів)", p_is))
    say(row_line(f"плацебо OOS (середнє {PLACEBO_K} входів)", p_oos))
    say(row_line("сигнал − плацебо IS", vs["IS"]))
    say(row_line("сигнал − плацебо OOS", vs["OOS"]))
    s_stress = summ(IS, "r_stress", nb)
    say(row_line("довідково: стрес IS (тейкер-вхід, витрати × 2)", s_stress))
    say(row_line("довідково: стрес OOS (тейкер-вхід, витрати × 2)", summ(OOS, "r_stress", nb)))
    nofund = [{**r, "r_nofund": r["r_net"] + r["fund_r"]} for r in IS]
    say(row_line("довідково: IS без фандингу", summ(nofund, "r_nofund", nb)))
    other = "B" if management == "strategy" else "strategy"
    say(row_line(f"довідково: ведення {other}, IS", summ(IS, "r_alt", nb)))
    say(row_line(f"довідково: ведення {other}, OOS", summ(OOS, "r_alt", nb)))
    for key, vals in (("side", ("long", "short")), ("tf_ms", sorted({r["tf_ms"] for r in rows}))):
        for v in vals:
            part = [r for r in IS if r[key] == v]
            if part:
                lab = v if key == "side" else f"{int(v) // HOUR_MS}h"
                say(row_line(f"IS, {lab}", summ(part, n_boot=min(nb, 2000))))
    say(f"\nвиходи (IS+OOS): {dict(sorted(Counter(r['exit_kind'] for r in rows).items()))}")
    if rows:
        src = Counter(r["fund_src"] for r in rows)
        say(
            f"медіана стопу {np.median([r['stop_frac'] for r in rows]):.2%} ціни; середні витрати {np.mean([r['cost_r'] for r in rows]):.3f}R; "
            f"середній фандинг {np.mean([r['fund_r'] for r in rows]):+.3f}R (історія {src.get('actual', 0)}, стрес {src.get('flat', 0)} угод); "
            f"медіана холду {np.median([(r['exit_ms'] - r['t_ms']) / HOUR_MS for r in rows]):.0f} год"
        )

    mde = mde_z(n_eff) * s_is.se if math.isfinite(s_is.se) else math.inf
    say("\n## Вердикт")
    say(
        f"- множинність: Holm над {n_reg} прогонами реєстру (з цим), рахується N_eff = max({n_reg}, {N_PLAN}) = {n_eff}; "
        f"p IS = max(p E[R] {s_is.p:.3g}, p сигнал − плацебо {vs['IS'].p:.3g}) = {p_test:.3g} → Holm {p_holm:.3g}; "
        f"найсуворіший крок α/N_eff = {p_threshold(n_reg):.2g}; бутстреп {nb} повторів"
    )
    say(
        f"- умови: E[R] IS ≥ +{MIN_EFFECT}R ({s_is.mean:+.3f}), CI IS > 0 ({s_is.lo:+.3f}), CI сигнал − плацебо IS > 0 "
        f"({vs['IS'].lo:+.3f}), Holm p < 0.05 ({p_holm:.3g}), OOS > 0 ({s_oos.mean:+.3f}), сигнал − плацебо OOS > 0 "
        f"({vs['OOS'].mean:+.3f}); MDE IS при N_eff {mde:.3f}R"
    )
    say(f"- **{outcome}**")
    if outcome == PASS and not (math.isfinite(s_stress.mean) and s_stress.mean > 0):
        say("- УВАГА: у стресі (тейкер-вхід, витрати × 2) E[R] IS ≤ 0; на демо перевага може зникнути на витратах")

    row: dict[str, Any] = {
        "run_id": f"{now:%Y%m%dT%H%M%SZ}-{spec.NAME}",
        "date": now.isoformat(timespec="seconds"),
        "commit": commit,
        "code_hash": chash,
        "spec_hash": spec.SIEVE_HASH,
        "strategy": spec.NAME,
        "params": params_s,
        "management": management,
        "timeframes": "/".join(f"{h}h" for h in spec.TIMEFRAMES),
        "symbols": len(syms),
        "n_is": s_is.n,
        "dates_is": s_is.dates,
        "mean_is": s_is.mean,
        "lo_is": s_is.lo,
        "hi_is": s_is.hi,
        "p_is": s_is.p,
        "mde_is": mde,
        "n_oos": s_oos.n,
        "dates_oos": s_oos.dates,
        "mean_oos": s_oos.mean,
        "lo_oos": s_oos.lo,
        "hi_oos": s_oos.hi,
        "fund_is": float(np.mean([r["fund_r"] for r in IS])) if IS else math.nan,
        "stress_is": s_stress.mean,
        "placebo_is": p_is.mean,
        "placebo_oos": p_oos.mean,
        "vs_placebo_is": vs["IS"].mean,
        "vs_placebo_lo_is": vs["IS"].lo,
        "vs_placebo_hi_is": vs["IS"].hi,
        "p_vs_placebo_is": vs["IS"].p,
        "vs_placebo_oos": vs["OOS"].mean,
        "p_test": p_test,
        "n_registry": n_reg,
        "n_effective": n_eff,
        "p_holm": p_holm,
        "outcome": outcome,
        "revealed": 0,
    }
    if outcome == PASS:
        prev = revealed_before(reg, spec.NAME, spec.SIEVE_HASH)
        if prev is not None:
            say(f"- приховане вже відкрито прогоном {prev.get('run_id')} ({prev.get('date')}); повторно не показується")
        else:
            full = run_all(syms, a.spec, params, management, a, include_hidden=True)["rows"]
            H = [r for r in full if r["period"] == "hidden"]
            s_h = summ(H, n_boot=nb)
            say("\n## Приховане (≥ 2026-03-01), відкрито один раз")
            say("| вибірка | n | дат | E[R] net [95 % CI] | R > 0 | p (E>0) | MDE 80 % (N=100) |")
            say("|---|---|---|---|---|---|---|")
            say(row_line("приховане", s_h))
            say(row_line("плацебо приховане", summ(H, "placebo_r", nb)))
            say(f"- той самий знак у прихованому: {'так' if s_h.n and s_h.mean > 0 else 'ні'}")
            row.update(revealed=1, n_hidden=s_h.n, mean_hidden=s_h.mean, lo_hidden=s_h.lo, hi_hidden=s_h.hi)
    append_registry(registry, row)
    for line in report:
        emit(line)
    emit(f"\nзаписано в реєстр: {registry.relative_to(ROOT) if registry.is_relative_to(ROOT) else registry} (рядок {n_reg})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
