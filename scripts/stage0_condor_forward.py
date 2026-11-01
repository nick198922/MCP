"""Stage 0 (before any forward data) of the weekly condor forward test H1 / H4, item 8j of
docs/ideas/IDEA-20261031-structural-edges-depth.md.

Prints SDs only (no means): the historical SD per week of H1 (item 7 backtest, cache only) and of the H4 increment d,
the MDE at 16 / 26 / 52 weeks and the weeks needed for MDE <= the a priori effect; and a pre-T0 cost prior from the
saved live Deribit answer `tests/fixtures/deribit_option_legs_2026-11-01.json` (not a forward week, not a snapshot).

    uv run python scripts/stage0_condor_forward.py [--out ~/.cache/tradedesk-agents/condor_forward/stage0.md]
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_option_modules as vom  # noqa: E402  (scripts/ is not a package)

from tradedesk.analytics.condor_forward import H1_QTY, condor_cost, leg_cost, mde, power, weeks_for_mde  # noqa: E402
from tradedesk.analytics.option_condor import dvol_size, exec_legs, scaled_minus_equal, settle_condor  # noqa: E402
from tradedesk.analytics.option_legs import expiry_surface, leg_row, plan_legs  # noqa: E402
from tradedesk.analytics.put_skew import dvol_known_at_entry, trailing_percentile  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIX = REPO / "tests" / "fixtures" / "deribit_option_legs_2026-11-01.json"
HORIZONS = (16, 26, 52)
PRIOR = {"H1": 0.0010, "H4": 0.0013}  # a priori effects, fractions of spot per week (see the registration)
# The registered test: H4 is the only primary hypothesis at two-sided alpha = 0.05 (H1 descriptive, outside Holm), so
# the power / MDE columns use alpha = 0.05. The last column is a reference at alpha = 0.025 (what Holm m = 2 would give).
Z_025 = 2.241403  # two-sided alpha = 0.025
Z_POW80 = 0.841621  # power 80 %


def pnl_weeks(D, model: str, band: float) -> dict[int, float]:
    kind, hs = vom.MODELS[model]
    out = {}
    for f in vom.fridays(vom.FIRST, vom.LAST):
        p, _ = vom.plan(D, f, "h1", band=band)
        if p is None:
            continue
        legs, _ = exec_legs(p, kind, hs)
        r = settle_condor(D["days"], p, legs, D["delivery"][vom.ymd(p["expiry"])])
        if r is not None:
            out[f] = r["pnl"]
    return out


def ac1(v: list[float]) -> float:
    m = statistics.mean(v)
    c0 = sum((x - m) ** 2 for x in v)
    return sum((a - m) * (b - m) for a, b in zip(v, v[1:], strict=False)) / c0


def mde_row(label: str, sd: float, n_hist: int, prior: float) -> str:
    ms = " | ".join(f"{100 * mde(sd, h):.2f}" for h in HORIZONS)
    pw = " / ".join(f"{power(sd, h, prior):.2f}" for h in HORIZONS)
    ref = f"{power(sd, 52, prior, z_alpha=Z_025):.2f} / {weeks_for_mde(sd, prior, k=Z_025 + Z_POW80)}"
    return f"| {label} | {n_hist} | {100 * sd:.2f} | {ms} | {100 * prior:.2f} | {pw} | {weeks_for_mde(sd, prior)} | {ref} |"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-dir", type=Path, default=Path.home() / ".cache" / "tradedesk-agents" / "put_skew")
    ap.add_argument("--out", type=Path, default=Path.home() / ".cache" / "tradedesk-agents" / "condor_forward" / "stage0.md")
    a = ap.parse_args()
    D = vom.load(a.src_dir)
    L = ["## Stage 0: SD per week (no means), MDE = 2.8 SD / sqrt(n) (alpha 0.05, power 80 %), % of spot per 1 BTC notional", ""]
    L.append(
        "| series | n hist | SD | MDE 16 | MDE 26 | MDE 52 | a priori | power 16 / 26 / 52 | weeks for MDE <= a priori "
        "| ref. alpha 0.025: power 52 / weeks |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    h1 = {}
    for model in ("side", "mark"):
        for band in (0.015, 0.03, 0.05):
            w = pnl_weeks(D, model, band)
            v = [w[d] for d in sorted(w)]
            h1[(model, band)] = w
            L.append(mde_row(f"H1 {model} band {band:.1%} (ac1 {ac1(v):+.2f})", statistics.stdev(v), len(v), PRIOR["H1"]))
    known = dvol_known_at_entry(D["dvol"])
    pct = trailing_percentile(sorted(known.items()), window_ms=365 * vom.DAY_MS, min_obs=180)
    for model, band in (("side", 0.015), ("mark", 0.015), ("side", 0.05), ("mark", 0.05)):
        tr = h1[(model, band)]
        lab = f"{model} band {band:.1%}"
        ds = [d for d in sorted(tr) if d in pct]
        size = {d: dvol_size(pct[d]) for d in ds}
        ins, oos = [d for d in ds if d < vom.OOS], [d for d in ds if d >= vom.OOS]
        s_is = statistics.mean(size[d] for d in ins)
        for label, sample, s_bar in (
            ("OOS, s_bar = IS mean (item 7 formula)", oos, s_is),
            ("OOS, s_bar = own mean", oos, statistics.mean(size[d] for d in oos)),
            ("all weeks with P_t, s_bar = own mean", ds, statistics.mean(size[d] for d in ds)),
        ):
            dv = scaled_minus_equal([tr[d] for d in sample], [size[d] for d in sample], s_bar)
            L.append(mde_row(f"H4 d {lab}: {label}", statistics.stdev(dv), len(dv), PRIOR["H4"]))
    L.append("")

    # ---- pre-T0 cost prior from the saved live answer (2026-11-01, a Sunday: tau ~4.9 d, not a Friday entry)
    live = json.loads(FIX.read_text())
    now = datetime.fromisoformat(live["fetched"])
    exp_dt = datetime(2026, 11, 6, 8, tzinfo=UTC)
    L.append(f"## Cost prior: live answer {live['fetched']} (expiry {exp_dt:%Y-%m-%d}), actual vs mark -/+ 1.5 vp, % of spot")
    L.append("")
    L.append(
        "| cur | role | strike | bid | ask | mark | (ask_iv - bid_iv)/2, vp | open actual | open model | close actual | close model | bid/ask size |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for cur in ("BTC", "ETH"):
        exp = expiry_surface(live["summary"][cur]["result"], cur, exp_dt, now)
        legs, _ = plan_legs(exp)
        rows = {}
        for leg in legs:
            if leg["role"] not in H1_QTY:
                continue
            t = (live["ticker"].get(leg["instrument"]) or {}).get("result")
            r = leg_row(leg, exp, t, currency=cur, expiry=exp_dt, now=now)
            rows[leg["role"]] = r
            o = leg_cost(r, H1_QTY[leg["role"]], "open")
            c = leg_cost(r, H1_QTY[leg["role"]], "close")
            hiv = (r["ask_iv"] - r["bid_iv"]) / 2 * 100 if r["ask_iv"] and r["bid_iv"] else float("nan")
            L.append(
                f"| {cur} | {leg['role']} | {r['strike']:.0f} | {r['bid']} | {r['ask']} | {r['mark']:.4f} | {hiv:.2f} | "
                f"{100 * o[0]:.3f} | {100 * o[1]:.3f} | {100 * c[0]:.3f} | {100 * c[1]:.3f} | {r['bid_size']}/{r['ask_size']} |"
                if o and c
                else f"| {cur} | {leg['role']} | {r['strike']:.0f} | {r['bid']} | {r['ask']} | {r['mark']} | — | — | — | — | — | — |"
            )
        for action in ("open", "close"):
            cc = condor_cost(rows, action) if len(rows) == 4 else None
            if cc:
                L.append(
                    f"| {cur} | condor {action} | | | | | | {100 * cc['actual']:.3f} | {100 * cc['model']:.3f} | ratio {cc['ratio']:.2f} | | |"
                )
    L.append("")
    L.append(f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
