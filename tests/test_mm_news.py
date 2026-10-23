"""Market-maker news (IDEA-20261017-market-makers, part C): CSV parsing, classes, the registered pool rules, and the
long / short trade of a class without lookahead."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

from tradedesk.analytics.news_events import (
    HOUR_MS,
    MIN_MS,
    Bars,
    Event,
    MmNews,
    dedupe,
    in_direction,
    mm_candidates,
    mm_exclusion,
    mm_side,
    parse_mm_news,
    reaction,
    short_trade,
    snowflake_ms,
)

MM_CSV = Path(__file__).resolve().parent.parent / "docs" / "verification" / "2026-10-23-news-mm.csv"
T0 = 1_760_000_000_000 // MIN_MS * MIN_MS


def mk(token: str = "X", type_: str = "mm_scandal", prior: str = "down", ts: str = "tweet_id", perps: tuple[str, ...] = ()) -> MmNews:
    return MmNews(token, T0, type_, prior, ts, "", perps, "")


def test_mm_csv_parse_times_and_pool_rules() -> None:
    rows = parse_mm_news(MM_CSV.read_text())
    assert len(rows) == 33
    assert sum(r.time_source == "tweet_id" for r in rows) == 29
    for r in rows:
        if r.time_source == "tweet_id":  # t0 is the tweet's own creation time, to the second
            tid = int(re.search(r"/status/(\d+)", r.url).group(1))  # type: ignore[union-attr]
            assert r.ms == snowflake_ms(tid) // 1000 * 1000, r.key
    reasons = Counter(mm_exclusion(r) for r in rows)
    assert reasons == {None: 20, "time is a date only": 4, "direction prior unclear": 7, "not token-specific": 2}
    pool = [r for r in rows if mm_exclusion(r) is None]
    assert sum(mm_side(r) > 0 for r in pool) == 14 and sum(mm_side(r) < 0 for r in pool) == 6
    # the registered 72 h same-ticker dedupe drops GPS 09.03, OM 14.04, MOVE 01.05 and 02.05
    evs = [Event(mm_candidates(r)[0], r.ms, r.type, "curated", r.url) for r in pool]
    kept, dropped = dedupe(evs)
    assert len(kept) == 16 and sorted(e.token for e in dropped) == ["GPS", "MOVE", "MOVE", "OM"]
    assert len({e.cluster for e in kept}) == 16  # GPS 09.03 (same URL as SHELL) is the dropped one


def test_mm_candidates_and_exclusions() -> None:
    by = {r.key: r for r in parse_mm_news(MM_CSV.read_text())}
    floki = next(r for r in by.values() if r.token == "FLOKI")
    assert mm_candidates(floki) == ["FLOKI"]  # 1000FLOKIUSDT -> FLOKI
    nexf = next(r for r in by.values() if r.token.startswith("NEXF"))
    assert mm_candidates(nexf) == []  # a description in brackets is not a ticker
    assert mm_candidates(mk("MULTI (GOTBIT CLIENTS)")) == []
    assert mm_candidates(mk("BTC/ETH/SOL")) == ["BTC"]
    assert mm_candidates(mk("EOS")) == ["EOS"]
    assert mm_candidates(mk("LUNC", perps=("binance:1000LUNCUSDT",))) == ["LUNC"]
    assert mm_exclusion(mk("X", "mm_partnership", "up")) is None
    assert mm_exclusion(mk("X", "mm_partnership", "down")) == "prior contradicts class"
    assert mm_exclusion(mk("X", "mm_offboard", "up")) == "prior contradicts class"
    assert mm_exclusion(mk("X", "other", "down")) == "unknown type"
    assert mm_exclusion(mk("ETH")) == "not token-specific"
    assert mm_exclusion(mk("X", ts="article_date")) == "time is a date only"
    assert mm_side(mk("X", "mm_offboard")) == 1.0 and mm_side(mk("X", "mm_partnership", "up")) == -1.0
    assert in_direction(-0.1, 1.0) == pytest.approx(0.1) and in_direction(-0.1, -1.0) == pytest.approx(-0.1)


def ramp(n: int, p0: float = 100.0, step: float = 0.1, start: int = T0 - 60 * MIN_MS) -> list[list[float]]:
    return [[start + i * MIN_MS, p0 + step * i, p0 + step * i + 0.5, p0 + step * i - 0.5, p0 + step * i] for i in range(n)]


def test_class_trade_closed_form_and_no_lookahead() -> None:
    rows = ramp(1700)
    b1 = Bars.from_rows(rows)
    ent, ex = T0 + 15 * MIN_MS, T0 + 15 * MIN_MS + 24 * HOUR_MS
    p_in, p_out = 100 + 0.1 * (59 + 15), 100 + 0.1 * (59 + 15 + 1440)
    side = mm_side(mk("X", "mm_partnership", "up"))
    fund = [(ent + HOUR_MS, 0.001)]  # positive rate: longs pay
    tr = short_trade(b1, ent, ex, cost_side=0.00205, funding=fund, sign=side)
    assert tr is not None
    assert tr.gross == pytest.approx(p_out / p_in - 1) and tr.funding == pytest.approx(-0.001)
    assert tr.net == pytest.approx(p_out / p_in - 1 - 0.001 - 0.0041)
    assert in_direction(reaction(b1, T0, 24 * HOUR_MS), side) == pytest.approx(reaction(b1, T0, 24 * HOUR_MS))  # type: ignore[arg-type]
    # bars closing after the exit do not change the trade or the reactions before it
    rows2 = [r if r[0] + MIN_MS <= ex else [r[0], r[1], r[2] * 4, r[3] / 4, r[4] * 3] for r in rows]
    b2 = Bars.from_rows(rows2)
    assert short_trade(b2, ent, ex, cost_side=0.00205, funding=fund, sign=side, stop=0.25) == short_trade(
        b1, ent, ex, cost_side=0.00205, funding=fund, sign=side, stop=0.25
    )
    assert reaction(b1, T0, 4 * HOUR_MS) == reaction(b2, T0, 4 * HOUR_MS)


def test_measure_sign_sets_the_trade_direction(tmp_path: Path) -> None:
    """``verify_news_events.measure(..., sign=-1)`` trades long; the default for a non-listing category is short."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import verify_news_events as vne

    day = 24 * HOUR_MS
    d0 = T0 // day * day
    start = d0 - day
    for k in range(7):  # 1m bars of a steadily rising perp, one cache file per UTC day (d0 - 1 ... d0 + 5)
        rows = [[start + i * MIN_MS, *(4 * [100.0 + 0.01 * i]), 1.0] for i in range(k * 1440, (k + 1) * 1440)]
        p = tmp_path / "px" / "binance" / "XUSDT" / "1m" / f"{vne.ymd(start + k * day)}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rows))
    e = Event("X", T0, "mm_partnership", "curated", "c1")
    r = ["binance", "XUSDT", 1.0]
    ent = T0 + 15 * MIN_MS
    i_in = (ent - MIN_MS - start) // MIN_MS  # close of the bar that closed at the entry
    i_out = (ent + 24 * HOUR_MS - MIN_MS - start) // MIN_MS
    p_in, p_out = 100.0 + 0.01 * i_in, 100.0 + 0.01 * i_out
    long_ = vne.measure(tmp_path, e, r, sign=-1.0)
    short = vne.measure(tmp_path, e, r)
    assert long_ is not None and short is not None
    tl, ts = long_["trades"]["d15_h24_base"], short["trades"]["d15_h24_base"]
    assert tl["gross"] == pytest.approx(p_out / p_in - 1) and tl["gross"] > 0
    assert ts["gross"] == pytest.approx(1 - p_out / p_in) and ts["gross"] < 0
    assert tl["net"] == pytest.approx(tl["gross"] - 2 * vne.COST)
    assert long_["react"] == short["react"]  # reactions are raw price changes, independent of the direction
