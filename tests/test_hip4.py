import json
from pathlib import Path

import pytest

from tradedesk.analytics.hip4 import (
    book_summary,
    decode_coin,
    encoding,
    outcome_asset_id,
    outcome_coin,
    parse_description,
    parse_stamp,
    priced_in_window,
    recurring_series,
    series_inventory,
)

FIX = json.loads((Path(__file__).parent / "fixtures" / "hip4_outcomes_2026-11-26.json").read_text())
H = 3_600_000


def test_encoding_matches_official_example():
    # asset-ID page: outcome 1, side 0 -> encoding 10 -> "#10", asset 100000010
    assert encoding(1, 0) == 10
    assert outcome_coin(1, 0) == "#10"
    assert outcome_asset_id(1, 0) == 100_000_010
    assert outcome_coin(47948, 1) == "#479481"
    with pytest.raises(ValueError):
        encoding(1, 2)


def test_decode_coin_roundtrip_and_rejects():
    assert decode_coin("#479480") == (47948, 0)
    assert decode_coin(outcome_coin(123, 1)) == (123, 1)
    assert decode_coin("#479482") is None  # side 2 is not valid
    assert decode_coin("BTC") is None
    assert decode_coin("#") is None


def test_parse_description_first_colon_only():
    d = parse_description("perp:xyz:ANT|seconds:180|threshold:83.55|other")
    assert d == {"perp": "xyz:ANT", "seconds": "180", "threshold": "83.55", "other": ""}
    assert parse_description(None) == {}


def test_parse_stamp_utc():
    assert parse_stamp("20261127-0600") == 1_795_759_200_000
    assert parse_stamp("2026-11-27") is None


def test_recurring_series_on_live_sample():
    rec = recurring_series(FIX["outcomeMeta"])
    by = {r["underlying"]: r for r in rec}
    assert set(by) == {"BTC", "ETH", "SOL", "HYPE"}
    btc = by["BTC"]
    assert btc["outcome"] == 47948 and btc["cls"] == "priceBinary" and btc["period"] == "1d"
    assert btc["target"] == pytest.approx(60789)
    assert btc["expiry_ms"] == parse_stamp("20261127-0600")
    assert (btc["coin_yes"], btc["coin_no"]) == ("#479480", "#479481")
    # deployer template outcomes are not recurring series
    assert all(r["outcome"] not in (46302, 41462, 45941) for r in rec)


def test_series_inventory_keys():
    inv = series_inventory(FIX["outcomeMeta"])
    assert inv["recurring:priceBinary:BTC:1d"] == 1
    assert inv["fox:template:priceTouch"] == 1
    assert sum(inv.values()) == len(FIX["outcomeMeta"]["outcomes"])


def test_book_summary_analytic():
    book = {
        "time": 1,
        "levels": [
            [{"px": "0.40", "sz": "100", "n": 1}, {"px": "0.30", "sz": "50", "n": 1}],
            [{"px": "0.42", "sz": "10", "n": 1}, {"px": "0.50", "sz": "20", "n": 1}],
        ],
    }
    s = book_summary(book, band=0.05)
    assert s["mid"] == pytest.approx(0.41)
    assert s["spread"] == pytest.approx(0.02)
    assert s["depth_bid_usd"] == pytest.approx(40 + 15)
    assert s["depth_ask_usd"] == pytest.approx(4.2 + 10)
    assert s["bid_band_usd"] == pytest.approx(40)  # 0.30 < 0.41 - 0.05
    assert s["ask_band_usd"] == pytest.approx(4.2)  # 0.50 > 0.41 + 0.05
    assert s["depth_bid_contracts"] == pytest.approx(150)


def test_book_summary_empty_and_one_sided():
    assert book_summary({"levels": [[], []]})["mid"] is None
    s = book_summary({"levels": [[{"px": "0.4", "sz": "1"}], []]})
    assert s["best_bid"] == 0.4 and s["mid"] is None and s["bid_band_usd"] is None
    assert book_summary(None)["n_bid_levels"] == 0


def test_book_summary_live_sample():
    s = book_summary(FIX["l2Book_479480"])
    assert s["n_bid_levels"] == 20 and s["n_ask_levels"] == 20
    assert s["best_bid"] == pytest.approx(0.32195)
    assert s["best_ask"] == pytest.approx(0.33796)
    assert 0 < s["spread"] < 0.05


def test_priced_in_window_bounds_and_no_lookahead():
    e = parse_stamp("20261127-0600")
    assert priced_in_window([e - 6 * H], e) and priced_in_window([e - 1 * H], e)
    assert not priced_in_window([e - 6 * H - 1, e - 1 * H + 1], e)
    # observations after the window end (expiry - 1 h) cannot turn an unpriced day into a priced one
    base: list[int] = []
    assert priced_in_window(base, e) is False
    late = [e - 1 * H + 1, e - 30 * 60_000, e, e + H]
    assert priced_in_window(base + late, e) is False
    # and an in-window observation keeps the day priced whatever comes after it
    assert priced_in_window([e - 3 * H] + late, e) is True
