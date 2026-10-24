"""Counter-trend entries of the demo trader: do trades that reach score >= 2 only through R:R do worse?

Pre-registered 2026-10-24 before reading R (owner asked why the demo takes only longs in a falling market):
  live-eligible set E = score >= 2, rr >= 1.5, stop <= 10 %  (the live gates; rr after fees as in the cache)
  H1 (main): in E, trades with no trend point (coin daily trend and BTC daily trend both not in the trade's direction)
      vs trades with >= 1 trend point. Delta = E[R] no-trend - E[R] trend.
  H2: in E, alt trades with BTC daily trend AGAINST the trade vs the rest.
  H3: rule "R:R gives at most one point" (score' = trend points + [rr >= 2]); dropped = E minus {score' >= 2}.
  Decision: a filter is worth a tagged demo version if the dropped group is worse by >= 0.05R in IS with the 95 % CI
  excluding 0, and the OOS sign agrees. Cluster bootstrap by entry date, 10 000 draws. Split by side reported.
"""

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tradedesk.analytics.demo_context_filters import cluster_boot_group_sums  # noqa: E402

C = Path.home() / ".cache/tradedesk-agents"
F = json.loads((C / "demo_context_filters/features.json").read_text())
R = {}
for p in sorted((C / "demo_stop_management/trades").glob("*.json")):
    for r in json.loads(p.read_text())["rows"]:
        R[(r["sym"], r["setup"], r["tf"], r["side"], r["entry_ms"])] = r["B_r"]
T = {
    k: np.asarray([x[k] for x in F])
    for k in ("sym", "setup", "tf", "side", "day", "oos", "rr", "stop_pct", "coin_trend", "btc_trend", "score")
}
T["r"] = np.asarray([R[(x["sym"], x["setup"], x["tf"], x["side"], x["entry_ms"])] for x in F])
want = np.where(T["side"] == "long", 1, -1)
alt = ~np.isin(T["sym"], ["BTCUSDT", "ETHUSDT"])
coin_ok = T["coin_trend"] == want
btc_ok = (T["btc_trend"] == want) & (T["sym"] != "BTCUSDT")
trend_pts = coin_ok.astype(int) + btc_ok.astype(int)
E = (T["score"] >= 2) & (T["rr"] >= 1.5) & (T["stop_pct"] <= 0.10)
score1 = trend_pts + (T["rr"] >= 2)


def boot(mask_drop, base, label):
    for part, sel in (("IS", ~T["oos"]), ("OOS", T["oos"])):
        for side in ("all", "long", "short"):
            m = base & sel & ((T["side"] == side) if side != "all" else True)
            g = np.where(m, np.where(mask_drop, 1, 0), -1)
            n0, n1 = int((g == 0).sum()), int((g == 1).sum())
            if min(n0, n1) < 20:
                print(f"{label:4} {part:3} {side:5} kept {n0:5} dropped {n1:5}  too few")
                continue
            s, c = cluster_boot_group_sums(T["r"], g, T["day"], 2, n_boot=10_000, seed=20261024)
            e0, e1 = T["r"][g == 0].mean(), T["r"][g == 1].mean()
            d = s[:, 0] / c[:, 0] - s[:, 1] / c[:, 1]
            lo, hi = np.nanpercentile(d, [2.5, 97.5])
            print(
                f"{label:4} {part:3} {side:5} kept {n0:5} E[R] {e0:+.3f} | dropped {n1:5} E[R] {e1:+.3f} | "
                f"kept-dropped {e0 - e1:+.3f} [{lo:+.3f}; {hi:+.3f}]"
            )


print(
    "E (live-eligible):", int(E.sum()), "of", len(F), "| by setup:", {s: int((E & (T["setup"] == s)).sum()) for s in np.unique(T["setup"])}
)
boot(trend_pts == 0, E, "H1")
boot(alt & (T["btc_trend"] == -want), E, "H2")
boot(score1 < 2, E, "H3")
print("\nS3 only:")
boot(trend_pts == 0, E & (T["setup"] == "S3"), "H1s3")
boot(alt & (T["btc_trend"] == -want), E & (T["setup"] == "S3"), "H2s3")
