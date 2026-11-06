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
    gex = _tool(name="deribit_gamma_exposure", description="Dealer gamma by strike. Heuristic: assumes dealers are short.")
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
    plain = "Probability of finishing above a level, decimal."
    for name in ("prob_from_options", "touch_probability", "deribit_gex", "deribit_gamma_exposure", "implied_move", "risk_neutral_density"):
        assert _checks(_tool(name=name, description=plain)) == [("missing_caveat", "no 'risk-neutral' or 'heuristic' in the description")]
    # names without a token are not checked for the caveat
    assert _checks(_tool(name="odds_devig", description=plain)) == []


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
    """The real registry through build_server(): every tool listed, no findings (06.11: 109 tools, 0 findings)."""
    from test_server import EXPECTED_TOOLS

    tools = tda.load_tools()
    assert EXPECTED_TOOLS <= {t["name"] for t in tools}
    assert all(t["input_schema"].get("type") == "object" for t in tools)
    assert tda.audit(tools).findings == []
