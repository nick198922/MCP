"""Read-only backtest bands for the CHECKPOINT-2026-11-30 criterion «узгоджується з бектестом».

Mode 1, a demo setup: the 5th..95th percentile of the mean R of n trades (n = 30 / 50 / 100), i.i.d. and rolling,
on the passport's OOS sample, line «OOS топ-N (список демо, еталон пункту 6)» of
``verify_setup_passport.py --stage report`` (limit setups: filled trades only). Reads only the passport cache
``~/.cache/tradedesk-agents/setup_passport/<setup>/``, ``funding_crowding`` and ``course_s14_ema``; no network, no demo
DB. n = 30 reproduces the passports. Also prints the decision band: per end the wider of i.i.d. and rolling.

    uv run python scripts/verify_band_n.py S3
    uv run python scripts/verify_band_n.py S17 1d      # 1d verdict chain, as --tf in the passport

Mode 2, carry A v2: i.i.d. band of the mean Δ = (iii) − (ii) of n delisting events (pp of slot notional), bootstrap
from the 16 backtest events of docs/verification/2026-10-31-carry-a-delisting.md (table «Події»), with and without KDA.

    uv run python scripts/verify_band_n.py carry-a-v2
"""

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from tradedesk.analytics import setup_passport as sp  # noqa: E402

SEED = 20261030
N_BOOT = 10_000

# (iii) − (ii), pp of slot notional, 16 events in date order (report 8b, FTM … ATA).
A_V2_EVENTS = {
    "FTM": 10.1, "AMB": 16.7, "BNX": 0.0, "LINA": 0.0, "LOKA": -11.8, "BSW": 0.0, "KDA": 62.0, "VOXEL": 11.0,
    "REI": 20.9, "FXS": -31.3, "CHESS": 27.3, "A2Z": 33.8, "FUN": -7.2, "FIO": 31.8, "AI": -28.1, "ATA": 22.4,
}  # fmt: skip


def carry_a_v2() -> None:
    d = np.array(list(A_V2_EVENTS.values()))
    print(f"A v2 events n={d.size} mean={d.mean():+.2f} sd={d.std(ddof=1):.2f} min={d.min():+.1f} max={d.max():+.1f}")
    no_kda = np.array([v for k, v in A_V2_EVENTS.items() if k != "KDA"])

    def iid(v: np.ndarray, n: int) -> tuple[float, float]:
        # same draw as setup_passport.r_band (which returns NaN when n > sample size)
        m = v[np.random.default_rng(SEED).integers(0, v.size, (N_BOOT, n))].mean(axis=1)
        lo, hi = np.quantile(m, (0.05, 0.95))
        return float(lo), float(hi)

    for n in (10, 16, 20):
        (a, b), (c, e) = iid(d, n), iid(no_kda, n)
        print(f"mean of {n}: iid {a:+.1f} … {b:+.1f}; without KDA {c:+.1f} … {e:+.1f}")
    # share-in-[min; max] rule, threshold 80 %: false-fail probability if p = 15/17 (new point inside the range)
    from math import ceil, comb

    p = 15 / 17
    for n in (10, 20):
        k = ceil(0.8 * n)
        ff = sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k))
        print(f"[min; max] rule, n={n}: need {k}, false fail {ff:.3f}")


def passport(setup: str, tf: str | None) -> None:
    import verify_setup_passport as vp

    home = Path.home() / ".cache/tradedesk-agents"
    vp.OUT["tf"] = tf
    T = vp.load_rows(home / "setup_passport" / setup)
    period = T["t_in"] >= vp.PASSPORT_MS
    demo = (T["rr"] >= vp.MIN_RR) & (T["stop_pct"] <= vp.MAX_STOP) & (T["score"] >= vp.MIN_SCORE)
    turn = np.nan_to_num(T["turn"], nan=-1) >= vp.SETUPS[setup][2]
    elig = vp.s1.eligible_syms(home / "funding_crowding")
    qv = vp.s1.daily_qv(home / "course_s14_ema", home / "funding_crowding", elig)
    top_days = vp.top_n_days(qv, vp.top_n_of(setup))
    sel = vp.chain(T, period & demo & turn & vp.in_prev_day_top(T, top_days), vp.BAR_CAP.get(setup))
    if setup in vp.LIMIT:  # chain on placements, statistics on filled trades (as stage_report)
        sel = sel & T["filled"]
    idx = np.flatnonzero(sel & (T["t_in"] >= vp.OOS_MS))
    idx = idx[np.argsort(T["t_in"][idx], kind="stable")]
    v = T["B"][idx]
    v = v[np.isfinite(v)]
    print(f"{setup} OOS top-{vp.top_n_of(setup)} n={v.size} mean={v.mean():+.3f}")
    for n in (30, 50, 100):
        b = sp.r_band(v, n, reps=vp.N_BOOT, seed=vp.SEED)
        lo, hi = min(b["iid_p5"], b["roll_p5"]), max(b["iid_p95"], b["roll_p95"])
        print(
            f"n={n}: iid {b['iid_p5']:+.3f} … {b['iid_p95']:+.3f}; rolling ({int(b['windows'])} windows) "
            f"{b['roll_p5']:+.3f} … {b['roll_p95']:+.3f}; decision band {lo:+.3f} … {hi:+.3f}"
        )


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "S3"
    if arg == "carry-a-v2":
        carry_a_v2()
    else:
        passport(arg, sys.argv[2] if len(sys.argv) > 2 else None)
