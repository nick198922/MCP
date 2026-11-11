#!/usr/bin/env python3
"""Item 318 of IDEA-20261102-standing-improvements: the band of the mean R of n trades of the S12 v4 base (the sample of
``PASSPORTS["v4"]``, ``verify_s12_modules.py`` stage report, «V OOS») for n = 30 (must reproduce the passport) and n = 100
(the S12 drop threshold of ``checkpoint.S12_DROP_FLOOR``). Local caches only, no network.

    uv run python scripts/verify_s12_band_n100.py
"""

from __future__ import annotations

import argparse
import resource
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_s12_modules as v12  # noqa: E402
import verify_setup_passport as vsp  # noqa: E402

from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics.s12_demo_audit import PASSPORTS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    home = Path.home() / ".cache/tradedesk-agents"
    ap.add_argument("--cache-dir", type=Path, default=home / "s12_modules")
    ap.add_argument("--s1-cache", type=Path, default=home / "course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=home / "funding_crowding")
    a = ap.parse_args()
    T, _h2, _q, _why = v12.load(a.cache_dir / "S12")
    # the masks of verify_s12_modules.stage_report, unchanged
    qv = vsp.s1.daily_qv(a.s1_cache, a.data_dir, vsp.s1.eligible_syms(a.data_dir))
    top_days = vsp.top_n_days(qv, vsp.TOP_N)
    guard = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn5 = np.nan_to_num(T["turn"], nan=-1) >= 5e6
    top100 = vsp.in_prev_day_top(T, top_days)
    post = T["t_in"] >= vsp.PASSPORT_MS
    V = vsp.chain(T, top100 & turn5 & guard & post)
    oos = T["t_in"] >= vsp.OOS_MS
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"rows {T['B'].size}, peak RSS {rss:.0f} MB, reps {vsp.N_BOOT}, seed {vsp.SEED}")
    out: dict[tuple[str, int], dict[str, float]] = {}
    for lab, msk in (("OOS", V & oos), ("2021+", V)):
        ii = np.flatnonzero(msk)
        ii = ii[np.argsort(T["t_in"][ii], kind="stable")]
        v = T["B"][ii]
        t0, t1 = (datetime.fromtimestamp(int(x) / 1000, UTC).date() for x in (T["t_in"][ii][0], T["t_in"][ii][-1]))
        print(f"{lab}: n {v.size}, mean R {np.mean(v):+.4f}, sd {np.std(v, ddof=1):.3f}, entries {t0}..{t1}")
        for n in (30, 100):
            b = sp.r_band(v, n, reps=vsp.N_BOOT, seed=vsp.SEED)
            out[(lab, n)] = b
            print(
                f"  n={n:3d}: iid {b['iid_p5']:+.3f} / {b['iid_p50']:+.3f} / {b['iid_p95']:+.3f}; "
                f"rolling {b['roll_p5']:+.3f} / {b['roll_p50']:+.3f} / {b['roll_p95']:+.3f} (windows {int(b['windows'])})"
            )
    p = PASSPORTS["v4"]
    b30 = out[("OOS", 30)]
    got = (b30["iid_p5"], b30["iid_p50"], b30["iid_p95"], b30["roll_p5"], b30["roll_p50"], b30["roll_p95"])
    ok = all(abs(x - y) <= 0.001 for x, y in zip(got, (*p.band_iid, *p.band_sliding), strict=True))
    print(f"n=30 OOS vs PASSPORTS['v4']: {'reproduced' if ok else 'NOT reproduced'} (±0.001)")
    b100 = out[("OOS", 100)]
    print(
        f"S12_DROP_FLOOR (rolling p5, n=100, OOS): {b100['roll_p5']:+.3f}; sqrt(30/100) estimate "
        f"{p.band_sliding[0] * (30 / 100) ** 0.5:+.3f}; old constant -0.50"
    )
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"peak RSS {rss:.0f} MB")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
