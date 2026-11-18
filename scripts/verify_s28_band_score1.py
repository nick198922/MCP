"""S28 passport band for the demo's selection «score >= 1» (accounting; weekly audit 17.11, card demo-setups-depth).

The CHECKPOINT-2026-11-30 band for S28 and the removal threshold -0.438R (Q-20261030-2) come from the passport's
verdict sample with score >= 2; since 10.11 the demo takes S28 with score >= 1 (``demo_trader.MIN_SCORE_BY_SETUP``).
This script repeats ``scripts/verify_band_n.py S28`` (same cache, same top-100 >= $5M list, same one-placement-per-coin
chain, OOS >= 2025-03-01, filled trades, ``setup_passport.r_band``, 10 000 draws, seed 20261030) with the score floor
as the only change. Reads only ``~/.cache/tradedesk-agents/setup_passport/S28/``, ``funding_crowding`` and
``course_s14_ema``; no network, no demo DB.

    uv run python scripts/verify_s28_band_score1.py --stage smoke   # counts of placements only, no R, no fill rate
    uv run python scripts/verify_s28_band_score1.py --stage run     # the registered numbers

Selections (registration «S28, смуга для відбору „бал ≥ 1“» in the card):
  ref  chain on score >= 2 (the passport; must give OOS filled n 497 and the n 30 band -0.438 .. +0.247)
  P1   chain on score >= 1 (the demo since 10.11)            <- primary: the band for S28 trades after 10.11
  E1   P1 placements with score == 1 (same chain)
  E2   P1 placements with score == 2 (same chain; descriptive)
  S1   chain on score == 1 alone (descriptive: a demo that would take only score 1)
"""

import argparse
import resource
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics.catalyst_scalp import wilson_ci  # noqa: E402

SETUP = "S28"
NS = (30, 50, 100)


class Tee:
    """Print to the console and to ``~/.cache/tradedesk-agents/s28-band-score1/<stage>.txt``."""

    def __init__(self, *streams: Any) -> None:
        self.streams = streams

    def write(self, s: str) -> None:
        for st in self.streams:
            st.write(s)

    def flush(self) -> None:
        for st in self.streams:
            st.flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def load() -> tuple[dict[str, np.ndarray], np.ndarray]:
    """The passport rows and the base mask without the score floor (period, R:R, stop, turnover, prev-day top-N)."""
    import verify_setup_passport as vp

    home = Path.home() / ".cache/tradedesk-agents"
    T = vp.load_rows(home / "setup_passport" / SETUP)
    period = T["t_in"] >= vp.PASSPORT_MS
    rr_stop = (T["rr"] >= vp.MIN_RR) & (T["stop_pct"] <= vp.MAX_STOP)
    turn = np.nan_to_num(T["turn"], nan=-1) >= vp.SETUPS[SETUP][2]
    elig = vp.s1.eligible_syms(home / "funding_crowding")
    qv = vp.s1.daily_qv(home / "course_s14_ema", home / "funding_crowding", elig)
    top_days = vp.top_n_days(qv, vp.top_n_of(SETUP))
    return T, period & rr_stop & turn & vp.in_prev_day_top(T, top_days)


def selections(T: dict[str, np.ndarray], base: np.ndarray) -> dict[str, np.ndarray]:
    """Placement masks (chain over the placements, as ``verify_band_n.passport`` for a limit setup)."""
    import verify_setup_passport as vp

    cap = vp.BAR_CAP.get(SETUP)
    s = T["score"]
    p1 = vp.chain(T, base & (s >= 1), cap)
    return {
        "ref": vp.chain(T, base & (s >= 2), cap),
        "P1": p1,
        "E1": p1 & (s == 1),
        "E2": p1 & (s == 2),
        "S1": vp.chain(T, base & (s == 1), cap),
    }


def ordered(T: dict[str, np.ndarray], m: np.ndarray) -> np.ndarray:
    idx = np.flatnonzero(m)
    return idx[np.argsort(T["t_in"][idx], kind="stable")]


def band_line(v: np.ndarray, n: int, seed: int, reps: int) -> str:
    b = sp.r_band(v, n, reps=reps, seed=seed)
    lo, hi = min(b["iid_p5"], b["roll_p5"]), max(b["iid_p95"], b["roll_p95"])
    return (
        f"n={n}: iid {b['iid_p5']:+.3f} / {b['iid_p50']:+.3f} / {b['iid_p95']:+.3f}; rolling ({int(b['windows'])} windows) "
        f"{b['roll_p5']:+.3f} / {b['roll_p50']:+.3f} / {b['roll_p95']:+.3f}; decision band {lo:+.3f} .. {hi:+.3f}"
    )


def stage_smoke(T: dict[str, np.ndarray], base: np.ndarray, S: dict[str, np.ndarray]) -> None:
    import verify_setup_passport as vp

    oos = T["t_in"] >= vp.OOS_MS
    vals, cnt = np.unique(T["score"][base], return_counts=True)
    print("score values in the base mask (2021+, all placements before the chain):", dict(zip(vals.tolist(), cnt.tolist(), strict=True)))
    for k, m in S.items():
        print(f"{k}: placements 2021+ {int(m.sum())}, OOS {int((m & oos).sum())}")
    ref_f = S["ref"] & oos & T["filled"]
    print(f"ref OOS filled n {int(ref_f.sum())} (passport 497; the only filled count printed at this stage)")


def stage_run(T: dict[str, np.ndarray], base: np.ndarray, S: dict[str, np.ndarray]) -> None:
    import verify_setup_passport as vp

    oos = T["t_in"] >= vp.OOS_MS
    F = T["filled"]
    for k, m in S.items():
        print(f"\n## {k}")
        for lab, per in (("OOS", oos), ("2021+", np.ones_like(oos)), ("IS", ~oos)):
            pm = m & per
            fm = pm & F
            n_p, n_f = int(pm.sum()), int(fm.sum())
            lo, hi = wilson_ci(n_f, n_p)
            r = vp.ci(T, fm)
            print(
                f"{lab}: placements {n_p}, filled {n_f}, fill {n_f / max(1, n_p):.1%} [{lo:.1%}; {hi:.1%}]; "
                f"E[R] filled {r['m']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}] (weeks {int(r['k'])}, p {r['p']:.3f})"
            )
        idx = ordered(T, m & oos & F)
        v = T["B"][idx]
        v = v[np.isfinite(v)]
        print(f"OOS filled R: n {v.size}, mean {v.mean():+.3f}, sd {v.std(ddof=1):.3f}")
        for n in NS:
            print("  filled " + band_line(v, n, vp.SEED, vp.N_BOOT))
        pidx = ordered(T, m & oos)
        pv = np.where(F[pidx], T["B"][pidx], 0.0)
        pv = pv[np.isfinite(pv)]
        print(f"  per placement (unfilled = 0R), descriptive: {band_line(pv, 30, vp.SEED, vp.N_BOOT)}")
    # share of score-1 trades in the primary selection, OOS (what the demo's mix looks like)
    p1f = S["P1"] & oos & F
    print(f"\nP1 OOS filled: score 1 share {(p1f & (T['score'] == 1)).sum() / max(1, p1f.sum()):.1%}")
    # score-2 placements the score-1 placements displace through the chain (coin busy), OOS
    lost = S["ref"] & ~S["P1"] & oos
    print(f"ref placements not in P1 (coin held by a score-1 limit), OOS: {int(lost.sum())} of {int((S['ref'] & oos).sum())}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("smoke", "run"), required=True)
    a = ap.parse_args()
    out = Path.home() / ".cache/tradedesk-agents/s28-band-score1"
    out.mkdir(parents=True, exist_ok=True)
    sys.stdout = Tee(sys.stdout, (out / f"{a.stage}.txt").open("w"))
    t0 = time.time()
    T, base = load()
    print(f"[load] rows {T['t_in'].size} in {time.time() - t0:.0f}s, peak rss {rss_mb():.0f} MB")
    S = selections(T, base)
    if a.stage == "smoke":
        stage_smoke(T, base, S)
    else:
        stage_run(T, base, S)
    print(f"[done] {time.time() - t0:.0f}s, peak rss {rss_mb():.0f} MB")


if __name__ == "__main__":
    main()
