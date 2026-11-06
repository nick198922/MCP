"""scripts/tool_desc_audit.py (item 149 of docs/ideas/IDEA-20261102-standing-improvements.md): tool description audit."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import tool_desc_audit as tda  # noqa: E402

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}


def _tool(name="market_ticker", description="Last price in USD.", params=None, annotations=READ_ONLY):
    props = {"symbol": {"type": "string", "description": "Unified ccxt symbol."}} if params is None else params
    return {"name": name, "description": description, "input_schema": {"type": "object", "properties": props}, "annotations": annotations}


def _checks(tool):
    return [(f.check, f.detail) for f in tda.check_tool(tool)]


def test_normal_tool_has_no_findings():
    assert tda.check_tool(_tool()) == []
    assert tda.check_tool(_tool(params={})) == []  # a tool without parameters
    # the non-idempotent read-only variant (carry_paper_open) passes too
    assert tda.check_tool(_tool(annotations={**READ_ONLY, "idempotentHint": False})) == []
    gex = _tool(name="deribit_gamma_exposure", description="Dealer gamma by strike, USD. Heuristic: assumes dealers are short.")
    prob = _tool(name="prob_from_options", description="Risk neutral probability of finishing above a level.")
    assert tda.check_tool(gex) == [] and tda.check_tool(prob) == []


def test_cyrillic_in_description_and_parameter():
    found = _checks(_tool(description="Last price in USD (ціна)."))
    assert [c for c, _ in found] == ["cyrillic_description"]
    assert "ціна" in found[0][1]
    assert _checks(_tool(params={"symbol": {"type": "string", "description": "Символ"}})) == [("cyrillic_param", "symbol")]


def test_empty_description_and_parameter_without_description():
    assert _checks(_tool(description="  ")) == [("empty_description", "")]
    assert _checks(_tool(description=None)) == [("empty_description", "")]
    params = {"b": {"type": "integer"}, "a": {"type": "string", "description": ""}, "c": {"type": "string", "description": "ok"}}
    assert _checks(_tool(params=params)) == [("param_no_description", "a"), ("param_no_description", "b")]


def test_missing_read_only_annotation():
    assert [c for c, _ in _checks(_tool(annotations=None))] == ["not_read_only"]
    assert [c for c, _ in _checks(_tool(annotations={**READ_ONLY, "destructiveHint": True}))] == ["not_read_only"]
    assert [c for c, _ in _checks(_tool(annotations={**READ_ONLY, "readOnlyHint": False}))] == ["not_read_only"]


def test_missing_risk_neutral_or_heuristic_caveat():
    plain = "Probability of finishing above a level, decimal; exposure in USD."
    for name in ("prob_from_options", "touch_probability", "deribit_gex", "deribit_gamma_exposure", "implied_move", "risk_neutral_density"):
        assert _checks(_tool(name=name, description=plain)) == [("missing_caveat", "no 'risk-neutral' or 'heuristic' in the description")]
    # names without a token are not checked for the caveat
    assert _checks(_tool(name="odds_devig", description=plain)) == []


def test_unit_words_in_name_tokens():
    # every name token passes with one of its unit words
    ok = {
        "deribit_vol_surface": "ATM IVs are decimals (0.45 = 45 %).",
        "iv_rank": "Rank of ATM IV; IVs in vol points.",
        "perps_funding_history": "Rates are fractions per interval.",
        "perps_basis_compare": "Premium in bps.",
        "realized_volatility": "Annualised, in percent.",
        "vega_ladder": "Vega per 1 vol point.",
        "theta_decay": "Theta in USD per calendar day.",
        "greeks_table": "Greeks in USD.",
        "deribit_gex": "Net GEX in USD per 1 % move. Heuristic.",
        "premium_scan": "Premium in BTC.",
    }
    for name, desc in ok.items():
        assert tda.check_tool(_tool(name=name, description=desc)) == [], name
    # the same names without any unit word
    for name in ok:
        assert [c for c, _ in _checks(_tool(name=name, description="Summary. Heuristic."))] == ["missing_unit"], name
    assert _checks(_tool(name="funding_basis_mix", description="Summary.")) == [
        ("missing_unit", "name token 'basis': description names no unit"),
        ("missing_unit", "name token 'funding': description names no unit"),
    ]
    # a token is a whole segment: volume, ivy, derivatives, percentile, "fraction of" do not count
    assert _checks(_tool(name="market_volume", description="Summary.")) == []
    assert _checks(_tool(name="derivatives_overview", description="Summary.")) == []
    assert [c for c, _ in _checks(_tool(name="iv_rank", description="Percentile = fraction of past points below."))] == ["missing_unit"]
    # vega needs "vol point", USD alone is not enough; theta needs "per day"
    assert [c for c, _ in _checks(_tool(name="vega_ladder", description="Vega in USD."))] == ["missing_unit"]
    assert [c for c, _ in _checks(_tool(name="theta_decay", description="Theta in USD."))] == ["missing_unit"]


def test_unit_words_in_parameters():
    def params(**descs):
        return {k: {"type": "number", "description": v} for k, v in descs.items()}

    good = params(iv="Flat IV, decimal (0.45 = 45 %).", call_premium="USD per contract.", entry_cost_vol="0.02 = 2 vol points.")
    assert tda.check_tool(_tool(params=good)) == []
    bad = params(iv="Flat IV.", call_premium="Premium at entry.", gamma="Per-contract gamma.", theta="Per-contract theta.")
    assert _checks(_tool(params=bad)) == [
        ("missing_unit", "param 'call_premium': description names no unit"),
        ("missing_unit", "param 'gamma': description names no unit"),
        ("missing_unit", "param 'iv': description names no unit"),
        ("missing_unit", "param 'theta': description names no unit"),
    ]
    # only the last segment is the quantity: funding_hours_ahead is hours, not a funding rate
    assert _checks(_tool(params=params(funding_hours_ahead="How far ahead, hours."))) == []
    # the tool description does not stand in for the parameter's own description
    assert [c for c, _ in _checks(_tool(description="IV is a decimal.", params=params(iv="Flat IV.")))] == ["missing_unit"]
    # a parameter without a description is reported once, as param_no_description
    assert _checks(_tool(params={"iv": {"type": "number"}})) == [("param_no_description", "iv")]


def test_audit_render_and_main(tmp_path, monkeypatch):
    tools = [_tool(name="z_tool"), _tool(name="prob_x", description="Мова"), _tool(name="a_tool", params={"x": {}})]
    r = tda.audit(tools)
    assert r.tools == 3
    assert [(f.tool, f.check) for f in r.findings] == [
        ("a_tool", "param_no_description"),
        ("prob_x", "cyrillic_description"),
        ("prob_x", "missing_caveat"),
    ]
    text = tda.render(r)
    assert "Tools: 3; findings: 3; tools with findings: 2." in text
    assert "By check: cyrillic_description: 1, missing_caveat: 1, param_no_description: 1." in text
    assert "- `a_tool` param_no_description: x" in text
    assert "- none" in tda.render(tda.audit([_tool()]))

    monkeypatch.setattr(tda, "load_tools", lambda: tools)
    out = tmp_path / "out/audit.md"
    assert tda.main(["--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == text


def test_registered_tools_load_and_pass():
    """The real registry through build_server(): every tool listed, no findings (06.11: 109 tools, 0 findings;
    item 183: 23 tools unit-checked, 7 missing_unit fixed in the descriptions)."""
    from test_server import EXPECTED_TOOLS

    tools = tda.load_tools()
    assert EXPECTED_TOOLS <= {t["name"] for t in tools}
    assert all(t["input_schema"].get("type") == "object" for t in tools)
    assert tda.audit(tools).findings == []
