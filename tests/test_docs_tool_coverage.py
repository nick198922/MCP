"""Guard for tool documentation coverage (item 283 of docs/ideas/IDEA-20261102-standing-improvements.md).

Invariant 5 of CLAUDE.md: every new tool gets a row in the README.md table and in docs/PARITY.md. This test checks
it by machine: every name in EXPECTED_TOOLS (tests/test_server.py) must appear as `name` (in backticks, as a whole
identifier) on a markdown table row (a line starting with `|`) of each document. No network, no server build.
"""

import re
from pathlib import Path

from test_server import EXPECTED_TOOLS

ROOT = Path(__file__).resolve().parents[1]
DOCS = ("README.md", "docs/PARITY.md")

# Tools allowed to be absent from a document, as (document, tool name). Add an entry only with a comment why the
# document legitimately omits the tool; a stale entry (the tool is documented after all) fails the test.
ALLOWLIST: set[tuple[str, str]] = set()


def table_rows(text: str) -> str:
    """Only markdown table rows: a mention in prose does not count as a documented tool. Fenced code is not
    stripped, so a table-looking line inside a fence counts (no such fences in README/PARITY today)."""
    return "\n".join(line for line in text.splitlines() if line.lstrip().startswith("|"))


def documented(name: str, rows: str) -> bool:
    """`name` in backticks; `name(args)` also counts, `name_suffix` (another tool) does not."""
    return re.search("`" + re.escape(name) + r"(?![A-Za-z0-9_])", rows) is not None


def missing_tools(root: Path, tools: set[str], docs: tuple[str, ...] = DOCS) -> set[tuple[str, str]]:
    gaps = set()
    for doc in docs:
        path = root / doc
        rows = table_rows(path.read_text(encoding="utf-8")) if path.is_file() else ""
        gaps |= {(doc, name) for name in tools if not documented(name, rows)}
    return gaps


def test_checker_on_a_fixture_tree(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text(
        "# Tools\n"
        "Prose mentions `alpha_tool` but outside a table.\n"
        "| Tool | What |\n|---|---|\n"
        "| `beta_tool(symbol)` | ok, call form counts |\n"
        "| `gamma_tool_extra` | another tool, not gamma_tool |\n"
        "  | `delta_tool` | indented row counts |\n"
        "```\n| `epsilon_tool` | inside a fence: still a table-looking row, counts (documented limit) |\n```\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "PARITY.md").write_text("| a | `alpha_tool`, `beta_tool` |\n", encoding="utf-8")
    tools = {"alpha_tool", "beta_tool", "gamma_tool", "delta_tool", "epsilon_tool"}
    assert missing_tools(tmp_path, tools) == {
        ("README.md", "alpha_tool"),  # prose only
        ("README.md", "gamma_tool"),  # only a longer name with the same prefix
        ("docs/PARITY.md", "gamma_tool"),
        ("docs/PARITY.md", "delta_tool"),
        ("docs/PARITY.md", "epsilon_tool"),
    }
    # a missing document reports every tool instead of crashing
    assert missing_tools(tmp_path, {"beta_tool"}, ("docs/NOPE.md",)) == {("docs/NOPE.md", "beta_tool")}


def test_every_expected_tool_is_in_readme_and_parity():
    """08.11: 109 tools, 0 gaps in README.md and docs/PARITY.md."""
    assert len(EXPECTED_TOOLS) > 100, "EXPECTED_TOOLS looks truncated"
    gaps = missing_tools(ROOT, EXPECTED_TOOLS)
    new = sorted(gaps - ALLOWLIST)
    assert new == [], "tools without a table row (document, tool); add a row or an ALLOWLIST entry with a reason:\n" + "\n".join(
        f"  {d}: {t}" for d, t in new
    )
    stale = sorted(ALLOWLIST - gaps)
    assert stale == [], f"these ALLOWLIST entries are documented now, remove them: {stale}"
    assert {t for _, t in ALLOWLIST} <= EXPECTED_TOOLS
