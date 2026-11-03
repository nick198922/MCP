"""Guard for tool metadata (CLAUDE.md invariant 5): walks every tool registered by build_server().

Checks: read-only annotation; non-empty English description of a sane length; a `description` on every input
parameter; the 'risk-neutral' caveat on descriptions that talk about probabilities from options (invariant 8).
PARAM_DESCRIPTION_ALLOWLIST is empty since 2026-11-03 and must stay so (a stale entry fails the test).
"""

import re

import pytest
from mcp import Client

MIN_DESCRIPTION_CHARS = 40
CYRILLIC = re.compile(r"[Ѐ-ӿ]")
PROBABILITY = re.compile(r"probabilit", re.I)
OPTIONS = re.compile(r"option", re.I)

# Parameters without a `description` in their JSON schema: 174 params in 55 tools on 2026-11-03 (see
# docs/changes/2026-11-03-tool-metadata-guard.md), cleared in three passes (param-descriptions, -2, -3).
# Empty on purpose: every parameter of every tool must now carry a description. Do not add entries here;
# add `Annotated[..., Field(description=...)]` to the new parameter instead.
PARAM_DESCRIPTION_ALLOWLIST: dict[str, set[str]] = {}


@pytest.fixture
async def tools(server):
    async with Client(server) as c:
        return (await c.list_tools()).tools


async def test_every_tool_is_read_only(tools):
    bad = sorted(t.name for t in tools if t.annotations is None or t.annotations.read_only_hint is not True)
    assert bad == []


async def test_every_description_is_english_and_long_enough(tools):
    empty_or_short = sorted(t.name for t in tools if len((t.description or "").strip()) < MIN_DESCRIPTION_CHARS)
    cyrillic = sorted(t.name for t in tools if CYRILLIC.search(t.description or ""))
    assert empty_or_short == []
    assert cyrillic == []


async def test_options_probability_descriptions_say_risk_neutral(tools):
    bad = sorted(
        t.name
        for t in tools
        if PROBABILITY.search(t.description or "")
        and OPTIONS.search(t.description or "")
        and "risk-neutral" not in (t.description or "").lower()
    )
    assert bad == []


def test_param_description_allowlist_stays_empty():
    assert PARAM_DESCRIPTION_ALLOWLIST == {}


async def test_every_parameter_has_a_description(tools):
    missing: dict[str, set[str]] = {}
    for t in tools:
        props = (t.input_schema or {}).get("properties", {})
        gaps = {p for p, schema in props.items() if not schema.get("description")}
        if gaps:
            missing[t.name] = gaps
    new = {name: gaps - PARAM_DESCRIPTION_ALLOWLIST.get(name, set()) for name, gaps in missing.items()}
    new = {name: gaps for name, gaps in new.items() if gaps}
    stale = {name: params - missing.get(name, set()) for name, params in PARAM_DESCRIPTION_ALLOWLIST.items()}
    stale = {name: params for name, params in stale.items() if params}
    assert new == {}, "add Annotated[..., Field(description=...)] to these parameters"
    assert stale == {}, "these parameters now have a description: remove them from PARAM_DESCRIPTION_ALLOWLIST"
