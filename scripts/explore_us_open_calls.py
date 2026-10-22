"""Exploratory (Claude, 22.10, owner's question on rule 8; NOT a registered test, see IDEA-20261022-catalyst-option-scalp): buying a daily BTC call/put before the US open and selling 1-2 h after it.

Binance USD-M 5m BTCUSDT 2020-12..2026-10 (agents' cache). Option values by Black-Scholes (r=0, spot as forward),
IV proxy = trailing 7-day realized vol / 0.9 (event-day-straddle study: realized/implied ~0.90), same IV at exit
(no crush: generous to the buyer). Daily expiry = next 08:00 UTC. Exploratory, not a registered test.
"""

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import NormalDist
from zoneinfo import ZoneInfo

import numpy as np

N = NormalDist().cdf
NY = ZoneInfo("America/New_York")
SRC = Path.home() / ".cache/tradedesk-agents/session_stop_hunt/k5m/BTCUSDT"
bars = []
for f in sorted(SRC.glob("*.json")):
    bars += json.loads(f.read_text()) or []
ts = np.array([b[0] / 1000 for b in bars])
close = np.array([b[4] for b in bars])
idx = {int(t): i for i, t in enumerate(ts)}
lr = np.diff(np.log(close))


def at(t: datetime):
    return idx.get(int(t.timestamp()) // 300 * 300)


def call(s, k, sig, t_years, put=False):
    if t_years <= 0:
        return max(0.0, (k - s) if put else (s - k))
    d1 = (math.log(s / k) + 0.5 * sig * sig * t_years) / (sig * math.sqrt(t_years))
    d2 = d1 - sig * math.sqrt(t_years)
    c = s * N(d1) - k * N(d2)
    return c - s + k if put else c


rows = []
day = datetime(2021, 1, 4, tzinfo=UTC)
end = datetime(2026, 10, 20, tzinfo=UTC)
while day < end:
    d = day
    day += timedelta(days=1)
    open_utc = datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY).astimezone(UTC)
    expiry = datetime(d.year, d.month, d.day, 8, 0, tzinfo=UTC) + timedelta(days=1)
    for lead_h in (4, 1):
        entry = open_utc - timedelta(hours=lead_h)
        i0, im = at(entry), at(datetime(d.year, d.month, d.day, tzinfo=UTC))
        if i0 is None or im is None or i0 < 2016:
            continue
        rv = lr[i0 - 2016 : i0].std() * math.sqrt(105120)
        iv = rv / 0.9
        s0 = close[i0]
        morning = math.log(s0 / close[im])
        for hold_h in (1, 2):
            ex = open_utc + timedelta(hours=hold_h)
            ie = at(ex)
            if ie is None:
                continue
            s1 = close[ie]
            t0 = (expiry - entry).total_seconds() / (365 * 86400)
            t1 = (expiry - ex).total_seconds() / (365 * 86400)
            c0, c1 = call(s0, s0, iv, t0), call(s1, s0, iv, t1)
            p0, p1 = call(s0, s0, iv, t0, True), call(s1, s0, iv, t1, True)
            rows.append(
                dict(
                    wd=d.weekday(),
                    lead=lead_h,
                    hold=hold_h,
                    morning=morning,
                    r=math.log(s1 / s0),
                    mc=c1 / c0,
                    mp=p1 / p0,
                    prem=c0 / s0,
                    iv=iv,
                    sd=iv * math.sqrt(t0 - t1),
                )
            )


def summary(sel, label, side):
    if not sel:
        print(f"{label:58s} n=0")
        return
    m = np.array([x["mc" if side == "call" else "mp"] for x in sel])
    r = np.array([x["r"] for x in sel]) * (1 if side == "call" else -1)
    net = m * 0.9  # ~10 % of premium round trip: spread + fees on short-dated options
    boot = [np.mean(np.random.default_rng(s).choice(net, len(net))) for s in range(400)]
    print(
        f"{label:58s} n={len(sel):4d} hit(spot)={np.mean(r > 0):.0%} meanR={np.mean(r) * 100:+.2f}% "
        f"x2={np.mean(m >= 2):.1%} x3={np.mean(m >= 3):.1%} mean mult={np.mean(m):.2f} net={np.mean(net):.2f} "
        f"[{np.percentile(boot, 2.5):.2f};{np.percentile(boot, 97.5):.2f}] median={np.median(m):.2f}"
    )


wk = [x for x in rows if x["wd"] < 5]
we = [x for x in rows if x["wd"] >= 5]
print(
    "premium (ATM call, % spot): median",
    round(np.median([x["prem"] for x in wk]) * 100, 2),
    " IV median",
    round(np.median([x["iv"] for x in wk]), 2),
)
for lead in (4, 1):
    for hold in (1, 2):
        base = [x for x in wk if x["lead"] == lead and x["hold"] == hold]
        plc = [x for x in we if x["lead"] == lead and x["hold"] == hold]
        print(f"\n== entry {lead} h before the US open, exit {hold} h after it")
        summary(base, "weekdays, any morning, CALL", "call")
        summary(base, "weekdays, any morning, PUT", "put")
        summary([x for x in base if x["morning"] <= -0.01], "weekdays, morning <= -1%, CALL (owner's case)", "call")
        summary([x for x in base if x["morning"] <= -0.02], "weekdays, morning <= -2%, CALL", "call")
        summary([x for x in base if x["morning"] >= 0.01], "weekdays, morning >= +1%, PUT", "put")
        summary([x for x in plc if x["morning"] <= -0.01], "placebo weekend same clock, morning <= -1%, CALL", "call")
        summary([x for x in base if x["morning"] <= -0.01 and x["r"] > 0], "  (hindsight) owner's case when spot did rise, CALL", "call")
