"""S3 demo vs its passport (IDEA-20261030-demo-setups-depth, items 6 and 9; pre-registered in the card).

    uv run python scripts/verify_s3_demo_vs_passport.py --json ~/.cache/tradedesk-agents/s3-demo-vs-passport/dvb.json
                                                       [--setup S3] [--out FILE]

Thin reader, no network and no new numeric logic: the per-trade replay comes from the ``--json`` output of
``scripts/demo_vs_backtest.py`` (pure ``analytics/demo_vs_backtest.py``); the CI is
``analytics/liquidation_flush.cluster_bootstrap_ci`` with the UTC day of the signal bar as the cluster; the not-executed
signals of the setup are summarised with ``analytics/demo_shadow.summary``. R is per trade risk, not on capital.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics import demo_shadow
from tradedesk.analytics.demo_vs_backtest import BE_SWITCH_MS
from tradedesk.analytics.liquidation_flush import cluster_bootstrap_ci

SEED = 20261030
N_BOOT = 10_000
# Passport S3 (docs/verification/2026-10-30-passport-s3.md): top-100 and >= $5M, OOS, mean R of 30 trades
BAND_IID = (-0.473, -0.087, 0.361)  # 5th, median, 95th
BAND_SLIDING = (-0.719, -0.102, 0.652)
PASSPORT_OOS_MEAN = -0.076
MIN_GROUP = 10


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def stat(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    pairs = [(float(r[key]), _day(int(r["signal_ts"]))) for r in rows if r.get(key) is not None]
    if not pairs:
        return {"n": 0}
    v = [p[0] for p in pairs]
    d = [p[1] for p in pairs]
    ci = cluster_bootstrap_ci(v, d, n_boot=N_BOOT, seed=SEED) if len(pairs) >= 2 else None
    return {"n": len(v), "days": len(set(d)), "mean": statistics.fmean(v), "ci": ci, "sum": sum(v), "win": sum(x > 0 for x in v) / len(v)}


def fmt(s: dict[str, Any]) -> str:
    if not s.get("n"):
        return "n 0"
    ci = f"[{s['ci'][0]:+.3f}; {s['ci'][1]:+.3f}]" if s.get("ci") else "[—]"
    tag = "" if s["n"] >= MIN_GROUP else "  (n < 10: невимірно)"
    return f"n {s['n']:>2}, днів {s['days']}, середнє {s['mean']:+.3f} {ci}, сума {s['sum']:+.2f}, R>0 {s['win']:.0%}{tag}"


def band_verdict(m: float) -> str:
    if BAND_IID[0] <= m <= BAND_IID[2]:
        return "✓ всередині i.i.d.-смуги"
    if BAND_SLIDING[0] <= m <= BAND_SLIDING[2]:
        return "≈ поза i.i.d., всередині ковзної"
    return "✗ поза ковзною смугою"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", required=True, help="output of scripts/demo_vs_backtest.py --json")
    ap.add_argument("--setup", default="S3")
    ap.add_argument("--out", help="also write the text to this file")
    a = ap.parse_args(argv)
    doc = json.loads(Path(a.json).expanduser().read_text(encoding="utf-8"))
    rows = [r for r in doc["rows"] if r["setup"] == a.setup]
    L: list[str] = []
    p = L.append
    p(
        f"# {a.setup}: демо проти паспорта; JSON від {datetime.fromtimestamp(doc['generated_ms'] / 1000, UTC):%Y-%m-%d %H:%M} UTC, venue {doc['venue']}"
    )
    p(
        f"закритих угод {len(rows)}; вердикти {dict(Counter(r['verdict'] for r in rows))}; r_demo відсутній у {sum(r.get('r_demo') is None for r in rows)}"
    )
    p(f"сигнальні дні: {sorted(Counter(_day(int(r['signal_ts'])) for r in rows).items())}")
    main_s = stat(rows, "r_demo")
    p("")
    p("## Основне (R на ризик угоди, не на капітал)")
    p(f"R демо (од. демо)          {fmt(main_s)}")
    p(f"R демо (од. ризику бектесту) {fmt(stat(rows, 'r_demo_bt'))}")
    p(f"R бектесту B (ті самі угоди) {fmt(stat(rows, 'r_bt'))}")
    p(f"R бектесту Bx               {fmt(stat(rows, 'r_bt_fast'))}")
    p(f"R бектесту без беззбитку    {fmt(stat(rows, 'r_plain'))}")
    if main_s.get("n"):
        m, n = main_s["mean"], main_s["n"]
        k = (30 / n) ** 0.5
        lo, mid, hi = BAND_IID
        p(f"смуга i.i.d. n=30 [{lo:+.3f}; {hi:+.3f}], ковзна [{BAND_SLIDING[0]:+.3f}; {BAND_SLIDING[2]:+.3f}] -> {band_verdict(m)}")
        p(f"описово: i.i.d. на n={n} (√(30/n) навколо медіани) [{mid + (lo - mid) * k:+.3f}; {mid + (hi - mid) * k:+.3f}]")
        ci = main_s["ci"]
        if ci:
            p(
                f"демо − OOS паспорта ({PASSPORT_OOS_MEAN:+.3f}): {m - PASSPORT_OOS_MEAN:+.3f} [{ci[0] - PASSPORT_OOS_MEAN:+.3f}; {ci[1] - PASSPORT_OOS_MEAN:+.3f}] (CI лише демо)"
            )
        mb = stat(rows, "r_demo_bt")
        if mb.get("n"):
            p(f"другорядна (од. бектесту) {mb['mean']:+.3f} -> {band_verdict(mb['mean'])}")
    p("")
    p("## Розбивки (описово, R демо в од. демо | в од. бектесту)")
    groups = {
        "4h": lambda r: r["timeframe"] == "4h",
        "1d": lambda r: r["timeframe"] == "1d",
        "лонг": lambda r: r["side"] == "long",
        "шорт": lambda r: r["side"] == "short",
        "4h лонг": lambda r: r["timeframe"] == "4h" and r["side"] == "long",
        "4h шорт": lambda r: r["timeframe"] == "4h" and r["side"] == "short",
        "1d лонг": lambda r: r["timeframe"] == "1d" and r["side"] == "long",
        "1d шорт": lambda r: r["timeframe"] == "1d" and r["side"] == "short",
        "оборот ≥ $5M": lambda r: r.get("turnover_group") == "ge5m",
        "оборот $1–5M": lambda r: r.get("turnover_group") == "1to5m",
        "оборот < $1M": lambda r: r.get("turnover_group") == "lt1m",
        "оборот невідомий": lambda r: r.get("turnover_group") not in ("ge5m", "1to5m", "lt1m"),
        "беззбиток ROE (до 22.10)": lambda r: r.get("be_rule") == "roe",
        "беззбиток шлях": lambda r: r.get("be_rule") == "path",
    }
    p(f"групи обороту в JSON: {dict(Counter(str(r.get('turnover_group')) for r in rows))}")
    for name, f in groups.items():
        sub = [r for r in rows if f(r)]
        p(f"- {name:<26} {fmt(stat(sub, 'r_demo'))} | {stat(sub, 'r_demo_bt').get('mean', float('nan')):+.3f}")
    # Descriptive, added after the results (critic B, finding 1): the old-rule (ROE) trades replayed under today's
    # break-even rule from the demo fill (r_bt_fill_now, backtest risk units); all other trades keep r_demo.
    roe = [r for r in rows if r.get("be_rule") == "roe"]
    for r in roe:
        p(
            f"  ROE {r['symbol']} {_day(int(r['signal_ts']))}: demo {r.get('r_demo')} fill_old {r.get('r_bt_fill')} fill_now {r.get('r_bt_fill_now')} plain {r.get('r_plain')} be_switch_inside {(r.get('extra') or {}).get('be_switch_inside')}"
        )
    now_rows = [
        dict(r, r_now=r["r_bt_fill_now"] if r.get("be_rule") == "roe" and r.get("r_bt_fill_now") is not None else r.get("r_demo"))
        for r in rows
    ]
    p(f"- ROE-угоди за чинним правилом (r_bt_fill_now; описово, після результатів) {fmt(stat(now_rows, 'r_now'))}")
    p("")
    p("## Пункт 9: виконання (од. ризику бектесту, + = демо краще)")
    p(f"причини розбіжностей: {dict(Counter(x for r in rows for x in r.get('reasons') or []))}")
    p(f"вхід демо проти закриття свічки (entry_edge_r) {fmt(stat(rows, 'entry_edge_r'))}")
    p(f"  з них план (різниця свічок, plan_edge_r)    {fmt(stat(rows, 'plan_edge_r'))}")
    ex = [
        dict(r, exec_r=r["entry_edge_r"] - r["plan_edge_r"])
        for r in rows
        if r.get("entry_edge_r") is not None and r.get("plan_edge_r") is not None
    ]
    p(f"  виконання (entry − plan)                    {fmt(stat(ex, 'exec_r'))}")
    p(
        f"|Δ стопу| R: макс {max((abs(r['stop_diff_r']) for r in rows if r.get('stop_diff_r') is not None), default=float('nan')):.3f}; |Δ цілі| R: макс {max((abs(r['target_diff_r']) for r in rows if r.get('target_diff_r') is not None), default=float('nan')):.3f}"
    )
    p(f"r_diff (демо − бектест B) {fmt(stat(rows, 'r_diff'))}")
    p(f"виходи демо / бектест: {sorted(Counter((str(r.get('demo_exit')), str(r.get('bt_exit'))) for r in rows).items())}")
    p("розбіжності:")
    for r in rows:
        if r["verdict"] != "match":
            p(
                f"  {r['symbol']} {_day(int(r['signal_ts']))} {r['timeframe']} {r['side']}: {r['verdict']} {r.get('reasons')} demo {r.get('r_demo')} demo_bt {r.get('r_demo_bt')} bt [{r.get('r_bt')}; {r.get('r_bt_fast')}] fill [{r.get('r_bt_fill')}; {r.get('r_bt_fill_fast')}] edge {r.get('entry_edge_r')} exit {r.get('demo_exit')}/{r.get('bt_exit')}"
            )
    ne = doc.get("not_executed") or {}
    srows = [demo_shadow.ShadowRow(**x) for x in ne.get("rows", []) if x.get("setup") == a.setup]
    p("")
    p(f"## Невиконані сигнали {a.setup} проти тіньових планів (R без беззбитку, од. демо; t-CI з demo_shadow)")
    if srows:
        sm = demo_shadow.summary(srows, now_ms=int(ne.get("now_ms") or doc["generated_ms"]))
        for g, s in sm["groups"].items():
            ci = f"[{s['ci95_r'][0]:+.2f}; {s['ci95_r'][1]:+.2f}]" if s.get("ci95_r") else "[—]"
            mr = f"{s['mean_r']:+.3f}" if s.get("mean_r") is not None else "—"
            p(
                f"- {g}: n {s['n']}, з R {s['n_r']}, відкрито {s['open']}, без плану {s['no_plan']}, середній {mr} {ci}, сума {s['sum_r'] if s['sum_r'] is None else round(s['sum_r'], 2)}"
            )
            for rr in s.get("reasons", [])[:6]:
                mr2 = f"{rr['mean_r']:+.2f}" if rr.get("mean_r") is not None else "—"
                p(f"    · {rr['reason'][:70]}: n {rr['n']}, з R {rr['n_r']}, середній {mr2}")
        tk = [x for x in srows if x.group == "taken" and x.r is not None]
        p(f"  (виконані з тіньовим R: {len(tk)})")
    else:
        p("рядків немає")
    p(f"\nBE_SWITCH_MS {BE_SWITCH_MS}; seed {SEED}; бутстреп {N_BOOT} кластерами за днем сигналу")
    text = "\n".join(L) + "\n"
    print(text, end="")
    if a.out:
        Path(a.out).expanduser().write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
