"""Bollinger Bands mean reversion with a fixed percent stop (TradingView "CM - Bollinger Bands Mean Reversion
[CriptoMatiko]", tested by its author on ETH/USDT 4h, Bybit linear futures): long when a bar closes below the lower
Bollinger Band (20, 2.0), short when it closes above the upper band. Stop loss 1.0 % from the entry, take profit 2.0 R
(= 2.0 % from the entry); the script also exits when a bar closes beyond the opposite band.

Source parameters, used unchanged: BB period 20, 2.0 standard deviations (sample standard deviation, ddof = 1, as the
page states), stop 1.0 %, take profit 2.0 R, 4h bars, both sides. The author notes that BB(20, 2) shorts lost in his
sample and that the stop matters more than the band parameters; no parameter is changed on that account.

Sieve adaptation: the source fills at the next bar's open, the sieve enters at the close of the signal bar. The exit
"close beyond the opposite band" needs future bars, and the source's page does not bound the holding time.

Caveats (critic review 2026-10-28): the author chose the 1 % stop and 2R target by grid search on ETH 4h from
2025-03 to 2026-08, which overlaps the sieve OOS window (ends 2026-03-01) and the hidden period, so passing OOS is
not independent confirmation. The source has shorts off by default ("Allow Short" is optional); this spec trades both
sides, so report long and short separately.

Default added by the harvester: time exit after ``hold_bars`` = 20 bars (80 hours). No variants."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "cm_bb_close_beyond"
SOURCE = "TradingView, CM - Bollinger Bands Mean Reversion [CriptoMatiko] (https://br.tradingview.com/script/UfVSoNrD-CM-Bollinger-Bands-Mean-Reversion-CriptoMatiko/)"
TIMEFRAMES = (4,)
MANAGEMENT = "strategy"
PARAMS = {"bb_n": 20, "bb_k": 2.0, "stop_pct": 0.01, "tp_r": 2.0, "hold_bars": 20}


def signals(bars: Bars, bb_n: int = 20, bb_k: float = 2.0, stop_pct: float = 0.01, tp_r: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    n = int(bb_n)
    pos = segment_pos(bars.t)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(len(bars)):
        if pos[i] < n - 1:
            continue
        w = bars.c[i - n + 1 : i + 1]
        m, s = float(np.mean(w)), float(np.std(w, ddof=1))
        if not s > 0:
            continue
        e = float(bars.c[i])
        if e < m - bb_k * s:
            out.append(Signal(bars.close_time(i), "long", e, e * (1.0 - stop_pct), e * (1.0 + tp_r * stop_pct), hold_ms))
        elif e > m + bb_k * s:
            out.append(Signal(bars.close_time(i), "short", e, e * (1.0 + stop_pct), e * (1.0 - tp_r * stop_pct), hold_ms))
    return out
