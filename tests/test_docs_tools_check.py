"""scripts/docs_tools_check.py (item 144 of docs/ideas/IDEA-20261102-standing-improvements.md): tool tables vs registry."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import docs_tools_check as dtc  # noqa: E402

README = """# x

Prose mentions `prose_only_tool` and `market_ticker` outside any table: ignored.

## Інструменти (4)

| Група | Інструмент | Що дає |
|---|---|---|
| Ринок | `market_ticker`, `market_ohlcv` | quotes; `probe=true`, `likely_fake`, `COINALYZE_API_KEY` in this column do not count |
| Старе | `ghost_tool` | documented, never registered |

Prose that cuts the table: the rows below lose their header.
| Пізні | `cut_off_tool` | GitHub renders this line as text |
"""

PARITY = """# Паритет

| DOUBLETOP | Наш аналог |
|---|---|
| ccxt `get_ticker` | ✅ `market_ticker` (+ `probe=true`) |
| `risk_hints_token` | 🟡 `risk_hints_token` ≈ `contract_risk`; `trending_tokens` не відтворюємо |
| hl `l2_book` | ✅ `market_ohlcv`, `ghost_tool` і `phantom_tool` |
| defillama | 🔜 `market_ticker`; 🔜 `liquidity_depth` (planned, fine) |

Prose: ✅ `prose_claim_tool` outside a table is ignored.
"""

REGISTRY = {"market_ticker", "market_ohlcv", "contract_risk", "cut_off_tool"}


def test_parse_tables_needs_header_and_separator():
    tables = dtc.parse_tables(README)
    assert len(tables) == 1
    assert tables[0].header == ["Група", "Інструмент", "Що дає"]
    assert [r[0] for r in tables[0].rows] == ["Ринок", "Старе"]  # the cut-off row is not part of the table


def test_names_are_backticked_snake_case_only():
    assert dtc.names("`market_ticker`, `probe=true`, `.A`, `COINALYZE_API_KEY`, `whoami`, `x_1`") == ["market_ticker", "x_1"]


def test_readme_names_take_the_tool_column_only():
    assert dtc.readme_names(README) == {"market_ticker", "market_ohlcv", "ghost_tool"}
    assert dtc.heading_count(README) == 4
    assert dtc.heading_count("# none\n") is None


def test_parity_counts_status_claims_and_drops_doubletop_names():
    claims, mentioned = dtc.parity_names(PARITY)
    assert claims == {
        "✅": {"market_ticker", "market_ohlcv", "ghost_tool", "phantom_tool"},
        "🟡": set(),  # risk_hints_token stands in the DOUBLETOP column
        "🔜": {"market_ticker", "liquidity_depth"},
    }
    assert "prose_claim_tool" not in mentioned and "contract_risk" in mentioned and "trending_tokens" in mentioned


def test_check_lists_both_directions():
    r = dtc.check(README, PARITY, REGISTRY)
    assert r.readme_unregistered == ["ghost_tool"]  # only in the doc
    assert r.readme_missing == ["contract_risk", "cut_off_tool"]  # only in the code (cut-off row and prose don't count)
    assert r.parity_unregistered == ["ghost_tool", "phantom_tool"]
    assert r.parity_stale_planned == ["market_ticker"]
    assert r.parity_unmentioned == ["cut_off_tool"]
    assert r.failed


def test_main_writes_report_and_exit_code(tmp_path, capsys):
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    (tmp_path / "docs" / "PARITY.md").write_text(PARITY, encoding="utf-8")
    out = tmp_path / "out" / "report.txt"
    assert dtc.main(["--root", str(tmp_path), "--out", str(out)], registry=REGISTRY) == 1
    assert "ghost_tool" in out.read_text(encoding="utf-8")

    fine_readme = "## Інструменти (2)\n\n| Група | Інструмент |\n|---|---|\n| a | `market_ticker`, `market_ohlcv` |\n"
    fine_parity = "| DOUBLETOP | Наш аналог |\n|---|---|\n| x | ✅ `market_ticker` |\n"
    (tmp_path / "README.md").write_text(fine_readme, encoding="utf-8")
    (tmp_path / "docs" / "PARITY.md").write_text(fine_parity, encoding="utf-8")
    capsys.readouterr()
    assert dtc.main(["--root", str(tmp_path)], registry={"market_ticker", "market_ohlcv"}) == 0
    assert "registered, not named in docs/PARITY.md tables (information only) (1)" in capsys.readouterr().out
