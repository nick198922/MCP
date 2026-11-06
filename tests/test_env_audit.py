"""scripts/env_audit.py (item 143 of docs/ideas/IDEA-20261102-standing-improvements.md): TRADEDESK_* names, code vs docs."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import env_audit  # noqa: E402

CONFIG = '''
"""Settings; see TRADEDESK_IN_DOCSTRING in the docs."""
import os
from os import environ


def _float(name, default):
    return default


def from_env():
    return dict(
        a=os.environ.get("TRADEDESK_BOTH_FINE", ""),
        b=_float("TRADEDESK_READ_ONLY_SECONDS", 60),
        c=os.getenv("TRADEDESK_GETENV"),
        d=os.environ["TRADEDESK_SUBSCRIPT"],
        e=environ["TRADEDESK_BARE_ENVIRON"],
        f=os.environ.get("COINALYZE_API_KEY"),
    )


def message():
    log("set TRADEDESK_IN_MESSAGE to enable this")  # a sentence, not a name literal
    table = ("job", "TRADEDESK_SECOND_ARG")  # not a read: not the first argument of a call
    return "TRADEDESK_ASSIGNED"  # plain constant, not a read
'''


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_names_read_counts_calls_and_environ_subscripts_only():
    assert env_audit.names_read(CONFIG) == {
        "TRADEDESK_BOTH_FINE",
        "TRADEDESK_READ_ONLY_SECONDS",
        "TRADEDESK_GETENV",
        "TRADEDESK_SUBSCRIPT",
        "TRADEDESK_BARE_ENVIRON",
    }
    assert env_audit.names_read("def broken(:\n") == set()


def test_names_written_skips_prefix_patterns_and_glued_words():
    text = (
        "#TRADEDESK_A_ENABLED=1\n"
        "TRADEDESK_B=2  # and `TRADEDESK_C_SECONDS`\n"
        "Accepted: TRADEDESK_{COLLECTOR,COLLECT}_* and TRADEDESK_COLLECT_* and TRADEDESK_PAPER_\n"
        "XTRADEDESK_GLUED and lower TRADEDESK_lower\n"
    )
    assert env_audit.names_written(text) == {"TRADEDESK_A_ENABLED", "TRADEDESK_B", "TRADEDESK_C_SECONDS"}


def test_audit_on_a_tmp_tree_lists_both_directions_and_writes_no_values(tmp_path):
    _write(tmp_path, "src/tradedesk/config.py", CONFIG)
    _write(tmp_path, "src/tradedesk/__pycache__/stale.py", 'os.environ.get("TRADEDESK_IN_PYCACHE")\n')
    _write(
        tmp_path,
        "deploy/env.example",
        "TRADEDESK_BOTH_FINE=1\n#TRADEDESK_GETENV=x\nTRADEDESK_SUBSCRIPT=y\nTRADEDESK_HTTP_BEARER_TOKENS=s3cr3t-value\n",
    )
    _write(tmp_path, "deploy/features.env", "TRADEDESK_BARE_ENVIRON=1\nTRADEDESK_STALE_ENABLED=1\n")
    _write(tmp_path, "README.md", "Switch: `TRADEDESK_GETENV=1`.\n")
    _write(tmp_path, "docs/ARCHITECTURE.md", "Old: TRADEDESK_STALE_ENABLED.\n")
    _write(tmp_path, "docs/changes/2026-01-01-x.md", "TRADEDESK_READ_ONLY_SECONDS lives in a journal only\n")

    a = env_audit.audit(*env_audit.collect(tmp_path))
    assert a.undocumented == ["TRADEDESK_READ_ONLY_SECONDS"]  # journals are not documentation
    assert a.not_in_template == ["TRADEDESK_BARE_ENVIRON", "TRADEDESK_READ_ONLY_SECONDS"]
    assert a.unread == {
        "TRADEDESK_HTTP_BEARER_TOKENS": ["deploy/env.example"],
        "TRADEDESK_STALE_ENABLED": ["deploy/features.env", "docs/ARCHITECTURE.md"],
    }
    assert "TRADEDESK_IN_PYCACHE" not in a.read

    out = tmp_path / "report" / "env.txt"
    assert env_audit.main(["--root", str(tmp_path), "--out", str(out)]) == 1
    report = out.read_text(encoding="utf-8")
    assert "TRADEDESK_READ_ONLY_SECONDS" in report and "TRADEDESK_STALE_ENABLED" in report
    assert "s3cr3t" not in report and "=" not in report  # names only, never a value


def test_audit_all_fine_returns_zero(tmp_path, capsys):
    _write(tmp_path, "src/tradedesk/config.py", 'import os\nos.environ.get("TRADEDESK_OK_ENABLED")\n')
    _write(tmp_path, "deploy/env.example", "#TRADEDESK_OK_ENABLED=1\n")
    assert env_audit.main(["--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "read but documented nowhere (0)" in out and "documented but not read by the code (0)" in out
