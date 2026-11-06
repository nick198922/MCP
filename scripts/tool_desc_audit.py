"""Audit of the registered MCP tool descriptions against invariant 5 of CLAUDE.md.

Read-only, stdlib only (plus the project itself, to list the tools). The server is built with
``Runtime(Settings(db_path=""))`` and listed through ``MCPServer.list_tools()``: no lifespan, no collector,
no store, no network. Checks per tool:

- ``empty_description``: the description is missing or blank.
- ``cyrillic_description``: the tool description contains Cyrillic (descriptions are English, invariant 5).
- ``param_no_description``: an input parameter has no ``description`` in the input schema
  (``Annotated[..., Field(description=...)]`` missing).
- ``cyrillic_param``: a parameter description contains Cyrillic.
- ``not_read_only``: annotations missing, or ``readOnlyHint`` is not true, or ``destructiveHint`` is not false
  (``READ_ONLY`` and its non-idempotent variant both pass).
- ``missing_caveat``: the tool name contains one of ``CAVEAT_TOKENS`` (probability / GEX tools) and the description
  has neither ``risk-neutral`` nor ``heuristic`` (case-insensitive; ``risk neutral`` also counts).

    uv run python scripts/tool_desc_audit.py [--out ~/.cache/tradedesk-agents/tmp/tool_desc_audit.md]

Exit code 0 always (an audit, not a gate). Code is never changed.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# "gex", "implied", "risk_neutral" and "prob" from the card; "gamma_exposure" added: the GEX tool is
# deribit_gamma_exposure, whose name has no "gex".
CAVEAT_TOKENS = ("prob", "gex", "gamma_exposure", "implied", "risk_neutral")
_CAVEAT = re.compile(r"risk[- ]neutral|heuristic", re.IGNORECASE)
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")


@dataclass(frozen=True)
class Finding:
    tool: str
    check: str
    detail: str = ""


@dataclass
class Report:
    tools: int = 0
    findings: list[Finding] = field(default_factory=list)


def check_tool(tool: dict[str, Any]) -> list[Finding]:
    """Findings for one tool: ``{"name", "description", "input_schema", "annotations"}`` (plain dicts). Pure."""
    name = str(tool.get("name") or "")
    desc = tool.get("description") or ""
    out: list[Finding] = []
    if not desc.strip():
        out.append(Finding(name, "empty_description"))
    elif m := _CYRILLIC.search(desc):
        out.append(Finding(name, "cyrillic_description", f"near {desc[max(0, m.start() - 20) : m.start() + 20]!r}"))
    props = (tool.get("input_schema") or {}).get("properties") or {}
    for pname in sorted(props):
        pdesc = (props[pname] or {}).get("description") or ""
        if not pdesc.strip():
            out.append(Finding(name, "param_no_description", pname))
        elif _CYRILLIC.search(pdesc):
            out.append(Finding(name, "cyrillic_param", pname))
    ann = tool.get("annotations") or {}
    if ann.get("readOnlyHint") is not True or ann.get("destructiveHint") is not False:
        out.append(Finding(name, "not_read_only", f"readOnlyHint={ann.get('readOnlyHint')}, destructiveHint={ann.get('destructiveHint')}"))
    if any(t in name.lower() for t in CAVEAT_TOKENS) and not _CAVEAT.search(desc):
        out.append(Finding(name, "missing_caveat", "no 'risk-neutral' or 'heuristic' in the description"))
    return out


def audit(tools: list[dict[str, Any]]) -> Report:
    """Run ``check_tool`` over every tool, sorted by name. Pure."""
    r = Report(tools=len(tools))
    for t in sorted(tools, key=lambda x: str(x.get("name") or "")):
        r.findings.extend(check_tool(t))
    return r


def render(r: Report) -> str:
    by_check: dict[str, int] = {}
    for f in r.findings:
        by_check[f.check] = by_check.get(f.check, 0) + 1
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(by_check.items())) or "none"
    lines = [
        "# MCP tool description audit (invariant 5)",
        "",
        f"Tools: {r.tools}; findings: {len(r.findings)}; tools with findings: {len({f.tool for f in r.findings})}.",
        f"By check: {summary}.",
        "",
        "## Findings",
        *([f"- `{f.tool}` {f.check}" + (f": {f.detail}" if f.detail else "") for f in r.findings] or ["- none"]),
    ]
    return "\n".join(lines) + "\n"


def load_tools() -> list[dict[str, Any]]:
    """Registered tools as plain dicts. Builds the server without its lifespan: no store, collector or network."""
    import asyncio  # noqa: PLC0415

    from tradedesk.config import Settings  # noqa: PLC0415
    from tradedesk.runtime import Runtime  # noqa: PLC0415
    from tradedesk.server import build_server  # noqa: PLC0415

    server, _ = build_server(Runtime(Settings(db_path="")))
    listed = asyncio.run(server.list_tools())
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
            "annotations": t.annotations.model_dump(by_alias=True) if t.annotations else None,
        }
        for t in listed
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, help="also write the report to this file")
    args = ap.parse_args(argv)
    text = render(audit(load_tools()))
    print(text, end="")
    if args.out:
        out = args.out.expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
