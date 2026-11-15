"""Exploratory: DataHawks video 0L_UFtw02Rc claim, BTC RR25 1M <= -6 pts then turns up -> local bottom.

Daily 09:00 UTC surfaces from the put-skew-v2 cache (Deribit trade marks, 2021-01..2026-10). RR25 at 30 d:
linear in T between the two expiries bracketing 30 d (each in [7, 60] d). Spot = 09:00 index from the cache.
Rule fixed from the video before looking at results: threshold -0.06, turn = +0.01 off the episode minimum.
No network; reads the cache only. Output: docs/verification/2026-11-15-rr25-bottom-btc-explore.md.
    uv run python scripts/explore_rr25_bottom_btc.py
"""

import json
import math
import os
import statistics
from datetime import UTC, datetime

from tradedesk.analytics.put_skew import rr25

DAY = 86_400_000
src = os.path.expanduser("~/.cache/tradedesk-agents/put_skew/snapshots_v2.json")
raw = json.load(open(src))["days"]


def ymd(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


rr, spot = {}, {}
for k, s in raw.items():
    d = int(k)
    spot[d] = s["spot"]
    pts = []
    for _e, x in s["expiries"]:
        dte = x["T"] * 365
        if 7 <= dte <= 60:
            v = rr25({**x, "strikes": dict((float(k), sv) for k, sv in x["strikes"])})
            if v is not None:
                pts.append((dte, v))
    lo = [p for p in pts if p[0] <= 30]
    hi = [p for p in pts if p[0] >= 30]
    if lo and hi:
        a, b = max(lo), min(hi)
        rr[d] = a[1] if b[0] == a[0] else a[1] + (b[1] - a[1]) * (30 - a[0]) / (b[0] - a[0])

days = sorted(spot)
print(f"days {len(days)} {ymd(days[0])}..{ymd(days[-1])}; rr30 on {len(rr)}")
vals = sorted(rr.values())
q = lambda p: vals[int(p * (len(vals) - 1))]  # noqa: E731
print(
    f"rr30 quantiles: 5% {q(0.05):+.3f} 10% {q(0.10):+.3f} 25% {q(0.25):+.3f} 50% {q(0.5):+.3f}; share <= -0.06: {sum(v <= -0.06 for v in vals) / len(vals):.1%}"
)


def fwd(d, h):
    t = d + h * DAY
    return spot[t] / spot[d] - 1 if t in spot else None


def path(d, h):
    xs = [spot[d + i * DAY] for i in range(1, h + 1) if d + i * DAY in spot]
    if len(xs) < h * 0.8:
        return None, None
    return max(xs) / spot[d] - 1, min(xs) / spot[d] - 1


def dd30(d):
    xs = [spot[d - i * DAY] for i in range(0, 31) if d - i * DAY in spot]
    return spot[d] / max(xs) - 1


def events(thr, turn, mode, cooldown=14):
    out, in_ep, mn, last = [], False, None, -(10**15)
    for d in days:
        v = rr.get(d)
        if v is None:
            continue
        if not in_ep and v <= thr and d - last >= cooldown * DAY:
            in_ep, mn = True, v
            if mode == "touch":
                out.append(d)
                last, in_ep = d, False
                continue
        if in_ep:
            mn = min(mn, v)
            if v >= mn + turn:
                out.append(d)
                last, in_ep = d, False
    return out


def summary(name, ev):
    rows = []
    for d in ev:
        r14, r30 = fwd(d, 14), fwd(d, 30)
        up, dn = path(d, 30)
        rows.append((d, rr[d], dd30(d), r14, r30, up, dn))
    ok = [r for r in rows if r[4] is not None]
    print(f"\n== {name}: events {len(rows)} (with 30d fwd {len(ok)})")
    for r in rows:
        f = lambda x: "  n/a" if x is None else f"{x * 100:+5.1f}"  # noqa: E731
        print(
            f"  {ymd(r[0])} rr {r[1] * 100:+5.1f} dd30 {r[2] * 100:+5.1f}% | 14d {f(r[3])} 30d {f(r[4])} | max up30 {f(r[5])} max dn30 {f(r[6])}"
        )
    if ok:
        r14 = [r[3] for r in ok if r[3] is not None]
        print(
            f"  mean 14d {statistics.mean(r14) * 100:+.1f}% (pos {sum(x > 0 for x in r14)}/{len(r14)}); mean 30d {statistics.mean(r[4] for r in ok) * 100:+.1f}% (pos {sum(r[4] > 0 for r in ok)}/{len(ok)}); median max-up30 {statistics.median(r[5] for r in ok if r[5] is not None) * 100:+.1f}%, median max-dn30 {statistics.median(r[6] for r in ok if r[6] is not None) * 100:+.1f}%"
        )
    return rows


def control(lo_dd, hi_dd, label, exclude=()):
    xs14, xs30, ups, dns = [], [], [], []
    for d in days:
        if d not in rr or any(abs(d - e) < 14 * DAY for e in exclude):
            continue
        dd = dd30(d)
        if lo_dd <= dd <= hi_dd:
            a, b = fwd(d, 14), fwd(d, 30)
            up, dn = path(d, 30)
            if a is not None and b is not None and up is not None:
                xs14.append(a)
                xs30.append(b)
                ups.append(up)
                dns.append(dn)
    print(
        f"  control {label}: days {len(xs30)}, mean 14d {statistics.mean(xs14) * 100:+.1f}% (pos {sum(x > 0 for x in xs14) / len(xs14):.0%}), mean 30d {statistics.mean(xs30) * 100:+.1f}% (pos {sum(x > 0 for x in xs30) / len(xs30):.0%}), median max-up30 {statistics.median(ups) * 100:+.1f}%, max-dn30 {statistics.median(dns) * 100:+.1f}%"
    )


print("\nunconditional:")
control(-1, 0.001, "all days")
for mode in ("turn", "touch"):
    for thr in (-0.06, -0.08, -0.10):
        if mode == "touch" and thr != -0.06:
            continue
        ev = events(thr, 0.01, mode)
        rows = summary(f"{mode} thr {thr}", ev)
        dds = [r[2] for r in rows]
        if dds:
            control(min(dds) - 0.0, max(min(-0.05, max(dds)), -0.05), "30d drawdown in events' range, outside +-14d of events", exclude=ev)
            control(-0.20, -0.10, "30d drawdown 10-20 %")
# period split for the main rule
ev = events(-0.06, 0.01, "turn")
cut = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
for lab, sel in (("2021-2023", [d for d in ev if d < cut]), ("2024-2026", [d for d in ev if d >= cut])):
    r = [fwd(d, 30) for d in sel if fwd(d, 30) is not None]
    if r:
        print(f"{lab}: n {len(r)} mean 30d {statistics.mean(r) * 100:+.1f}% pos {sum(x > 0 for x in r)}/{len(r)}")
print(f"\nlast rr30 in cache: {ymd(max(rr))} {rr[max(rr)] * 100:+.1f}")
print("check:", math.isfinite(sum(rr.values())))
