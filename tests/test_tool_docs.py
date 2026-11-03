"""Guard for tool documentation (CLAUDE.md invariant 5): every tool registered by build_server() is in README.md.

Only the table rows of the README section "## Інструменти" count (a name in backticks in a `|` row), so a
tool mentioned elsewhere in prose does not pass. docs/PARITY.md is not checked: it maps DOUBLETOP tools to
ours and by design does not list every tool of ours (see docs/changes/2026-11-04-tool-docs-guard.md).
"""

import re
from pathlib import Path

import pytest
from mcp import Client

README = Path(__file__).resolve().parents[1] / "README.md"
TOOLS_SECTION = re.compile(r"^## Інструменти\b.*?(?=^## )", re.M | re.S)
BACKTICKED = re.compile(r"`([a-z0-9_]+)`")

# Tools allowed to be missing from the README table. Zero gaps on 2026-11-04 (108 tools, all in the table),
# so empty on purpose: add a README row for a new tool instead of an entry here (a stale entry fails the test).
README_ALLOWLIST: set[str] = set()


def readme_table_names() -> set[str]:
    section = TOOLS_SECTION.search(README.read_text(encoding="utf-8"))
    assert section is not None, "README.md lost the '## Інструменти' section"
    rows = (line for line in section.group(0).splitlines() if line.startswith("|"))
    return {name for row in rows for name in BACKTICKED.findall(row)}


@pytest.fixture
async def tool_names(server) -> set[str]:
    async with Client(server) as c:
        return {t.name for t in (await c.list_tools()).tools}


def test_readme_table_is_found():
    assert "system_health" in readme_table_names()


async def test_every_tool_is_in_readme_table(tool_names):
    documented = readme_table_names()
    missing = tool_names - documented
    assert missing - README_ALLOWLIST == set(), "add these tools to the README.md table (section '## Інструменти')"
    assert README_ALLOWLIST - missing == set(), "these tools are now in README.md: remove them from README_ALLOWLIST"
