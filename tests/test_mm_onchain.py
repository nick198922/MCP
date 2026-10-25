import math

import pytest

from tradedesk.analytics.mm_onchain import (
    Bar,
    Transfer,
    daily_token_flows,
    find_events,
    flow_event,
    is_exchange_address,
    mde,
    mm_firm,
    next_page,
    pick_contracts,
    pick_contracts_from_counts,
    shift,
    short_return,
    utc_day,
)


def test_mm_firm_anchored_and_not_deposit():
    assert mm_firm("Wintermute 1") == "Wintermute"
    assert mm_firm("DWFLabs: 0x53c...3Fa") == "DWF Labs"
    assert mm_firm("Jump Trading") == "Jump Trading"
    assert mm_firm("Fringe Finance : Jump Rate Model V3") is None
    assert mm_firm("Bitget Dep: 0x2aDdD08b5868FBc0D105183b2c247e2FD2dEEB9B") is None  # hex contains b2c2
    assert mm_firm("Wintermute: XCN/Onyx Deposit") is None  # deposit addresses are destinations
    assert mm_firm("Alameda Research 1") is None  # not in the registered list
    assert mm_firm("") is None


def test_exchange_address():
    assert is_exchange_address("Binance 14")
    assert is_exchange_address("Binance")
    assert is_exchange_address("Gate.io 5", "gate-io")
    assert is_exchange_address("Crypto.com 2", "crypto-com")
    assert is_exchange_address("Coinbase Cold 14")
    assert is_exchange_address("Kraken: Hot Wallet 4")
    assert is_exchange_address("Coinbase: LINK Hot 12")
    assert is_exchange_address("Bitget Dep: 0xC7d3C88077d07B9EdE6aCd2060e5DACBCf08e668", "bitget")
    assert is_exchange_address("Binance: Deposit 1")
    assert is_exchange_address("Galaxy Digital: HTX Deposit")
    assert is_exchange_address("Jump Trading: Binance Deposit")
    assert is_exchange_address("", "binance-deposit")
    assert not is_exchange_address("Uniswap V3: Router", "dex")


@pytest.mark.parametrize(
    ("tag", "label"),
    [
        ("Bybit Exploiter 33", "bybit-exploit"),
        ("Bybit Exploiter 33", ""),  # tag shape alone rejects it
        ("Binance Charity: 2018-19 L-Istrina Campaign 3", "binance-charity"),
        ("Binance Charity", "binance charity donate"),
        ("BitDAO: Bybit Locked 5", "bitdao"),
        ("Bitget Wallet: MultiSwap", "bitget"),
        ("Tether: USDT Stablecoin", "bitfinex"),
        ("Binance: BNB Token", "binance blocked"),
        ("Binance USD", "binance"),
        ("Coinbase: AVS Operator", "avs-operator"),
        ("HTX: AVS Operator", ""),
        ("Coinbase: Deposit Funder 9", "coinbase"),
        ("Kraken: Deployer 1", "kraken"),
        ("Binance Pool", "binance mining"),
        ("Binance Dep: 0x0Ab18527970d8f3c5Faf22DFE96f4970cc8", "blocked"),
        ("OKX: DEX Router 4", "dex"),
        ("", "gate-io"),  # a category without a tag is not enough
        ("", "binance"),
    ],
)
def test_exchange_address_rejects_non_exchange_tags(tag, label):
    assert not is_exchange_address(tag, label)


def _rows(*blocks):
    return [{"blockNumber": str(b)} for b in blocks]


def test_next_page_full_page_drops_and_refetches_last_block():
    rows = _rows(100, 101, 101, 105, 105)  # page size 5: block 105 may be cut in half
    st = next_page(rows, start=100, b1=200, page=5)
    assert [r["blockNumber"] for r in st.kept] == ["100", "101", "101"]
    assert st.next_start == 105 and not st.done and not st.truncated


def test_next_page_short_page_closes_wallet():
    rows = _rows(150, 160)
    st = next_page(rows, start=140, b1=200, page=5)
    assert len(st.kept) == 2 and st.done and st.next_start == 201 and not st.truncated
    empty = next_page([], start=140, b1=200, page=5)
    assert empty.kept == [] and empty.done


def test_next_page_single_block_full_page_is_truncated():
    rows = _rows(*([120] * 5))
    st = next_page(rows, start=120, b1=200, page=5)
    assert len(st.kept) == 5 and st.truncated and st.next_start == 121 and not st.done
    last = next_page(_rows(*([200] * 5)), start=200, b1=200, page=5)
    assert last.truncated and last.done


def test_next_page_chain_counts_each_row_once():
    """Walking a synthetic ledger page by page yields every row exactly once."""
    ledger = _rows(1, 1, 2, 3, 3, 3, 4, 7, 7, 8, 9, 9)
    start, seen = 1, []
    for _ in range(20):
        page = [r for r in ledger if int(r["blockNumber"]) >= start][:4]
        st = next_page(page, start=start, b1=9, page=4)
        seen += st.kept
        if st.done:
            break
        start = st.next_start
    assert [r["blockNumber"] for r in seen] == [r["blockNumber"] for r in ledger]


def test_pick_contracts_from_counts_uses_all_destinations():
    keep, dropped = pick_contracts_from_counts({("ABC", "0xa"): 3, ("ABC", "0xb"): 10, ("XYZ", "0xc"): 1})
    assert keep == {"ABC": "0xb", "XYZ": "0xc"} and dropped == {"ABC": ["0xa"]}


def test_utc_day_and_shift():
    assert utc_day(0) == "1970-01-01"
    assert utc_day(1704067200 + 86399) == "2024-01-01"
    assert utc_day(1704067200 + 86400) == "2024-01-02"
    assert shift("2024-03-01", -1) == "2024-02-29"


def _t(ts, sym, amt, contract="0xc"):
    return Transfer(ts, "0xmm", "0xex", contract, sym, amt)


def test_pick_contracts_and_flows():
    tr = [_t(0, "ABC", 1.0, "0xa"), _t(10, "ABC", 2.0, "0xa"), _t(20, "ABC", 50.0, "0xfake"), _t(30, "USDC", 9.0)]
    keep, dropped = pick_contracts(tr)
    assert keep["ABC"] == "0xa" and dropped["ABC"] == ["0xfake"]
    flows = daily_token_flows(tr, keep)
    assert flows == {("ABC", "1970-01-01"): 3.0}  # fake contract and stablecoin removed


def _bars(n_days=60, close=2.0, qv=1_000_000.0, start="2024-01-01"):
    return {shift(start, k): Bar(close, close, qv) for k in range(n_days)}


def test_flow_event_threshold_analytic():
    bars = _bars()
    d = shift("2024-01-01", 40)
    # 20 000 tokens x $2 = $40k = 4 % of $1M ADV
    ev = flow_event(20_000, d, bars, min_share=0.02, min_usd=10_000)
    assert ev is not None and math.isclose(ev.usd, 40_000) and math.isclose(ev.share, 0.04)
    assert flow_event(20_000, d, bars, min_share=0.05, min_usd=10_000) is None
    assert flow_event(20_000, d, bars, min_share=0.02, min_usd=50_000) is None
    # fewer than 30 days of history -> no event
    assert flow_event(1e9, shift("2024-01-01", 20), bars, min_share=0.0, min_usd=0.0) is None


def test_flow_event_ignores_day_d_and_later():
    """Point-in-time: changing bars on day d and after must not change the decision or the numbers."""
    bars = _bars()
    d = shift("2024-01-01", 40)
    a = flow_event(20_000, d, bars, min_share=0.02, min_usd=10_000)
    later = dict(bars)
    for k in range(40, 60):
        later[shift("2024-01-01", k)] = Bar(99.0, 0.01, 1.0)
    b = flow_event(20_000, d, later, min_share=0.02, min_usd=10_000)
    assert a == b


def test_find_events_dedupe_and_period():
    bars = {"ABCUSDT": _bars()}
    flows = {("ABCUSDT", shift("2024-01-01", k)): 20_000.0 for k in (35, 38, 43, 50)}
    evs = find_events(flows, bars, min_share=0.02, min_usd=10_000, dedupe_days=7)
    # 35 kept; 38 within 7 d of 35 -> dropped; 43 = 35 + 8 -> kept; 50 = 43 + 7 -> dropped
    assert [e.day for e in evs] == [shift("2024-01-01", 35), shift("2024-01-01", 43)]
    evs2 = find_events(flows, bars, min_share=0.02, min_usd=10_000, first_day=shift("2024-01-01", 40))
    assert [e.day for e in evs2] == [shift("2024-01-01", 43)]  # 50 is within 7 d of 43


def test_find_events_earlier_unchanged_by_later_flows():
    bars = {"ABCUSDT": _bars()}
    base = {("ABCUSDT", shift("2024-01-01", 35)): 20_000.0}
    more = dict(base)
    more[("ABCUSDT", shift("2024-01-01", 55))] = 20_000.0
    a = find_events(base, bars, min_share=0.02, min_usd=10_000)
    b = find_events(more, bars, min_share=0.02, min_usd=10_000)
    assert b[: len(a)] == a and len(b) == 2


def test_short_return_and_mde():
    bars = {"2024-01-02": Bar(100.0, 101.0, 1.0), "2024-01-04": Bar(95.0, 90.0, 1.0)}
    assert short_return(bars, "2024-01-01", 3) == pytest.approx(0.10)  # entry open 100, exit close 90
    assert short_return(bars, "2024-01-01", 2) is None
    assert mde(0.10, 49) == pytest.approx(0.04)
    assert mde(0.1, 0) == math.inf
