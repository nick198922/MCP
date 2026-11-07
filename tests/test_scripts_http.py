"""Scripts that read public HL / Bybit data through core.http.Http (item 235, BACKLOG 6.45): httpx.MockTransport, no network.

scripts/research/vault_profile.py (`_post`, `run_sync`), scripts/copy_vaults.py (`_get`) and scripts/hyperliquid_import.py
(`fetch_fills`): same results as with their old urlopen / httpx.Client code, failures raised the way callers expect.
"""

import json
import sys
from pathlib import Path

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "research"))

import copy_vaults as cv  # noqa: E402
import hyperliquid_import as hl  # noqa: E402
import vault_profile as vp  # noqa: E402

from tradedesk.core.errors import ProviderError  # noqa: E402
from tradedesk.core.http import Http  # noqa: E402


def mock_http(handler, base="https://api.hyperliquid.xyz", retries=2):
    return Http("t", base, rate=1000.0, retries=retries, backoff_base=0.0, transport=httpx.MockTransport(handler))


# --- research/vault_profile._post -------------------------------------------------------------------------------


def test_vault_profile_post_sends_the_body_and_survives_repeated_calls():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"marginSummary": {"accountValue": "1"}})

    h = mock_http(handler)
    body = {"type": "clearinghouseState", "user": "0xabc"}
    assert vp._post(body, h) == {"marginSummary": {"accountValue": "1"}}
    assert vp._post(body, h) == {"marginSummary": {"accountValue": "1"}}  # a new event loop per call, client re-created
    assert seen == [("POST", "/info", body)] * 2


def test_vault_profile_post_retries_5xx_then_raises_runtime_error():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(RuntimeError, match="Hyperliquid info vaultDetails failed") as e:
        vp._post({"type": "vaultDetails", "vaultAddress": "0x1"}, mock_http(handler, retries=3))
    assert len(calls) == 4 and isinstance(e.value.__cause__, ProviderError)


def test_vault_profile_post_does_not_retry_a_client_error():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(422, text="Failed to deserialize")

    with pytest.raises(RuntimeError, match="candleSnapshot"):
        vp._post({"type": "candleSnapshot", "req": {}}, mock_http(handler, retries=3))
    assert len(calls) == 1


def test_load_candles_skips_a_coin_whose_request_fails(tmp_path, monkeypatch):
    def handler(req):
        coin = json.loads(req.content)["req"]["coin"]
        if coin == "BAD":
            return httpx.Response(500)
        return httpx.Response(200, json=[{"t": 0, "o": "1", "h": "2", "l": "0.5", "c": "1.5"}])

    monkeypatch.setattr(vp, "HL_INFO", mock_http(handler, retries=0))
    out = vp.load_candles({"BTC", "BAD"}, 0, 0, tmp_path)
    assert out == {"BTC": {0: (1.0, 2.0, 0.5, 1.5)}}


# --- copy_vaults._get --------------------------------------------------------------------------------------------


def test_copy_vaults_get_returns_result_and_feeds_quotes():
    seen = []

    def handler(req):
        seen.append((req.url.path, dict(req.url.params)))
        rows = [
            {"symbol": "BTCUSDT", "bid1Price": "99", "ask1Price": "101"},
            {"symbol": "DEADUSDT", "bid1Price": "", "ask1Price": "1"},
        ]
        return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {"list": rows}})

    h = mock_http(handler, base="https://api.bybit.com")
    quotes = cv.bybit_quotes(lambda path, **p: cv._get(path, http=h, **p))
    assert quotes == {"BTCUSDT": (99.0, 101.0)}
    assert seen == [("/v5/market/tickers", {"category": "linear"})]


def test_copy_vaults_get_raises_on_ret_code_and_on_transport_failure():
    def bad_code(req):
        return httpx.Response(200, json={"retCode": 10001, "retMsg": "params error", "result": {}})

    with pytest.raises(RuntimeError, match="Bybit funding/history: params error"):
        cv._get("funding/history", http=mock_http(bad_code, base="https://api.bybit.com"), symbol="X")

    def down(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderError) as e:
        cv._get("tickers", http=mock_http(down, base="https://api.bybit.com", retries=1), category="linear")
    assert e.value.reason == "network"


def test_copy_vaults_run_skips_on_a_failed_call(tmp_path, monkeypatch, capsys):
    def down(req):
        return httpx.Response(503)

    monkeypatch.setattr(cv, "BYBIT", mock_http(down, base="https://api.bybit.com", retries=0))
    assert cv.main(["run", "--db", str(tmp_path / "c.sqlite")]) == 1  # a --db other than DB runs without FLAG
    assert "run skipped: ProviderError" in capsys.readouterr().err


# --- hyperliquid_import.fetch_fills ------------------------------------------------------------------------------


def fill(t, tid):
    return {"coin": "ETH", "px": "1", "sz": "1", "side": "B", "time": t, "startPosition": "0", "oid": tid, "tid": tid}


def test_hyperliquid_fetch_fills_pages_and_dedupes():
    starts = []

    def handler(req):
        body = json.loads(req.content)
        assert req.url.path == "/info" and body["type"] == "userFillsByTime" and body["aggregateByTime"] is True
        starts.append(body["startTime"])
        if body["startTime"] == 0:
            return httpx.Response(200, json=[fill(1000 + i, i) for i in range(2000)])  # a full page: ask for more
        return httpx.Response(200, json=[fill(2999, 1999), fill(3500, 5000)])  # one row again, one new

    out = hl.fetch_fills("0xabc", http=mock_http(handler))
    assert starts == [0, 3000]
    assert len(out) == 2001 and out[-1]["tid"] == 5000
    assert [f["time"] for f in out] == sorted(f["time"] for f in out)


def test_hyperliquid_fetch_fills_raises_provider_error_on_failure():
    def handler(req):
        return httpx.Response(500, text="oops")

    with pytest.raises(ProviderError) as e:
        hl.fetch_fills("0xabc", http=mock_http(handler, retries=1))
    assert e.value.reason == "backend_error"
