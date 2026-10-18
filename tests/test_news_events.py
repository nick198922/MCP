"""News events: title parsing (real titles from the 2026-10-17 archive), price paths, the delayed short, placebo, stats."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from tradedesk.analytics.news_events import (
    DAY_MS,
    HOUR_MS,
    MIN_MS,
    Bars,
    Event,
    _chi2_sf,
    btc_cross_ratio,
    classify_delisting,
    classify_listing,
    classify_upbit,
    closure_exclusion,
    cluster_bootstrap,
    cluster_sd,
    cochran_q,
    dedupe,
    flat_share,
    hack_events,
    holm,
    max_move,
    mde_clusters,
    move_share_bootstrap,
    move_share_median,
    move_share_pooled,
    one_sided_p,
    paired_diff_bootstrap,
    parse_closures,
    perp_base,
    perp_live,
    placebo_times,
    price_at,
    reaction,
    second_bars,
    short_trade,
    snowflake_ms,
    taker_buy_fill,
    taker_sell_exit,
    upbit_batch_size,
    upbit_body_is_reminder,
    upbit_body_tickers,
    upbit_time_ms,
    vwap_fill,
)

CLOSURES_CSV = Path(__file__).resolve().parent.parent / "docs" / "verification" / "2026-10-17-news-closures.csv"

T0 = 1_760_000_000_000 // MIN_MS * MIN_MS  # a minute boundary


# ------------------------------------------------------------------ titles (real samples, Binance / Bybit / OKX)


@pytest.mark.parametrize(
    ("source", "title", "kind", "ticks"),
    [
        ("binance", "Binance Will Delist EPIC, QUICK on 2026-10-28", "spot_delist", ["EPIC", "QUICK"]),
        ("binance", "Binance Will Delist HNT and WABI on 2023-03-24", "spot_delist", ["HNT", "WABI"]),
        # rebrandings / migrations are excluded as registered, even when the spot pair goes away
        ("binance", "Binance Will Delist UTK and Support Rebranding and Airdrop Plan on Binance Alpha", "ignore", []),
        (
            "binance",
            "Binance Announced the First Batch of Vote to Delist Results and Will Delist BADGER, BAL, BETA on 2025-04-16",
            "spot_delist",
            ["BADGER", "BAL", "BETA"],
        ),
        ("binance", "Binance Will Delist USDP on 2026-09-24", "ignore", []),
        ("binance", "Binance Margin Will Delist ARDR on 2026-03-12", "ignore", []),
        ("binance", "Binance Alpha Will Remove SUP, CUDIS, PYTHIA (2026-10-09)", "ignore", []),
        ("binance", "Notice of Removal of Spot Trading Pairs - 2026-10-02", "ignore", []),
        (
            "binance",
            "Binance Futures Will Delist USDⓈ-M XEMUSDT, ORBSUSDT and LOOMUSDT Perpetual Contracts (2024-12-09)",
            "perp_delist",
            ["XEMUSDT", "ORBSUSDT", "LOOMUSDT"],
        ),
        ("binance", "Binance Futures Will Delist Multiple USDⓈ-M Perpetual Contracts (2026-10-05)", "needs_body", []),
        ("binance", "Delisting of USDⓈ-M OMGUSDT Perpetual Contract Postponed", "ignore", []),
        ("bybit", "Delisting of  RONUSDT Perpetual Contract", "perp_delist", ["RONUSDT"]),
        # known miss of the registered parser: a title naming a USDC perp too is skipped as a USDC-perp delisting
        ("bybit", "Delisting of AI16ZUSDT, AI16ZUSDC Perpetual Contract", "ignore", []),
        ("bybit", "Delisting of DAOUSDT, VGXUSDT, LOOMUSDT Perpetual Contract", "perp_delist", ["DAOUSDT", "VGXUSDT", "LOOMUSDT"]),
        ("bybit", "Delisting of AMI, BDXN, ONE, SIS, XEM and INSP", "spot_delist", ["AMI", "BDXN", "ONE", "SIS", "XEM", "INSP"]),
        ("bybit", "Delisting of Metachain (MTC) on Bybit", "spot_delist", ["MTC"]),
        ("bybit", "Delisting of VANRY on Aug 18, 2026", "spot_delist", ["VANRY"]),
        ("bybit", "Delisting of CBK,FLIP,FUEL", "spot_delist", ["CBK", "FLIP", "FUEL"]),
        ("bybit", "Delisting of BUSD on Bybit", "ignore", []),
        ("bybit", "Bybit to Delist NEXT and Support Migration to CLEAR for Withdrawals", "ignore", []),
        ("bybit", "Discontinuation of KDA  as Collateral and Lending Asset", "ignore", []),
        ("bybit", "Delisting of Selected Perpetual Contracts (Oct 19, 2025)", "needs_body", []),
        ("bybit", "Bybit to Delist 2 Token(s) on Oct 8, 2026", "needs_body", []),
        ("bybit", "Delisting of WIFUSD Inverse Perpetual Contract", "ignore", []),
        (
            "okx",
            "OKX to delist CEL, OMG, SPELL, APM, LHINU, POLS, and DCR spot trading pairs ",
            "spot_delist",
            ["CEL", "OMG", "SPELL", "APM", "LHINU", "POLS", "DCR"],
        ),
        ("okx", "OKX to delist ULTI, GEAR, VRA, DAO, CXT and ELON in EEA", "ignore", []),
        ("okx", "OKX to delist WAVES margin trading pairs and perpetual futures", "ignore", []),
    ],
)
def test_classify_delisting(source: str, title: str, kind: str, ticks: list[str]) -> None:
    assert classify_delisting(source, title) == (kind, ticks)


def test_classify_listing() -> None:
    assert classify_listing("Binance Will List Hyperliquid (HYPE) with Seed Tag Applied") == ["HYPE"]
    assert classify_listing("Binance Will List Usual (USUAL) and Movement (MOVE)") == ["USUAL", "MOVE"]
    assert classify_listing("Binance Futures Will Launch USDⓈ-Margined HYPEUSDT Perpetual Contract") == []
    assert classify_listing("Binance Will Add 4 bStocks Tokenized Securities as Collateral Asset") == []


def test_perp_base_multipliers() -> None:
    assert perp_base("1000PEPEUSDT") == ("PEPE", 1000)
    assert perp_base("1000000CHEEMSUSDT") == ("CHEEMS", 1000000)
    assert perp_base("BLURUSDT") == ("BLUR", 1)
    assert perp_base("1INCHUSDT") == ("1INCH", 1)  # not a multiplier prefix


# ------------------------------------------------------------------ events


def test_dedupe_keeps_first_within_window() -> None:
    a = Event("X", 0, "spot_delist", "binance", "c1")
    b = Event("X", 2 * DAY_MS, "spot_delist", "bybit", "c2")  # inside 72 h of a -> dropped
    c = Event("X", 4 * DAY_MS, "spot_delist", "okx", "c3")  # 4 d after a -> kept
    d = Event("Y", DAY_MS, "spot_delist", "bybit", "c2")
    kept, dropped = dedupe([c, b, d, a])
    assert kept == [a, d, c] and dropped == [b]


def test_hack_events_maps_symbol_and_day() -> None:
    hacks = [
        {"date": 1_700_000_000, "name": "Proto", "amount": 5e6, "defillamaId": "42"},
        {"date": 1_700_000_000, "name": "Small", "amount": 5e5, "defillamaId": "42"},
        {"date": 1_700_000_000, "name": "NoToken", "amount": 9e6, "defillamaId": "7"},
    ]
    protos = [{"id": "42", "symbol": "prt"}, {"id": "7", "symbol": "-"}]
    ev = hack_events(hacks, protos, min_amount=1e6, start_ms=0, end_ms=10**13)
    assert len(ev) == 1
    assert ev[0].token == "PRT" and ev[0].ms == 1_700_000_000_000 // DAY_MS * DAY_MS and not ev[0].precise


# ------------------------------------------------------------------ price paths


def ramp(n: int, p0: float = 100.0, step: float = -0.1, start: int = T0 - 60 * MIN_MS) -> list[list[float]]:
    """Minute bars with closes p0 + step*i, high = close + 0.5, low = close - 0.5."""
    rows = []
    for i in range(n):
        c = p0 + step * i
        rows.append([start + i * MIN_MS, c, c + 0.5, c - 0.5, c])
    return rows


def test_price_at_uses_last_closed_bar_and_staleness() -> None:
    b = Bars.from_rows(ramp(120))
    # the bar opening at T0 - 1 min closes at T0: index 59, close 100 - 5.9
    assert price_at(b, T0) == pytest.approx(94.1)
    # 30 s into the next minute: still the same bar
    assert price_at(b, T0 + 30_000) == pytest.approx(94.1)
    # after the series end + 30 min -> stale
    end = T0 + 60 * MIN_MS
    assert price_at(b, end + 29 * MIN_MS) is not None and price_at(b, end + 31 * MIN_MS) is None


def test_reaction_matches_closed_form() -> None:
    b = Bars.from_rows(ramp(200))
    p0 = 100 - 0.1 * 59
    p15 = 100 - 0.1 * (59 + 15)
    assert reaction(b, T0, 15 * MIN_MS) == pytest.approx(p15 / p0 - 1)


def test_short_trade_gross_funding_costs() -> None:
    b = Bars.from_rows(ramp(3000))
    ent, ex = T0 + 15 * MIN_MS, T0 + 15 * MIN_MS + 24 * HOUR_MS
    p_in = 100 - 0.1 * (59 + 15)
    p_out = 100 - 0.1 * (59 + 15 + 24 * 60)
    fund = [(ent, 0.5), (ent + HOUR_MS, 0.001), (ex, -0.002), (ex + 1, 0.9)]  # only (entry, exit] count
    tr = short_trade(b, ent, ex, cost_side=0.002, funding=fund)
    assert tr is not None
    assert tr.gross == pytest.approx(1 - p_out / p_in)
    assert tr.funding == pytest.approx(-0.001)
    assert tr.cost == pytest.approx(0.004)
    assert tr.net == pytest.approx(tr.gross + tr.funding - tr.cost)
    assert not tr.truncated and not tr.stopped
    # long (sign=-1) is the mirror image before costs
    tl = short_trade(b, ent, ex, cost_side=0.002, funding=fund, sign=-1.0)
    assert tl is not None and tl.gross == pytest.approx(-tr.gross) and tl.funding == pytest.approx(0.001)


def test_short_trade_stop_and_truncation() -> None:
    rows = ramp(100, p0=100.0, step=0.0)
    rows.append([T0 + 40 * MIN_MS, 100, 130, 99, 120])  # last bar (the ramp ends at T0 + 39 min): spike to 130
    b = Bars.from_rows(rows)
    tr = short_trade(b, T0 + 15 * MIN_MS, T0 + 15 * MIN_MS + 24 * HOUR_MS, cost_side=0.0, stop=0.25)
    assert tr is not None and tr.stopped and tr.gross == pytest.approx(-0.25)
    assert tr.exit_ms == T0 + 41 * MIN_MS
    worst = short_trade(b, T0 + 15 * MIN_MS, T0 + 15 * MIN_MS + 24 * HOUR_MS, cost_side=0.0, stop=0.25, stop_fill="extreme")
    assert worst is not None and worst.stopped and worst.gross == pytest.approx(-0.30)
    # without a stop the series ends at T0 + 40 min -> truncated exit at the last close
    tr2 = short_trade(b, T0 + 15 * MIN_MS, T0 + 15 * MIN_MS + 24 * HOUR_MS, cost_side=0.0)
    assert tr2 is not None and tr2.truncated and tr2.gross == pytest.approx(1 - 120 / 100)
    assert tr2.mae == pytest.approx(0.30)


def settled(n_live: int, n_flat: int, *, volume: bool = True, start: int = T0 - DAY_MS) -> list[list[float]]:
    """``n_live`` moving minute bars, then ``n_flat`` bars at the constant settlement price with zero volume (what the
    Binance archive publishes after a perp is delisted)."""
    rows = []
    for i in range(n_live):
        c = 100.0 + (i % 7) * 0.1
        rows.append([start + i * MIN_MS, c, c + 0.2, c - 0.2, c] + ([10.0] if volume else []))
    for i in range(n_live, n_live + n_flat):
        rows.append([start + i * MIN_MS, 100.0, 100.0, 100.0, 100.0] + ([0.0] if volume else []))
    return rows


def test_bars_keep_volume_only_when_every_row_has_it() -> None:
    assert Bars.from_rows(settled(5, 5)).v is not None
    assert Bars.from_rows(settled(5, 5, volume=False)).v is None
    assert Bars.from_rows([*settled(5, 0), [T0 + DAY_MS, 1, 1, 1, 1]]).v is None


def test_perp_live_rejects_flat_post_settlement_bars() -> None:
    day = 24 * 60
    # moving for the whole day before T0 -> live
    assert perp_live(Bars.from_rows(settled(day + 60, 0)), T0)
    # settled long ago: a fresh bar exists (price_at is not None) but the whole 24 h before T0 is flat
    dead = Bars.from_rows(settled(0, day + 60))
    assert price_at(dead, T0) is not None and not perp_live(dead, T0)
    # same without a volume column: the flat 24 h range alone rejects it
    assert not perp_live(Bars.from_rows(settled(0, day + 60, volume=False)), T0)
    # settled 3 h before T0: the 24 h range is still positive, but the last hour has zero volume -> dead
    rows = settled(day - 180, 240)
    assert not perp_live(Bars.from_rows(rows), T0)
    # without volume this case is not caught by the 24 h range rule (documented limitation)
    assert perp_live(Bars.from_rows([r[:5] for r in rows]), T0)
    # a few zero-volume minutes in an otherwise live hour do not kill it
    rows = settled(day + 60, 0)
    for r in rows[day - 5 : day]:
        r[5] = 0.0
    assert perp_live(Bars.from_rows(rows), T0)


def test_perp_live_no_lookahead() -> None:
    day = 24 * 60
    live = settled(day, 0)  # ends exactly at T0
    after = settled(day, 600)  # settled right after T0: the flat bars all close after T0
    assert perp_live(Bars.from_rows(live), T0) == perp_live(Bars.from_rows(after), T0) is True


def test_no_lookahead_results_before_t_ignore_data_after_t() -> None:
    rows = ramp(3000)
    b1 = Bars.from_rows(rows)
    cut = T0 + 2 * HOUR_MS
    rows2 = [r if r[0] + MIN_MS <= cut else [r[0], r[1], r[2] * 5, r[3] / 5, r[4] * 3] for r in rows]
    b2 = Bars.from_rows(rows2)
    for dt in (5 * MIN_MS, 15 * MIN_MS, HOUR_MS):
        assert reaction(b1, T0, dt) == reaction(b2, T0, dt)
    t1 = short_trade(b1, T0 + 15 * MIN_MS, T0 + 90 * MIN_MS, cost_side=0.001, stop=0.25)
    t2 = short_trade(b2, T0 + 15 * MIN_MS, T0 + 90 * MIN_MS, cost_side=0.001, stop=0.25)
    assert t1 == t2
    assert max_move(b1, T0, cut, 100.0) == max_move(b2, T0, cut, 100.0)


# ------------------------------------------------------------------ placebo


def test_placebo_times_reproducible_before_event_and_blocked() -> None:
    t0 = 400 * DAY_MS
    blocked = [t0 - 50 * DAY_MS]
    a = placebo_times("X@1", t0, 20, blocked=blocked)
    b = placebo_times("X@1", t0, 20, blocked=blocked)
    assert a == b and len(a) == 20 and len(set(a)) == 20
    assert all(t0 - 120 * DAY_MS <= t <= t0 - 7 * DAY_MS for t in a)
    assert all(t % HOUR_MS == 0 for t in a)
    assert all(abs(t - blocked[0]) >= 4 * DAY_MS for t in a)
    assert placebo_times("X@2", t0, 20) != a


# ------------------------------------------------------------------ statistics


def test_cluster_bootstrap_mean_and_ci_cover() -> None:
    rng = np.random.default_rng(0)
    vals = list(rng.normal(0.05, 0.1, 400))
    cl = [i // 4 for i in range(400)]
    m, lo, hi = cluster_bootstrap(vals, cl, n_boot=4000)
    assert m == pytest.approx(float(np.mean(vals)))
    assert lo < m < hi
    se = np.std(vals, ddof=1) / math.sqrt(400)
    assert (hi - lo) / 2 == pytest.approx(1.96 * se, rel=0.2)


def test_paired_diff_and_cluster_sd_and_mde() -> None:
    m, lo, hi = paired_diff_bootstrap([0.1, 0.2, 0.3, 0.4], [0.0, 0.1, 0.2, 0.3], ["a", "b", "c", "d"], n_boot=500)
    assert m == pytest.approx(0.1) and lo == pytest.approx(0.1) and hi == pytest.approx(0.1)
    sd, k = cluster_sd([1.0, 3.0, 5.0], ["a", "a", "b"])
    assert k == 2 and sd == pytest.approx(math.sqrt(((2 - 3.5) ** 2 + (5 - 3.5) ** 2) / 1))
    assert mde_clusters(0.2, 100) == pytest.approx(2.8 * 0.02)


def test_cochran_q_and_chi2() -> None:
    q, df, i2, p = cochran_q([0.0, 0.0, 0.0], [1.0, 1.0, 1.0])
    assert q == 0 and df == 2 and i2 == 0 and p == pytest.approx(1.0)
    q, df, i2, p = cochran_q([0.0, 2.0], [1.0, 1.0])
    assert q == pytest.approx(2.0) and df == 1 and i2 == pytest.approx(0.5)
    # chi-square references: sf(3.841, 1) = 0.05, sf(2, 2) = exp(-1), sf(18.307, 10) = 0.05
    assert p == pytest.approx(_chi2_sf(2.0, 1))
    assert _chi2_sf(3.841459, 1) == pytest.approx(0.05, abs=1e-5)
    assert _chi2_sf(2.0, 2) == pytest.approx(math.exp(-1.0), rel=1e-9)
    assert _chi2_sf(18.307038, 10) == pytest.approx(0.05, abs=1e-5)


# ------------------------------------------------------------------ stage 0b: curated closures and speed


def test_snowflake_ms_owner_example() -> None:
    # BLUR post https://x.com/blur_io/status/2109735810857861203 -> 2026-10-12 19:59:48 UTC (card, owner's example)
    want = datetime(2026, 10, 12, 19, 59, 48, tzinfo=UTC).timestamp()
    assert snowflake_ms(2109735810857861203) // 1000 == int(want)


def test_closures_csv_times_match_snowflakes_and_pool() -> None:
    rows = parse_closures(CLOSURES_CSV.read_text())
    assert len(rows) == 33
    for c in rows:  # every t0 is the tweet's own creation time, to the second
        tid = int(re.search(r"/status/(\d+)", c.url).group(1))  # type: ignore[union-attr]
        assert c.ms == snowflake_ms(tid) // 1000 * 1000, c.key
    reasons = [closure_exclusion(c) for c in rows]
    assert reasons.count("leak") == 1 and reasons.count("hack (stage 0d)") == 1
    assert reasons.count("direction prior up") == 1 and reasons.count("hypothesis source") == 2
    assert reasons.count(None) == 28


def test_move_share_closed_forms() -> None:
    r24 = [-0.10, -0.10, -0.02]
    r5 = [-0.05, -0.01, 0.01]
    assert move_share_pooled(r5, r24) == pytest.approx((-0.05 / 3) / (-0.22 / 3))
    med, n = move_share_median(r5, r24)  # only |r24| >= 5 %: ratios 0.5 and 0.1
    assert n == 2 and med == pytest.approx(0.3)
    assert math.isnan(move_share_pooled([0.01], [0.0]))
    # a constant per-event share gives a degenerate bootstrap interval at that share
    s, lo, hi = move_share_bootstrap([0.5 * x for x in (-0.2, -0.1, -0.05, -0.3)], [-0.2, -0.1, -0.05, -0.3], n_boot=500)
    assert s == pytest.approx(0.5) and lo == pytest.approx(0.5) and hi == pytest.approx(0.5)


def test_one_minute_reaction_ignores_data_after_it() -> None:
    rows = ramp(600)
    cut = T0 + 2 * MIN_MS
    rows2 = [r if r[0] + MIN_MS <= cut else [r[0], r[1], r[2] * 4, r[3] / 4, r[4] * 2] for r in rows]
    b1, b2 = Bars.from_rows(rows), Bars.from_rows(rows2)
    for t0 in (T0, T0 + 48_000):  # on the minute and 48 s into it (post times carry seconds)
        assert reaction(b1, t0, MIN_MS) == reaction(b2, t0, MIN_MS)
    assert reaction(b1, T0, 5 * MIN_MS) != reaction(b2, T0, 5 * MIN_MS)  # the change is visible later


# ------------------------------------------------------------------ stage 0c: Upbit titles (real, 2026-10-18 API), seconds


@pytest.mark.parametrize(
    ("title", "kind", "ticks"),
    [
        ("퍼퍼(PUFFER) KRW 마켓 디지털 자산 추가", "listing", ["PUFFER"]),
        ("KRW 마켓 디지털 자산 추가 (MINA)", "listing", ["MINA"]),
        ("뉴메레르(NMR) KRW, USDT 마켓 디지털 자산 추가", "listing", ["NMR"]),
        ("제타체인(ZETA), 옴니네트워크(OMNI) 신규 거래지원 안내 (KRW, BTC, USDT 마켓)", "listing", ["ZETA", "OMNI"]),
        ("주피터(JUP) 신규 거래지원 안내 (KRW, BTC, USDT 마켓) (거래지원 개시 시점 연기 안내)", "listing", ["JUP"]),
        ("렌조(REZ) 신규 거래지원 안내 (USDT 마켓)", "ignore", []),  # no KRW market
        ("BTC, USDT 마켓 신규 거래지원 안내 (BICO, BMT, NIL, GWEI)", "ignore", []),
        ("페이팔유에스디(PYUSD), 제이피와이코인(JPYC) 신규 거래지원 안내 (KRW, BTC, USDT 마켓)", "ignore", []),  # stablecoins
        ("질리카(ZIL) 거래 유의 종목 지정 안내", "warning", ["ZIL"]),
        ("리니어파이낸스(LINA) 유의 종목 지정 안내", "warning", ["LINA"]),
        ("디지털 자산 유의종목 지정 안내 디크레드(DCR)", "warning", ["DCR"]),
        ("질리카(ZIL) 거래 유의 종목 지정 기간 연장 안내", "ignore", []),
        ("샌드박스(SAND) 거래 유의 종목 지정 해제 안내", "ignore", []),
        ("웨이브(WAVES) 유의 촉구 안내", "ignore", []),
        ("질리카(ZIL) 거래지원 종료 안내 (11/16 15:00)", "delist", ["ZIL"]),
        ("던프로토콜(DAWN), 리퍼리움(RFR), 마로(MARO) 거래지원 종료 안내 (09/26 16:00)", "delist", ["DAWN", "RFR", "MARO"]),
        (
            "ThunderCore 네트워크 종료에 따른 썬더코어(TT) 출금 중단(종료) 및 거래지원 종료 예정일 변경 안내 (09/01 09:00 ~)",
            "ignore",
            [],
        ),
        ("디지털 자산 거래에 관한 유의사항 안내", "ignore", []),
        # critic's review 2026-10-18: KRW listings the first classifier missed (real titles, Upbit API)
        ("KRW, BTC 마켓 디지털 자산 추가 (SOL, MATIC, NU)", "listing", ["SOL", "MATIC", "NU"]),
        ("KRW, BTC 마켓 디지털 자산 추가 (AVAX, IMX) (거래지원 개시 시점 연장 안내)", "listing", ["AVAX", "IMX"]),
        ("KRW, BTC, USDT 마켓 디지털 자산 추가 (MNT, POKT) (POKT 거래지원 개시 시점 연기 안내)", "listing", ["MNT", "POKT"]),
        ("원화 마켓 디지털 자산 추가 (도지코인 DOGE) (완료)", "listing", ["DOGE"]),
        ("원화 마켓 디지털 자산 추가 (AXS, STX, FLOW, DAWN)", "listing", ["AXS", "STX", "FLOW", "DAWN"]),
        ("원화 마켓 디지털 자산 추가 (휴먼스케이프 HUM)", "listing", ["HUM"]),
        ("원화마켓 신규 상장 (디카르고 DKA)", "listing", ["DKA"]),
        ("원화마켓, BTC 마켓 신규 상장 (카바 KAVA)", "listing", ["KAVA"]),
        ("원화 마켓 신규 거래 지원 (체인링크 LINK, 테조스 XTZ)", "listing", ["LINK", "XTZ"]),
        (
            "KRW, BTC 마켓 디지털 자산 추가 (T) 및 이에 따른 NU 유의 종목 지정 안내 (NU <> T 스왑 사이트 추가 안내)",
            "listing",
            ["T"],
        ),
        ("BTC 마켓 디지털 자산 추가 (바운스토큰 AUCTION, 던프로토콜 DAWN)", "ignore", []),  # BTC market only
        ("인피닛(IN) BTC, USDT 마켓, 비쓰리(B3) USDT 마켓 신규 거래지원 안내", "ignore", []),
        ("테더(USDT) KRW, BTC 마켓 추가 안내 (출금 개시 안내)", "ignore", []),
        ("질리카(ZIL) BTC 마켓 거래 지원 일시 중단 (ZIL 원화 마켓 이용 권장) - 완료", "ignore", []),
        # spacing variant "거래 지원 종료", leading bare tickers, batch notices, update notes appended to a title
        ("하이드로(HYDRO) 거래 지원 종료 안내 (5/29 18:00)", "delist", ["HYDRO"]),
        ("AMP, RVR 사업 종료에 따른 거래지원 종료 및 바이백 진행 안내", "delist", ["AMP", "RVR"]),
        ("시프트(SHIFT) BTC 마켓 거래 지원 종료 안내 (10/12)", "ignore", []),
        ("엔진코인(ENJ) KRW 마켓 거래지원 종료(10/10 18:00) 및 BTC 마켓 유의 종목 지정 해제 안내", "ignore", []),
        ("썬더코어(TT) 거래지원 종료 안내 (거래지원 종료 일정 변경 안내)", "delist", ["TT"]),
        ("EMC2 디지털 자산 거래지원 종료 사유 및 거래지원 종료 일시 정정", "ignore", []),
        ("디지털 자산 거래 지원 종료 안내 (5종) - GRS, IOTX, KMD 유의 종목 해제", "delist_batch", []),
        ("24종 디지털 자산 거래지원 종료, 베이직(BASIC) 유의종목 지정 유지 안내 (iOS 관련 추가 안내)", "delist_batch", []),
        ("LUNA 유의 종목 지정 안내", "warning", ["LUNA"]),
        ("디지털 자산 유의 종목 지정 안내 (8종)", "warning_batch", []),
        ("TT, LSK 유의 종목 해제 안내 (전체 유의 종목 해제)", "ignore", []),
    ],
)
def test_classify_upbit(title: str, kind: str, ticks: list[str]) -> None:
    assert classify_upbit(title) == (kind, ticks)


def test_upbit_batch_body() -> None:
    # shape of the real notice 1765 (2021-02-04): three delisted in a numbered list, three released in the text
    body = (
        "기프토(GTO), 오에스티(OST), 비트쉐어(BTS) 마켓의 거래 지원 종료를 안내드립니다.\n"
        "퓨전(FSN), 엔케이엔(NKN), 고체인(GO)는 ... 유의 종목을 해제하며\n"
        "**[디지털 자산 별 마켓 거래 지원 종료 안내]**\n"
        "1) 기프토(GTO)\n- 거래 지원 종료 예정일: 2021.02.09 11:00 (KST)\n"
        "**2) 오에스티(OST)**\n- 거래 지원 종료 예정일: 2021.02.09 11:00 (KST)\n"
        "3) 비트쉐어(BTS)\n---\n[Market] Termination ...\n1) Gifto(GTO)\n"
    )
    assert upbit_body_tickers(body) == ["GTO", "OST", "BTS"]
    kept = "코모도(KMD) 외 23종 ... 종료를 안내드립니다.\n베이직(BASIC)은 유의종목 지정이 연장됩니다.\n1) 코모도(KMD)\n2) 애드엑스(ADX)\n3) 베이직(BASIC)\n"
    assert upbit_body_tickers(kept) == ["KMD", "ADX"]  # notice 2022 (2021-06-18): BASIC stays on the warning list
    plain = "- 비트코인골드(BTG), 엔엑스티(NXT), Cred(LBA)\n- 마켓: BTC 마켓(BTC), 원화 마켓(KRW) 11:00 (KST)"
    assert upbit_body_tickers(plain) == ["BTG", "NXT", "LBA"]  # no numbered list: every name(TICK), markets dropped
    reminder = "지난 6월 18일 공지사항을 통해 ... 안내드린 바 있습니다. 이에 대한 일정을 다시 한 번 안내드리오니"
    assert upbit_body_is_reminder(reminder) and not upbit_body_is_reminder(body)
    assert upbit_batch_size("디지털 자산 유의 종목 지정 안내 (8종)") == 8
    assert upbit_batch_size("24종 디지털 자산 거래지원 종료 안내") == 24
    assert upbit_batch_size("질리카(ZIL) 거래 유의 종목 지정 안내") is None


def test_btc_cross_ratio() -> None:
    # token at 0.5 USDT, BTC 100 000 USDT on the perp; on Upbit 750 KRW and BTC 150 000 000 KRW (premium cancels)
    assert btc_cross_ratio(0.5, 100_000.0, 750.0, 150_000_000.0) == pytest.approx(1.0)
    assert btc_cross_ratio(0.5, 100_000.0, 7500.0, 150_000_000.0) == pytest.approx(0.1)  # a different token
    assert btc_cross_ratio(None, 100_000.0, 750.0, 150_000_000.0) is None  # type: ignore[arg-type]
    assert btc_cross_ratio(0.5, 100_000.0, 0.0, 150_000_000.0) is None


def test_upbit_time_prefers_first_listed_and_converts_kst() -> None:
    utc = int(datetime(2026, 10, 16, 7, 30, 4, tzinfo=UTC).timestamp() * 1000)
    assert upbit_time_ms({"listed_at": "2026-10-16T16:30:04+09:00"}) == utc
    # an edited notice: the first publication is earlier than listed_at
    n = {"first_listed_at": "2026-10-09T17:06:46+09:00", "listed_at": "2026-10-09T18:23:09+09:00"}
    assert upbit_time_ms(n) == int(datetime(2026, 10, 9, 8, 6, 46, tzinfo=UTC).timestamp() * 1000)
    assert upbit_time_ms({"listed_at": "2026-10-16T16:30:04"}) == utc  # naive -> KST
    assert upbit_time_ms({"listed_at": "garbage"}) is None and upbit_time_ms({}) is None


def test_flat_share_and_no_lookahead() -> None:
    rows = ramp(120)
    for i in range(60, 90):  # 30 flat bars among the 60 closed in (t - 1 h, t]
        r = rows[i]
        rows[i] = [r[0], r[4], r[4], r[4], r[4]]
    b = Bars.from_rows(rows)
    t = int(rows[119][0]) + MIN_MS
    assert flat_share(b, t) == pytest.approx(30 / 60)
    t_mid = int(rows[89][0]) + MIN_MS
    rows2 = [*rows[:90], *[[r[0], r[1], r[2] * 3, r[3], r[4]] for r in rows[90:]]]
    assert flat_share(b, t_mid) == flat_share(Bars.from_rows(rows2), t_mid)
    assert math.isnan(flat_share(b, int(rows[0][0])))


def test_second_bars_and_vwap_fill() -> None:
    a = T0
    trades = [(a + 200, 10.0, 1.0), (a + 900, 12.0, 3.0), (a + 1500, 11.0, 2.0), (a + 5000, 20.0, 1.0), (a - 1, 99.0, 1.0)]
    rows = second_bars(trades, a, a + 10_000)
    assert [r[0] for r in rows] == [a, a + 1000, a + 5000]  # the trade before a is dropped
    assert rows[0][1:6] == [10.0, 12.0, 10.0, 12.0, 4.0] and rows[0][6] == pytest.approx(10 + 36)
    assert vwap_fill(rows, a, a + 2000) == pytest.approx((10 + 36 + 22) / 6)
    assert vwap_fill(rows, a + 2000, a + 4000) == 20.0  # empty window: open of the first bar within the fallback
    assert vwap_fill(rows, a + 2000, a + 4000, fallback_ms=500) is None
    # bars after the fill window (beyond the fallback) do not change the fill
    rows2 = [*rows[:2], [a + 5000, 50.0, 50.0, 50.0, 50.0, 1.0, 50.0]]
    assert vwap_fill(rows, a, a + 2000) == vwap_fill(rows2, a, a + 2000)
    # 1 s bars feed the same price functions: the last trade before t
    b = Bars.from_rows(rows, step=1000)
    assert price_at(b, a + 2000, max_stale_ms=5 * MIN_MS) == 11.0
    assert reaction(b, a + 1000, 5000, max_stale_ms=5 * MIN_MS) == pytest.approx(20.0 / 12.0 - 1)


def test_side_aware_second_bars_and_taker_fills() -> None:
    a = T0
    # (time, price, qty, is_buyer_maker): 1 = the aggressor sold (hit the bid), 0 = bought (lifted the ask)
    trades = [
        (a + 100, 10.0, 1.0, 0),
        (a + 300, 9.9, 2.0, 1),
        (a + 600, 10.2, 3.0, 0),
        (a + 1200, 10.1, 1.0, 1),
        (a + 5000, 11.0, 1.0, 0),
        (a + 5100, 10.8, 4.0, 1),
    ]
    rows = second_bars(trades, a, a + 10_000)
    assert [r[0] for r in rows] == [a, a + 1000, a + 5000]
    r0 = rows[0]
    assert r0[1:5] == [10.0, 10.2, 9.9, 10.2] and r0[5] == pytest.approx(6.0)  # same OHLCV as before
    assert r0[7] == pytest.approx(4.0) and r0[8] == pytest.approx(10.0 + 30.6)  # aggressive buys
    assert r0[9] == 10.0 and r0[10] == 9.9 and r0[11] == pytest.approx(2.0) and r0[12] == pytest.approx(19.8)
    assert rows[1][9] is None and rows[1][10] == 10.1  # a second with sells only
    assert second_bars([(a, 1.0, 1.0)], a, a + 1000)[0] == [float(a), 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]  # no side: 7 columns
    # entry: VWAP of aggressive buys only (the sells at 9.9 / 10.1 do not lower it), or the first aggressive buy
    assert taker_buy_fill(rows, a, a + 2000) == pytest.approx((10.0 + 30.6) / 4.0)
    assert taker_buy_fill(rows, a, a + 2000, first=True) == 10.0
    assert taker_buy_fill(rows, a + 1000, a + 2000) == 11.0  # no buys in the window: first buy within the fallback
    assert taker_buy_fill(rows, a + 1000, a + 2000, fallback_ms=1000) is None
    # exit: the last aggressive sell in a bar closed by t
    assert taker_sell_exit(rows, a + 2000) == 10.1
    assert taker_sell_exit(rows, a + 1000) == 9.9
    assert taker_sell_exit(rows, a + 6000) == 10.8
    assert taker_sell_exit(rows, a + 5500) == 10.1  # the bar at a + 5000 has not closed yet
    assert taker_sell_exit(rows, a + 6000 + 5 * MIN_MS) == 10.8
    assert taker_sell_exit(rows, a + 6000 + 5 * MIN_MS + 1) is None  # stale
    # no lookahead: rows after t do not change the fills before t
    later = [*rows, [float(a + 9000), 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]]
    assert taker_buy_fill(later, a, a + 2000) == taker_buy_fill(rows, a, a + 2000)
    assert taker_sell_exit(later, a + 6000) == taker_sell_exit(rows, a + 6000)


def test_exit_price_override() -> None:
    b = Bars.from_rows(ramp(200, step=0.1))
    ent, ex = T0 + 10 * MIN_MS, T0 + 40 * MIN_MS
    tr = short_trade(b, ent, ex, cost_side=0.0, sign=-1.0, entry_price=100.0, exit_price=99.0)
    assert tr is not None and tr.gross == pytest.approx(-0.01)
    tr2 = short_trade(b, ent, ex, cost_side=0.0, sign=-1.0, entry_price=100.0)
    assert tr2 is not None and tr2.gross == pytest.approx(price_at(b, ex) / 100.0 - 1.0)


def test_entry_price_override_long() -> None:
    b = Bars.from_rows(ramp(200, step=0.1))  # rising
    ent, ex = T0 + 10 * MIN_MS, T0 + 40 * MIN_MS
    tr = short_trade(b, ent, ex, cost_side=0.001, sign=-1.0, entry_price=100.0)
    p1 = price_at(b, ex)
    assert tr is not None and tr.gross == pytest.approx(p1 / 100.0 - 1.0) and tr.net == pytest.approx(tr.gross - 0.002)
    seg = b.lo[np.searchsorted(b.t, ent) : np.searchsorted(b.t, ex - MIN_MS, side="right")]  # bars from entry to exit
    assert tr.mae == pytest.approx(1.0 - float(seg.min()) / 100.0)  # long: adverse = fall below the entry fill


def test_placebo_minutes_within_bounds() -> None:
    ts = placebo_times("H1:X@1", T0, 20, grid_ms=MIN_MS, lo_ms=23 * HOUR_MS, hi_ms=2 * HOUR_MS)
    assert len(ts) == 20 and all(T0 - 23 * HOUR_MS <= t < T0 - 2 * HOUR_MS and t % MIN_MS == 0 for t in ts)
    assert ts == placebo_times("H1:X@1", T0, 20, grid_ms=MIN_MS, lo_ms=23 * HOUR_MS, hi_ms=2 * HOUR_MS)


def test_one_sided_p_and_holm() -> None:
    assert one_sided_p([0.1, 0.2, 0.3, 0.15], ["a", "b", "c", "d"], n_boot=999) == pytest.approx(1 / 1000)
    assert one_sided_p([-0.1, -0.2, -0.3], ["a", "b", "c"], n_boot=999) == pytest.approx(1.0)
    assert math.isnan(one_sided_p([0.1], ["a"]))
    adj = holm({"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.5, "e": float("nan")})
    assert adj["a"] == pytest.approx(0.04)  # 4 x 0.01 (NaN does not count)
    assert adj["c"] == pytest.approx(0.09)  # 3 x 0.03
    assert adj["b"] == pytest.approx(0.09)  # 2 x 0.04 = 0.08, monotone -> 0.09
    assert adj["d"] == pytest.approx(0.5) and math.isnan(adj["e"])
