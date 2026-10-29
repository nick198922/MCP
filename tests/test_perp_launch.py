"""Binance perp launch of an existing token: title parsing, launch time L, eligibility, placebo outcome."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime

import pytest

from tradedesk.analytics.perp_launch import (
    DAY_MS,
    HOUR_MS,
    beta,
    body_text,
    classify_launch_title,
    collision_ratio,
    eligible,
    hedged_short,
    launch_from_title,
    launch_times,
    live_at,
    placebo_hours,
    venue_symbols,
)


def ms(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp() * 1000)


@pytest.mark.parametrize(
    ("title", "kind", "syms"),
    [
        ("Binance Futures Will Launch USDⓈ-Margined HYPEUSDT Perpetual Contract", "launch", ["HYPEUSDT"]),
        (
            "Binance Futures Will Launch USDⓈ-Margined CROSSUSDT and AINUSDT Perpetual Contracts (2025-07-10)",
            "launch",
            ["CROSSUSDT", "AINUSDT"],
        ),
        ("Binance Futures Will Launch USDⓈ-Margined 1000000BOBUSDT Perpetual Contract (2025-06-05)", "launch", ["1000000BOBUSDT"]),
        ("Binance Futures Will Launch USDⓈ-M ORDI Perpetual Contract With Up to 50x Leverage", "launch", ["ORDIUSDT"]),
        (
            "Binance Futures Will Launch USDⓈ-M POLYX and GAS Perpetual Contracts With Up to 50x Leverage",
            "launch",
            ["POLYXUSDT", "GASUSDT"],
        ),
        ("Binance Futures Will Launch USDⓈ-M Threshold (T) Perpetual Contracts with Up to 20X Leverage", "launch", ["TUSDT"]),
        (
            "Binance Futures Will Launch USDT-Margined AMB, LEVER and TLM Perpetual Contracts with Up to 20X Leverage",
            "launch",
            ["AMBUSDT", "LEVERUSDT", "TLMUSDT"],
        ),
        ("Binance Futures Will Launch MYROUSDT and 1000PEPEUSDC Perpetual Contracts With Up to 50x Leverage", "launch", ["MYROUSDT"]),
        ("Binance Futures Will Launch USDⓈ-Margined PUMPUSDT Perpetual Contract Pre-Market Trading (2025-07-10)", "premarket", []),
        ("Binance Futures Will Launch USDⓈ-Margined AAVEUSDC and UNIUSDC Perpetual Contracts (2025-06-16)", "usdc", []),
        ("Binance Futures Will Launch USDC-Margined DOGE Perpetual Contract With Up to 75x Leverage", "other_futures", []),
        ("Binance Futures Will Launch BUSD-Margined AGIX Perpetual Contracts", "other_futures", []),
        ("Binance Futures Will Launch WIFUSD COIN-M Perpetual Contract with up to 20x Leverage", "other_futures", []),
        ("Binance Futures Will Launch MUUSDT and SNDKUSDT USDⓈ-Margined Equity Perpetual Contracts (2026-04-07)", "tradfi", []),
        ("Binance Futures Will Launch EWJUSDT USDⓈ-Margined Index Perpetual Contract (2026-03-19)", "tradfi", []),
        ("Binance Futures Will Launch Multiple USDⓈ-Margined Perpetual Contracts (2026-09-18)", "multiple", []),
        ("Binance Futures Will Launch USDⓈ-Margined 龙虾USDT Perpetual Contract (2026-03-11)", "other_futures", []),
        ("Binance Will List Hyperliquid (HYPE)", "none", []),
    ],
)
def test_classify_launch_title(title: str, kind: str, syms: list[str]) -> None:
    assert classify_launch_title(title) == (kind, syms)


def test_body_text_flattens_cms_json() -> None:
    body = json.dumps(
        {
            "node": "root",
            "child": [{"node": "p", "child": [{"text": "Binance Futures will launch "}, {"text": "XUSDT at 2025-01-02 10:00 (UTC)."}]}],
        }
    )
    assert body_text(body) == "Binance Futures will launch XUSDT at 2025-01-02 10:00 (UTC)."
    assert body_text("<p>plain <b>html</b></p>") == "plain html"


def test_launch_times_single_and_multi() -> None:
    rel = ms("2025-07-10 10:00")
    text = (
        "Binance Futures will launch the USDⓈ-margined CROSSUSDT perpetual contract at 2025-07-10 12:00 (UTC), "
        "and AINUSDT perpetual contract at 2025-07-10 12:15 (UTC). Trading campaign ends 2025-08-30 00:00 (UTC)."
    )
    assert launch_times(text, ["CROSSUSDT", "AINUSDT"], rel) == {"CROSSUSDT": ms("2025-07-10 12:00"), "AINUSDT": ms("2025-07-10 12:15")}
    one = "Binance Futures will launch the USDⓈ-M ORDI perpetual contract on 2023-11-07 10:00 (UTC) with up to 50x leverage."
    assert launch_times(one, ["ORDIUSDT"], ms("2023-11-07 08:30")) == {"ORDIUSDT": ms("2023-11-07 10:00")}


def test_launch_times_spec_table_columns() -> None:
    rel = ms("2023-02-15 10:00")
    text = (
        "Binance Futures will launch USDⓈ-M AGIX and PHB perpetual contracts on 2023-02-16 14:15 (UTC) and 14:30. More on "
        "AGIXUSDT and PHBUSDT below:&nbsp; USDⓈ-M Perpetual Contracts AGIXUSDT PHBUSDT Launch Time&nbsp; 2023-02-16 14:15 (UTC)&nbsp; "
        "2023-02-16 14:30 (UTC)&nbsp; Underlying Asset AGIX PHB"
    )
    assert launch_times(text, ["AGIXUSDT", "PHBUSDT"], rel) == {"AGIXUSDT": ms("2023-02-16 14:15"), "PHBUSDT": ms("2023-02-16 14:30")}


def test_launch_times_list_format_stamp_before_symbol() -> None:
    rel = ms("2026-09-18 07:00")
    text = (
        "will launch the following: 2026-09-18 09:05 (UTC): AMCUSDT USDT-Priced Perpetual Contract&nbsp; "
        "2026-09-18 09:10 (UTC): CYPHUSDT USDT-Priced Perpetual Contract&nbsp; 2026-09-18 09:15 (UTC): ANETUSDT Perpetual"
    )
    got = launch_times(text, ["AMCUSDT", "CYPHUSDT", "ANETUSDT"], rel)
    assert got == {"AMCUSDT": ms("2026-09-18 09:05"), "CYPHUSDT": ms("2026-09-18 09:10"), "ANETUSDT": ms("2026-09-18 09:15")}


def test_launch_times_shared_stamp_and_out_of_window() -> None:
    rel = ms("2024-03-05 08:00")
    shared = "Binance Futures will launch MYROUSDT and 1000PEPEUSDC perpetual contracts at 2024-03-05 09:00 (UTC)."
    assert launch_times(shared, ["MYROUSDT"], rel) == {"MYROUSDT": ms("2024-03-05 09:00")}
    # a stamp before the release (e.g. a reference to an older notice) and one 30 days later are ignored
    text = "Following the notice of 2024-03-01 10:00 (UTC), XUSDT will launch at 2024-04-05 09:00 (UTC)."
    assert launch_times(text, ["XUSDT"], rel) == {"XUSDT": None}


def test_launch_from_title_and_venue_symbols() -> None:
    assert launch_from_title("1000BONKUSDT") == ("BONK", 1000)
    assert launch_from_title("1INCHUSDT") == ("1INCH", 1)
    v = venue_symbols("PEPE")
    assert v["bybit"][:2] == ["PEPEUSDT", "1000PEPEUSDT"]
    assert v["hyperliquid"] == ["PEPE", "kPEPE"]


def test_eligible_age() -> None:
    ann = ms("2025-05-01 00:00")
    assert eligible(ann - 14 * DAY_MS, ann)
    assert not eligible(ann - 14 * DAY_MS + 1, ann)
    assert not eligible(None, ann)


def test_live_at_boundaries() -> None:
    ann = ms("2025-05-01 08:00")
    assert live_at(ann - 2 * DAY_MS, 12, ann)  # last daily bar exactly 2 days old, 12 of 14 days traded
    assert not live_at(ann - 2 * DAY_MS - 1, 14, ann)  # older than 2 days: stopped trading
    assert not live_at(ann - DAY_MS, 11, ann)  # too few traded days
    assert live_at(ann - DAY_MS, 14, ann)
    assert not live_at(None, 14, ann)


def _bars(start: int, closes: list[float]) -> list[list[float]]:
    return [[start + i * HOUR_MS, c, c, c, c, 1.0] for i, c in enumerate(closes)]


def test_collision_ratio_pump_is_not_a_collision() -> None:
    """MOODENG-like: Bybit doubles between the announcement (08:00) and L (10:30); Binance opens at the Bybit level
    just before L -> ratio ~ 1 (same asset). Multipliers are removed per token."""
    ann, launch = ms("2024-10-25 08:00"), ms("2024-10-25 10:30")
    by = _bars(ms("2024-10-25 06:00"), [0.10, 0.10, 0.15, 0.20, 0.50])  # bars 06..10; the 09:00 bar closes 0.20
    bn = [[ms("2024-10-25 10:00"), 0.201], [ms("2024-10-25 11:00"), 0.9]]
    assert collision_ratio(by, bn, launch) == pytest.approx(0.201 / 0.20)
    # the first version (Bybit 1 h before the announcement) would have flagged it: 0.201 / 0.10 > 2
    assert 0.201 / by[1][4] > 2.0 and ann > by[1][0]
    # a different asset: Binance trades at 1/100 of Bybit just before L
    assert collision_ratio(by, [[ms("2024-10-25 10:00"), 0.002]], launch) == pytest.approx(0.01)
    # multipliers: Bybit 1000X at 1000 x the per-token price, Binance X per token
    by_k = _bars(ms("2024-10-25 08:00"), [100.0, 200.0, 999.0])
    assert collision_ratio(by_k, [[ms("2024-10-25 10:00"), 0.2]], launch, bybit_mult=1000) == pytest.approx(1.0)
    # missing pre-L Bybit bar or no Binance bar -> None
    assert collision_ratio(by[:3], bn, launch) is None
    assert collision_ratio(by, [], launch) is None


def test_collision_ratio_reads_no_bybit_bar_after_l() -> None:
    launch = ms("2024-10-25 10:30")
    by = _bars(ms("2024-10-25 06:00"), [1.0, 1.0, 1.0, 1.0, 1.0, 1.0])
    bn = [[ms("2024-10-25 10:00"), 1.0]]
    base = collision_ratio(by, bn, launch)
    later = [list(x) for x in by]
    for x in later:
        if x[0] >= ms("2024-10-25 10:00"):
            x[1:5] = [50.0] * 4
    assert collision_ratio(later, bn, launch) == base == pytest.approx(1.0)


def _paths(n_h: int, k: float) -> tuple[list[list[float]], list[list[float]]]:
    """BTC with alternating daily log returns +-1 % (and a trend), alt = exp(k * log BTC): beta = k exactly."""
    t0 = ms("2025-01-01 00:00")
    lb = [0.0]
    for i in range(1, n_h):
        day = i // 24
        lb.append(lb[-1] + (0.01 if day % 2 else -0.007) / 24)
    btc = _bars(t0, [100 * math.exp(x) for x in lb])
    alt = _bars(t0, [5 * math.exp(k * x) for x in lb])
    return alt, btc


def test_beta_analytic_and_causal() -> None:
    alt, btc = _paths(24 * 90, 1.7)
    t = ms("2025-03-15 05:00")
    b = beta(alt, btc, t)
    assert b == pytest.approx(1.7, abs=1e-9)
    # bars after t do not change the beta at t
    alt2 = [r if r[0] < t else [r[0], 1.0, 1.0, 1.0, 1.0, 1.0] for r in alt]
    assert beta(alt2, btc, t) == pytest.approx(b, abs=1e-12)
    assert beta(alt[: 24 * 20], btc[: 24 * 20], ms("2025-01-20 00:00")) is None  # < 30 days


def test_hedged_short_analytic() -> None:
    t = ms("2025-01-01 00:00")
    alt = _bars(t, [10.0] + [10.0] * 23 + [9.0])  # -10 % in 24 h
    btc = _bars(t, [100.0] + [100.0] * 23 + [102.0])  # +2 %
    assert hedged_short(alt, btc, t, 1.5, cost=0.0025) == pytest.approx(0.10 + 1.5 * 0.02 - 0.0025)
    assert hedged_short(alt[:10], btc, t, 1.0) is None


def test_placebo_hours_reproducible_and_blocked() -> None:
    a, z = ms("2025-05-01 00:00"), ms("2025-06-01 00:00")
    ann = ms("2025-05-15 12:00")
    h1 = placebo_hours("X@1", a, z, 20, blocked=[ann])
    assert h1 == placebo_hours("X@1", a, z, 20, blocked=[ann])
    assert len(h1) == 20
    assert all(a <= t < z - DAY_MS and abs(t - ann) >= 4 * DAY_MS and t % HOUR_MS == 0 for t in h1)
    assert h1 != placebo_hours("Y@1", a, z, 20, blocked=[ann])
