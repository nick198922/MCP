#!/usr/bin/env python3
"""Exploratory check (after results, not registered; docs/verification/2026-10-30-passport-s12v7-s28.md): why the
S12 v7 passport (about 0R) differs from the +0.185R of ``verify_prelimit_entries.py`` (lim_mid, 2026-10-26).

Reads the S12 candidate files of ``verify_setup_passport.py S12 --stage run`` (no network) and recomputes the same
placements under several occupancy chains. The 26.10 chain (per symbol and side; only a FILLED trade blocks, and a
later placement is checked against it at placement time) keeps a limit placed while an earlier one is pending only if
that earlier one never fills: future information. The causal analogue blocks from the fill time only; it is NOT a
one-position rule (a limit may be placed while an earlier one is pending, and if both fill, two positions are open on
the same symbol and side at once). The demo chain (symbol busy from placement) is the one-position rule.

    uv run python scripts/check_prelimit_chain.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_setup_passport as vp  # noqa: E402

from tradedesk.analytics import setup_passport as sp  # noqa: E402

CACHE = Path.home() / ".cache/tradedesk-agents"


def main() -> int:
    T = vp.load_rows(CACHE / "setup_passport/S12")
    data_dir, s1c = CACHE / "funding_crowding", CACHE / "course_s14_ema"
    qv = vp.s1.daily_qv(s1c, data_dir, vp.s1.eligible_syms(data_dir))
    top100 = vp.in_prev_day_top(T, vp.top_n_days(qv, 100))
    guard = (T["rr"] >= vp.MIN_RR) & (T["stop_pct"] <= vp.MAX_STOP)
    score = T["score"] >= vp.MIN_SCORE
    turn5 = np.nan_to_num(T["turn"], nan=-1) >= 5e6
    F, p21, OOS = T["filled"], T["t_in"] >= vp.PASSPORT_MS, T["t_in"] >= vp.OOS_MS
    t_fill = np.array([int(x) for x in T["t_fill"]])

    def order(m: np.ndarray) -> np.ndarray:
        idx = np.flatnonzero(m)
        return idx[np.lexsort((T["order"][idx], T["t_in"][idx]))]

    def chain_26_10(m: np.ndarray, rk: str = "B", xk: str = "B_x") -> np.ndarray:
        """verify_prelimit_entries.run_symbol: per symbol + side, busy = exit of the last filled kept trade."""
        busy: dict[tuple[str, str], int] = {}
        keep = np.zeros(m.size, bool)
        for k in order(m):
            key = (str(T["sym"][k]), str(T["side"][k]))
            if int(T["t_in"][k]) < busy.get(key, -1):
                continue
            if np.isfinite(T[rk][k]):
                keep[k] = True
                busy[key] = int(T[xk][k])
        return keep

    def chain_causal(m: np.ndarray) -> np.ndarray:
        """Causal analogue: a kept trade blocks only between its fill and its exit (known at a later placement).

        Not a one-position rule: two kept limits of one symbol and side may both be open at the same time.
        """
        open_: dict[tuple[str, str], list[tuple[int, int]]] = {}
        keep = np.zeros(m.size, bool)
        for k in order(m):
            key = (str(T["sym"][k]), str(T["side"][k]))
            t0 = int(T["t_in"][k])
            if any(f <= t0 < x for f, x in open_.get(key, [])):
                continue
            if np.isfinite(T["B"][k]):
                keep[k] = True
                open_.setdefault(key, []).append((int(t_fill[k]), int(T["B_x"][k])))
        return keep

    def show(lab: str, m: np.ndarray, rk: str = "B") -> None:
        ok = m & np.isfinite(T[rk])
        r = sp.mean_ci(T[rk][ok], T["week"][ok], n_boot=2000, seed=1)
        o = ok & OOS
        print(f"{lab:70s} n {int(r['n']):6d} {r['m']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}]  OOS n {int(o.sum())} {np.mean(T[rk][o]):+.3f}")

    base = T["top30"] & guard
    show("top30 guard 2020-02+, touch, 26.10 chain (known number)", chain_26_10(base, "T", "T_x"), "T")
    show("top30 guard 2020-02+, through, 26.10 chain", chain_26_10(base))
    show("top30 guard 2020-02+, through, causal analogue (fill->exit, 2 open allowed)", chain_causal(base))
    show("top30 guard 2020-02+, through, no chain", base & F)
    show("top30 guard 2020-02+, through, demo chain (per symbol, from placement)", vp.chain(T, base) & F)
    wide = top100 & turn5 & guard & p21
    show("top100 >= $5M guard 2021+, 26.10 chain", chain_26_10(wide))
    show("top100 >= $5M guard 2021+, causal analogue (2 open allowed)", chain_causal(wide))
    show("top100 >= $5M guard 2021+, no chain", wide & F)
    show("passport verdict sample (top100 >= $5M, demo selection, demo chain)", vp.chain(T, wide & score) & F)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
