"""contract_risk second source: honeypot.is (live samples in tests/fixtures/honeypot_is_*.json) behind MockTransport."""

import copy

import httpx
import pytest

from test_contract_risk import HP_LIVE, NOW, SHIB, by_name, call, clean_token, make_server
from tradedesk.analytics import contract_risk as cr
from tradedesk.config import Settings
from tradedesk.core.errors import ProviderError
from tradedesk.runtime import Runtime

LIQ = {"liquidity_usd": 200_000.0, "market_cap_usd": 1_000_000.0}  # liquidity_to_mcap passes


def merged(token, hp_body, **kw):
    hp = None if hp_body is None else cr.parse_honeypot_is(hp_body)
    return cr.cross_check(cr.assess(token, now=NOW, **(LIQ | kw)), hp)


def hp_body(**sim):
    """The live SHIB answer with other simulated taxes (percent, as honeypot.is sends them)."""
    b = copy.deepcopy(HP_LIVE["shib_ethereum"])
    b["simulationResult"] |= sim
    return b


# ---------- pure analytics ----------


def test_parse_live_samples():
    weth = cr.parse_honeypot_is(HP_LIVE["weth_ethereum"])
    assert weth == {
        "simulation_success": True,
        "is_honeypot": False,
        "buy_tax": 0.0,
        "sell_tax": 0.0,
        "risk": "low",
        "risk_level": 1,
        "flags": [],
    }
    floki = cr.parse_honeypot_is(HP_LIVE["floki_ethereum"])
    assert floki["buy_tax"] == pytest.approx(0.003) and floki["sell_tax"] == pytest.approx(0.003)  # percent -> decimal
    grow = cr.parse_honeypot_is(HP_LIVE["evergrow_bsc"])
    assert grow["is_honeypot"] is True and grow["sell_tax"] == 1.0 and grow["risk"] == "honeypot"
    sfm = cr.parse_honeypot_is(HP_LIVE["safemoon_bsc"])
    assert sfm["simulation_success"] is False and sfm["is_honeypot"] is None
    assert sfm["buy_tax"] is None and sfm["flags"] == ["high_fail_rate"]
    assert cr.parse_honeypot_is("not a dict")["simulation_success"] is None


def test_both_sources_clean_pass():
    res = merged(clean_token(), HP_LIVE["weth_ethereum"])
    assert res["overall"] == "pass" and res["passed_count"] == 25 and res["unknown"] == []
    c = by_name(res)
    assert c["holder_sell_fail_rate"]["verdict"] == "pass" and "sources" not in c["holder_sell_fail_rate"]
    assert [x["name"] for x in res["checks"]][:3] == ["honeypot", "cannot_sell_all", "holder_sell_fail_rate"]
    assert c["honeypot"]["sources"] == {"goplus": {"verdict": "pass", "value": False}, "honeypot_is": {"verdict": "pass", "value": False}}
    assert c["honeypot"]["detail"].startswith("verdict from GoPlus")
    assert c["sell_tax"]["value"] == 0.01  # the higher of GoPlus 0.01 and honeypot.is 0.0
    assert res["second_source"] == {
        "provider": "honeypot.is",
        "available": True,
        "disagreements": [],
        "simulation_success": True,
        "risk": "low",
        "risk_level": 1,
        "flags": [],
    }


def test_live_disagreement_takes_worse_verdict():
    # EverGrow (BSC), same minute: GoPlus is_honeypot "0", honeypot.is isHoneypot true with a 100 % sell tax.
    res = merged(clean_token(), HP_LIVE["evergrow_bsc"])
    c = by_name(res)
    assert c["honeypot"]["verdict"] == "fail" and c["honeypot"]["sources"]["goplus"]["verdict"] == "pass"
    # value and detail come from the source that decided the verdict (honeypot.is), not GoPlus's "not flagged"
    for name in ("honeypot", "cannot_sell_all"):
        assert c[name]["verdict"] == "fail" and c[name]["value"] is True, name
        assert c[name]["detail"].startswith("verdict from honeypot.is: honeypot.is buy/sell simulation: the sell failed"), name
    assert c["sell_tax"]["verdict"] == "fail" and c["sell_tax"]["value"] == 1.0
    assert c["sell_tax"]["detail"].startswith("verdict from honeypot.is")
    assert c["buy_tax"]["verdict"] == "pass"
    # medium_fail_rate (honeypot.is severity "high") is its own red flag, not a critical cannot_sell_all
    assert c["holder_sell_fail_rate"]["verdict"] == "fail" and c["holder_sell_fail_rate"]["severity"] == "red_flag"
    assert res["overall"] == "fail" and res["critical_failed"] == ["honeypot", "cannot_sell_all"]
    assert [d["check"] for d in res["second_source"]["disagreements"]] == ["honeypot", "cannot_sell_all", "sell_tax"]


def test_honeypot_is_pass_never_upgrades_goplus():
    res = merged(clean_token(is_honeypot="1", buy_tax=""), HP_LIVE["weth_ethereum"])
    c = by_name(res)
    assert c["honeypot"]["verdict"] == "fail"
    assert c["buy_tax"]["verdict"] == "unknown" and c["buy_tax"]["value"] == 0.0  # GoPlus unknown, honeypot.is 0
    assert [d["check"] for d in res["second_source"]["disagreements"]] == ["honeypot"]


def test_tax_gap_over_two_points_is_a_disagreement():
    res = merged(clean_token(sell_tax="0.01"), hp_body(sellTax=4))  # 1 % vs 4 %: both pass, 3 points apart
    c = by_name(res)
    assert c["sell_tax"]["verdict"] == "pass" and c["sell_tax"]["value"] == pytest.approx(0.04)
    assert [d["check"] for d in res["second_source"]["disagreements"]] == ["sell_tax"]
    res = merged(clean_token(sell_tax="0.01"), hp_body(sellTax=3))  # exactly 2 points: no disagreement
    assert res["second_source"]["disagreements"] == []
    res = merged(clean_token(buy_tax="0.049"), hp_body(buyTax=5.1))  # 0.2 points apart, but pass vs fail
    assert by_name(res)["buy_tax"]["verdict"] == "fail"
    assert [d["check"] for d in res["second_source"]["disagreements"]] == ["buy_tax"]


def test_second_source_missing_means_no_green():
    res = merged(clean_token(sell_tax="0.2"), None)
    c = by_name(res)
    for name in ("honeypot", "cannot_sell_all", "buy_tax"):
        assert c[name]["verdict"] == "unknown", name
        assert c[name]["sources"]["honeypot_is"] == {"verdict": "unknown", "value": None}
    assert c["honeypot"]["value"] is False  # GoPlus's value is kept when honeypot.is gave no answer
    assert c["sell_tax"]["verdict"] == "fail"  # a GoPlus fail stays a fail
    assert c["holder_sell_fail_rate"]["verdict"] == "unknown"
    assert res["second_source"] == {"provider": "honeypot.is", "available": False, "disagreements": []}
    assert merged(clean_token(), None)["overall"] == "unknown"


def test_failed_simulation_is_unknown_and_high_fail_rate_is_its_own_critical_check():
    res = merged(clean_token(), HP_LIVE["safemoon_bsc"])
    c = by_name(res)
    assert {c[n]["verdict"] for n in cr.CROSS_CHECKED} == {"unknown"}  # cannot_sell_all rests on isHoneypot only
    assert c["cannot_sell_all"]["sources"]["honeypot_is"] == {"verdict": "unknown", "value": None}
    fr = c["holder_sell_fail_rate"]
    assert (fr["verdict"], fr["severity"], fr["value"]) == ("fail", "critical", "high_fail_rate")
    assert res["critical_failed"] == ["holder_sell_fail_rate"] and res["second_source"]["disagreements"] == []


def test_fail_rate_flag_after_clean_simulation_is_a_red_flag_not_a_disagreement():
    body = copy.deepcopy(HP_LIVE["weth_ethereum"]) | {"flags": ["medium_fail_rate"]}
    res = merged(clean_token(), body)
    c = by_name(res)
    assert c["cannot_sell_all"]["verdict"] == "pass" and c["honeypot"]["verdict"] == "pass"
    assert (c["holder_sell_fail_rate"]["verdict"], c["holder_sell_fail_rate"]["severity"]) == ("fail", "red_flag")
    assert res["overall"] == "fail" and res["critical_failed"] == [] and res["failed"] == ["holder_sell_fail_rate"]
    assert res["second_source"]["disagreements"] == []


@pytest.mark.parametrize("flags", ["absent", None, "high_fail_rate", [{"flag": "high_fail_rate"}], ["low_liquidity", 3]])
def test_missing_or_malformed_flags_never_pass(flags):
    # Live WETH answer (clean simulation, summary.flags == []) with the top-level flag list gone or of another shape.
    body = copy.deepcopy(HP_LIVE["weth_ethereum"])
    if flags == "absent":
        del body["flags"]
    else:
        body["flags"] = flags
    assert cr.parse_honeypot_is(body)["flags"] is None
    res = merged(clean_token(), body)
    fr = by_name(res)["holder_sell_fail_rate"]
    assert fr["verdict"] == "unknown" and "missing or malformed" in fr["detail"]
    assert res["overall"] == "unknown" and res["unknown"] == ["holder_sell_fail_rate"]


def test_summary_flag_objects_still_fail_without_top_level_list():
    # Top-level list gone, but summary.flags (object shape, as in the live SafeMoon answer) names high_fail_rate.
    body = copy.deepcopy(HP_LIVE["weth_ethereum"])
    del body["flags"]
    body["summary"]["flags"] = [{"flag": "high_fail_rate", "description": "x", "severity": "critical"}]
    assert cr.parse_honeypot_is(body)["flags"] == ["high_fail_rate"]
    res = merged(clean_token(), body)
    fr = by_name(res)["holder_sell_fail_rate"]
    assert (fr["verdict"], fr["severity"]) == ("fail", "critical")
    assert res["overall"] == "fail" and res["critical_failed"] == ["holder_sell_fail_rate"]
    # With a well-formed top-level list the two are joined, so a flag only in summary.flags is not lost.
    body["flags"] = []
    assert cr.parse_honeypot_is(body)["flags"] == ["high_fail_rate"]


def test_risk_honeypot_without_is_honeypot_fails():
    body = copy.deepcopy(HP_LIVE["evergrow_bsc"])
    body["honeypotResult"]["isHoneypot"] = None
    c = by_name(merged(clean_token(), body))
    assert c["honeypot"]["verdict"] == "fail" and c["honeypot"]["value"] is True
    assert "risk as 'honeypot'" in c["honeypot"]["detail"]


def test_negative_honeypot_is_tax_is_unknown():
    hp = cr.parse_honeypot_is(hp_body(sellTax=-1))
    assert hp["sell_tax"] is None
    c = by_name(merged(clean_token(), hp_body(sellTax=-1)))
    assert c["sell_tax"]["verdict"] == "unknown" and c["sell_tax"]["sources"]["honeypot_is"]["verdict"] == "unknown"


# ---------- MCP tool ----------


async def test_tool_sends_lower_case_address_and_chain_id():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=copy.deepcopy(HP_LIVE["shib_ethereum"]))

    env = await call(make_server(hp_handler=handler), "contract_risk", chain="eth", address="0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE")
    assert env["ok"] is True and env["missing"] == []
    assert seen[0].url.path == "/v2/IsHoneypot"
    assert seen[0].url.params["address"] == SHIB and seen[0].url.params["chainID"] == "1"
    assert by_name(env["data"])["honeypot"]["verdict"] == "pass"
    assert env["data"]["second_source"]["available"] is True


async def test_tool_honeypot_is_failure_keeps_goplus_without_green():
    srv = make_server(hp_handler=lambda req: httpx.Response(503, text="down"))
    env = await call(srv, "contract_risk", chain="ethereum", address=SHIB)
    assert env["ok"] is True
    assert [(m["field"], m["provider"]) for m in env["missing"]] == [("honeypot_is", "honeypot_is")]
    c = by_name(env["data"])
    assert {c[n]["verdict"] for n in cr.CROSS_CHECKED} == {"unknown"}  # GoPlus passed all four for SHIB
    assert c["open_source"]["verdict"] == "pass"  # the rest of GoPlus still counts
    assert any("rest on GoPlus alone" in w for w in env["warnings"])


async def test_tool_unsupported_chain_makes_no_honeypot_request():
    seen = []
    srv = make_server(hp_handler=lambda req: seen.append(req) or httpx.Response(200, json=HP_LIVE["weth_ethereum"]))
    env = await call(srv, "contract_risk", chain="polygon", address=SHIB)
    assert env["ok"] is True and seen == []
    miss = [m for m in env["missing"] if m["provider"] == "honeypot_is"]
    assert miss and miss[0]["reason"] == "data_unavailable"


async def test_tool_warns_on_disagreement_and_failed_simulation():
    srv = make_server(hp_handler=lambda req: httpx.Response(200, json=HP_LIVE["evergrow_bsc"]))
    env = await call(srv, "contract_risk", chain="ethereum", address=SHIB)
    assert env["data"]["overall"] == "fail"
    assert any("disagree on honeypot" in w and "worse verdict" in w for w in env["warnings"])
    srv = make_server(hp_handler=lambda req: httpx.Response(200, json=HP_LIVE["safemoon_bsc"]))
    env = await call(srv, "contract_risk", chain="ethereum", address=SHIB)
    assert any("could not simulate" in w for w in env["warnings"])
    assert all("execution reverted" not in w for w in env["warnings"])  # third-party text is not echoed


async def test_tool_warns_on_high_risk_without_honeypot():
    body = copy.deepcopy(HP_LIVE["shib_ethereum"])
    body["summary"] |= {"risk": "very_high", "riskLevel": 80}
    env = await call(make_server(hp_handler=lambda req: httpx.Response(200, json=body)), "contract_risk", chain="eth", address=SHIB)
    assert any("overall risk as very_high" in w for w in env["warnings"])
    env = await call(make_server(), "contract_risk", chain="eth", address=SHIB)  # live SHIB: risk low
    assert all("overall risk as" not in w for w in env["warnings"])


# ---------- provider ----------


async def test_provider_errors_and_shape():
    def handler(req):
        if req.url.params["address"] == "0x" + "1" * 40:
            return httpx.Response(404, json={"code": 404, "error": "Token not found"})
        return httpx.Response(200, json={"unexpected": True})

    rt = Runtime(Settings(http_retries=0, cache_scale=0.0), transports={"honeypot_is": httpx.MockTransport(handler)})
    rt.now = lambda: NOW
    try:
        with pytest.raises(ProviderError) as e:
            await rt.honeypot_is.token_check("1", "0x" + "1" * 40)
        assert e.value.reason == "data_unavailable"
        with pytest.raises(ProviderError) as e:
            await rt.honeypot_is.token_check("1", SHIB)
        assert e.value.reason == "backend_error"
        with pytest.raises(ProviderError) as e:
            await rt.honeypot_is.token_check("137", SHIB)
        assert e.value.reason == "data_unavailable"
        assert rt.health.snapshot()["providers"]["honeypot_is"]["last_reason"] == "backend_error"
    finally:
        await rt.aclose()
