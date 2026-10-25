"""The owner's open Bybit options: analytics.my_options (pure) and scripts/bybit_options_positions.py (fake client)."""

from __future__ import annotations

import json
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tradedesk.analytics.my_options import MAX_AGE_HOURS, my_options_view, parse_bybit_option, position_view, prob_profit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import bybit_options_positions as bop  # noqa: E402

TICKERS = json.loads((ROOT / "tests" / "fixtures" / "bybit_option_tickers_btc_2026-10-26.json").read_text(encoding="utf-8"))
ASOF = datetime.strptime(TICKERS["_fetched_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)  # whole seconds, as the file
ASOF_S = ASOF.strftime("%Y-%m-%dT%H:%M:%SZ")
TICK = {t["symbol"]: t for t in TICKERS["list"]}
CALL, PUT_NEAR, PUT_FAR = "BTC-27NOV26-81000-C-USDT", "BTC-27NOV26-77000-P-USDT", "BTC-27NOV26-70000-P-USDT"


def _pos(symbol: str, side: str, size: str, avg: str, upnl: str | None = None, **kw) -> dict:
    """A position/list row (category=option) with the fields the fetch script keeps; values are strings, as Bybit sends."""
    t = TICK.get(symbol, {})
    return {
        "symbol": symbol, "side": side, "size": size, "avgPrice": avg, "markPrice": t.get("markPrice"),
        "unrealisedPnl": upnl, "positionValue": None, "delta": None, "theta": "-1.0",
    } | kw  # fmt: skip


def _state(positions: list, **kw) -> dict:
    return {
        "schema": 1, "source": "bybit", "asof_utc": ASOF_S, "status": "ok", "detail": "", "positions": positions,
        "tickers": {s: {k: t[k] for k in ("markPrice", "markIv", "underlyingPrice", "indexPrice")} for s, t in TICK.items()},
        "errors": [],
    } | kw  # fmt: skip


def _n(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def test_black76_theta_reproduces_bybit_per_contract_on_the_live_sample():
    # real sample 26.10 01:05 Kyiv: Black-76 (r=0) at markIv on underlyingPrice, T to 08:00 UTC / 365, gives Bybit's theta
    for sym, t in TICK.items():
        v = position_view(_pos(sym, "Buy", "1", t["markPrice"]), t, ASOF)
        assert v["theta_usd_day"] == pytest.approx(float(t["theta"]), rel=5e-3), sym
        assert v["iv"] == float(t["markIv"]) and v["forward"] == float(t["underlyingPrice"])
        assert v["expiry_utc"] == "2026-11-27T08:00:00Z" and 32 < v["days"] < 33


def test_parse_bybit_option_symbols_and_hostile_text():
    base, exp, k, is_call, canon = parse_bybit_option(CALL)
    assert (base, exp, k, is_call, canon) == ("BTC", datetime(2026, 11, 27, 8, tzinfo=UTC), 81000.0, True, CALL)
    assert parse_bybit_option("ETH-3NOV26-2500-P")[4] == "ETH-3NOV26-2500-P"  # the USDC form (4 parts)
    assert parse_bybit_option("SOL-27NOV26-0d5-C")[2] == 0.5
    for bad in (
        "<b>-27NOV26-81000-C", "BTC-27NOV26-81000-C-XYZ", "BTC-27NOV26-8e4-C", "BTC-27NOV26-81000-X", "BTC-32NOV26-1-C",
        "BTC-27NOV26-0-C", "BTC-27NOV26-81000", "B" * 41, None, 5, ["BTC"],
    ):  # fmt: skip
        assert parse_bybit_option(bad) is None, bad


def test_prob_profit_analytic_reference():
    F, T, s = 80000.0, 30 / 365, 0.4
    v = s * math.sqrt(T)
    # at-the-money call bought for nothing: P(F_T > F) = N(-v/2)
    assert prob_profit(F, F, T, s, True, True, 0.0) == pytest.approx(_n(-v / 2), abs=1e-12)
    # a call bought for 2000 needs F_T > 82000: N(d2) at the breakeven
    d2 = (math.log(F / 82000) - v * v / 2) / v
    assert prob_profit(F, F, T, s, True, True, 2000.0) == pytest.approx(_n(d2), abs=1e-12)
    # long put: P(F_T < K - premium); a short leg is the complement of its long twin
    d2p = (math.log(F / 75000) - v * v / 2) / v
    assert prob_profit(F, 77000, T, s, False, True, 2000.0) == pytest.approx(1 - _n(d2p), abs=1e-12)
    assert prob_profit(F, 77000, T, s, False, False, 2000.0) == pytest.approx(_n(d2p), abs=1e-12)
    assert prob_profit(F, 1000, T, s, False, True, 1500.0) == 0.0  # premium above the strike: never pays back
    assert prob_profit(F, 1000, T, s, False, False, 1500.0) == 1.0
    assert prob_profit(F, F, T, s, True, True, 500) > prob_profit(F, F, T, s, True, True, 1500)  # dearer -> less likely
    for args in ((0, F, T, s), (F, F, 0, s), (F, F, T, 0)):
        assert prob_profit(*args, True, True, 100.0) is None
    assert prob_profit(F, F, T, s, True, True, -1.0) is None


def test_my_options_view_happy_path_on_the_live_tickers():
    state = _state(
        [
            _pos(CALL, "Buy", "0.1", "3000", "57.3"),  # long call 0.1 BTC, entry $3 000
            _pos(PUT_FAR, "Sell", "0.2", "900", "44.8"),  # short put 0.2 BTC, entry $900
            _pos(PUT_NEAR, "Buy", "0.05", "2100", None),  # no unrealisedPnl: (mark - entry) x size
        ]
    )
    data, why = my_options_view(state, ASOF + timedelta(minutes=10))
    assert why is None and data["label"] == "risk_neutral" and data["n_positions"] == 3 and data["skipped"] == 0
    assert data["age_hours"] == pytest.approx(10 / 60, abs=0.01) and data["asof_utc"] == ASOF_S
    call, put_far, put_near = (next(p for p in data["positions"] if p["symbol"] == s) for s in (CALL, PUT_FAR, PUT_NEAR))
    assert [p["symbol"] for p in data["positions"]] == [CALL, PUT_FAR, PUT_NEAR]  # by |P&L|: 57.3, 44.8, 8.8
    assert put_near["upnl_usd"] == pytest.approx((1923.75834553 - 2100) * 0.05)
    assert data["upnl_usd"] == pytest.approx(57.3 + 44.8 + (1923.75834553 - 2100) * 0.05)
    # theta: Bybit's per-contract theta x signed size (the long call pays, the short put earns)
    assert call["theta_usd_day"] == pytest.approx(-54.17225779 * 0.1, rel=5e-3)
    assert put_far["theta_usd_day"] == pytest.approx(33.3131568 * 0.2, rel=5e-3)
    assert data["theta_usd_day"] == pytest.approx(sum(p["theta_usd_day"] for p in data["positions"]))
    assert call["theta_exchange"] == -1.0  # Bybit's own figure is kept apart, not used
    # chance of profit at expiry: N(d2) at the breakeven under the leg's own mark IV
    T = (datetime(2026, 11, 27, 8, tzinfo=UTC) - ASOF).total_seconds() / (365 * 86400)
    v = 0.365 * math.sqrt(T)
    assert call["breakeven"] == 84000 and call["pop"] == pytest.approx(_n((math.log(81115.4 / 84000) - v * v / 2) / v), abs=1e-9)
    v = 0.4464 * math.sqrt(T)
    assert put_far["breakeven"] == 69100 and put_far["pop"] == pytest.approx(_n((math.log(81115.4 / 69100) - v * v / 2) / v), abs=1e-9)
    assert 0.2 < call["pop"] < 0.5 and 0.85 < put_far["pop"] < 0.95
    assert (call["side"], put_far["side"], call["kind"], put_far["kind"]) == ("long", "short", "call", "put")


def test_my_options_view_without_usable_data_gives_a_reason():
    now = ASOF + timedelta(minutes=5)
    assert my_options_view(None, now) == (None, "no Bybit positions file yet (timer tradedesk-bybit-options off or no key)")
    assert "no read-only Bybit key" in my_options_view(_state([], status="no_key"), now)[1]
    assert "can trade" in my_options_view(_state([], status="key_not_read_only"), now)[1]
    assert my_options_view(_state([], status="error", detail="user/query-api: AuthenticationError"), now)[1] == (
        "the Bybit request failed (user/query-api: AuthenticationError)"
    )
    assert "unknown status" in my_options_view(_state([], status="<b>"), now)[1]
    late = ASOF + timedelta(hours=MAX_AGE_HOURS, minutes=1)
    assert "seems stopped" in my_options_view(_state([]), late)[1]
    assert my_options_view(_state([]), ASOF + timedelta(hours=MAX_AGE_HOURS))[0] is not None  # at the limit
    assert "asof_utc" in my_options_view(_state([], asof_utc=None), now)[1]
    assert "positions list" in my_options_view(_state("x"), now)[1]


def test_my_options_view_no_positions_is_data_and_junk_is_skipped():
    data, why = my_options_view(_state([]), ASOF)
    assert why is None and data["n_positions"] == 0 and data["positions"] == [] and data["upnl_usd"] is None
    junk = [
        "x", {"symbol": ["BTC"]}, _pos("<script>-27NOV26-1-C", "Buy", "1", "1"), _pos(CALL, "Hold", "1", "1"),
        _pos(CALL, "Buy", "0", "1"), _pos(CALL, "Buy", "nan", "1"), _pos(CALL, "Buy", "1", "x", "inf"),
    ]  # fmt: skip
    data, _ = my_options_view(_state(junk, errors=["position/list SOL: NetworkError", 5]), ASOF)
    assert data["n_positions"] == 1 and data["skipped"] == 6 and data["errors"] == ["position/list SOL: NetworkError"]
    p = data["positions"][0]  # entry unreadable: no P&L, breakeven or chance; theta still from the ticker
    assert p["upnl_usd"] is None and p["pop"] is None and p["breakeven"] is None and p["theta_usd_day"] < 0
    data, _ = my_options_view(_state([_pos(CALL, "Buy", "1", "3000", "5")], tickers={}), ASOF)  # no tickers
    p = data["positions"][0]
    assert p["upnl_usd"] == 5 and p["theta_usd_day"] is None and p["pop"] is None and data["theta_usd_day"] is None


# ----------------------------------------------------------------------------- the fetch script, with a fake client

ALLOWED = {"privateGetV5UserQueryApi", "privateGetV5PositionList", "publicGetV5MarketTickers"}


class FakeBybit:
    """ccxt.bybit stand-in: records every call; only the three read-only GETs exist."""

    def __init__(self, read_only: str = "1", fail: set[str] | None = None, positions: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.read_only, self.fail = read_only, fail or set()
        self.positions = positions if positions is not None else {"BTC": [_pos(CALL, "Buy", "0.1", "3000", "57.3")]}

    def _rec(self, name: str, params: dict | None = None) -> None:
        self.calls.append((name, dict(params or {})))
        key = params.get("baseCoin") or params.get("settleCoin") if params else name
        if name in self.fail or key in self.fail:
            raise RuntimeError("bybit {'retMsg': 'boom', 'X-BAPI-SIGN': 'deadbeef'}")

    def privateGetV5UserQueryApi(self, params=None):  # noqa: N802 - ccxt's name
        self._rec("privateGetV5UserQueryApi", params)
        return {"result": {"readOnly": self.read_only, "note": "n", "ips": ["169.58.169.184"]}}

    def privateGetV5PositionList(self, params=None):  # noqa: N802
        self._rec("privateGetV5PositionList", params)
        coin = params.get("baseCoin") or params.get("settleCoin")
        rows = self.positions.get(coin, []) if coin != "USDT" else [r for v in self.positions.values() for r in v]  # duplicates
        return {"result": {"list": rows, "nextPageCursor": ""}}

    def publicGetV5MarketTickers(self, params=None):  # noqa: N802
        self._rec("publicGetV5MarketTickers", params)
        return {"result": {"list": TICKERS["list"] + [{"symbol": "BTC-27NOV26-99000-C-USDT", "markIv": "0.4"}]}}


def test_collect_happy_path_only_reads():
    ex = FakeBybit(positions={"BTC": [_pos(CALL, "Buy", "0.1", "3000", "57.3"), _pos(PUT_FAR, "Sell", "0", "900", "1")]})
    out = bop.collect(ex)
    assert out["status"] == "ok" and out["errors"] == [] and {n for n, _ in ex.calls} <= ALLOWED
    assert ex.calls[0][0] == "privateGetV5UserQueryApi"  # the key is checked before anything else
    assert [p["symbol"] for p in out["positions"]] == [CALL]  # de-duplicated, size 0 dropped
    assert set(out["positions"][0]) == set(bop.POSITION_FIELDS)
    assert list(out["tickers"]) == [CALL] and set(out["tickers"][CALL]) == set(bop.TICKER_FIELDS)  # only held symbols
    assert ("publicGetV5MarketTickers", {"category": "option", "baseCoin": "BTC"}) in ex.calls
    data, _ = my_options_view(out, datetime.now(UTC))
    assert data["n_positions"] == 1 and data["positions"][0]["theta_usd_day"] < 0


def test_collect_refuses_a_trading_key_and_reports_failures_without_messages():
    ex = FakeBybit(read_only="0")
    assert bop.collect(ex)["status"] == "key_not_read_only" and [n for n, _ in ex.calls] == ["privateGetV5UserQueryApi"]
    out = bop.collect(FakeBybit(fail={"privateGetV5UserQueryApi"}))
    assert out["status"] == "error" and out["detail"] == "user/query-api: RuntimeError" and "deadbeef" not in json.dumps(out)
    out = bop.collect(FakeBybit(fail={"ETH"}))  # one coin failing: the rest still counts, the gap is named
    assert out["status"] == "ok" and out["errors"] == ["position/list ETH: RuntimeError"] and out["positions"]
    out = bop.collect(FakeBybit(fail={"privateGetV5PositionList"}))
    assert out["status"] == "error" and out["positions"] == [] and "deadbeef" not in json.dumps(out)
    out = bop.collect(FakeBybit(fail={"publicGetV5MarketTickers"}))  # P&L still there, theta and chance empty
    assert out["status"] == "ok" and out["tickers"] == {} and out["errors"] == ["market/tickers BTC: RuntimeError"]


def test_main_never_prints_or_stores_the_key(tmp_path, monkeypatch, capsys):
    env = tmp_path / "bybit.env"
    env.write_text("BYBIT_API_KEY=KEY_abc123\nBYBIT_API_SECRET=SECRET_xyz789\n", encoding="utf-8")
    seen: dict = {}

    def fake_bybit(cfg):
        seen.update(cfg)
        return FakeBybit()

    import ccxt

    monkeypatch.setattr(ccxt, "bybit", fake_bybit)
    out = tmp_path / "a" / "my_options.json"
    assert bop.main(["--env", str(env), "--out", str(out), "--print"]) == 0
    assert seen["apiKey"] == "KEY_abc123"  # the key reached the client, and nowhere else
    text, printed = out.read_text(encoding="utf-8"), capsys.readouterr()
    for secret in ("KEY_abc123", "SECRET_xyz789"):
        assert secret not in text and secret not in printed.out and secret not in printed.err
    assert json.loads(text)["status"] == "ok" and '"n_positions": 1' in printed.out


def test_main_without_a_key_writes_a_status_and_exits_cleanly(tmp_path, capsys):
    out = tmp_path / "my_options.json"
    assert bop.main(["--env", str(tmp_path / "none.env"), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["status"] == "no_key" and "status no_key" in capsys.readouterr().out
    (tmp_path / "half.env").write_text("BYBIT_API_KEY=k\n", encoding="utf-8")  # no secret
    assert bop.main(["--env", str(tmp_path / "half.env"), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["status"] == "no_key"


def test_unit_and_timer_run_the_script_only_with_a_key():
    unit = (ROOT / "deploy" / "user-units" / "tradedesk-bybit-options.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy" / "user-units" / "tradedesk-bybit-options.timer").read_text(encoding="utf-8")
    assert "scripts/bybit_options_positions.py" in unit and "ConditionPathExists=/etc/tradedesk-bybit.env" in unit
    assert "OnCalendar=*-*-* *:50:00 Europe/Kyiv" in timer
    assert str(bop.OUT_DEFAULT) == "/var/lib/tradedesk-mcp/assistant/my_options.json"  # <dir of the server's db>/assistant
