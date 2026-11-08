"""scripts/units_check.py (item 145 of docs/ideas/IDEA-20261102-standing-improvements.md): user units vs repository."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import units_check as uc  # noqa: E402

GOOD_SERVICE = """[Unit]
Description=ok
# ExecStart=/bin/bash scripts/commented_out.sh   <- a comment, never checked
; ExecStart=python scripts/also_commented.py

[Service]
Type=oneshot
WorkingDirectory=%h/tradedesk-mcp
EnvironmentFile=-%h/.config/tradedesk/secret.env
Environment=TRADEDESK_SECRET_PATH=never/read.py
ExecStart=/home/tradedesk/.local/bin/uv run --quiet python scripts/good.py \\
    --json /var/lib/tradedesk-mcp/out/latest.json
"""

GOOD_TIMER = """[Timer]
OnCalendar=Mon 09:00
"""

ABS_SERVICE = """[Service]
ExecStart=/bin/bash %h/tradedesk-mcp/scripts/backup.sh hourly
"""

SHELL_SERVICE = """[Service]
WorkingDirectory=/home/tradedesk/tradedesk-mcp
ExecStart=/bin/bash -c '{ printf "a 3.1 %% b\\n"; cat docs/NOTE.md; } | python3 scripts/tg_send.py --topic chat'
"""

MODULE_SERVICE = """[Service]
WorkingDirectory=/home/tradedesk/tradedesk-mcp
ExecStart=/home/tradedesk/.local/bin/uv run python -m tradedesk.execution.demo_trader --manage
"""

GONE_SERVICE = """[Service]
WorkingDirectory=/home/tradedesk/tradedesk-mcp
ExecStart=/usr/bin/python3 -I scripts/gone.py run
"""

NO_WD_SERVICE = """[Service]
ExecStart=/usr/bin/python3 scripts/good.py
"""

EXPLICIT_TIMER = """[Timer]
Unit=tradedesk-abs.service
OnCalendar=hourly
"""

REPO = {
    "scripts/good.py",
    "scripts/backup.sh",
    "scripts/tg_send.py",
    "docs/NOTE.md",
    "src/tradedesk/execution/demo_trader.py",
}


def exists(rel: str) -> bool:
    return rel in REPO


def test_parse_unit_skips_comments_and_joins_continuations():
    unit = uc.parse_unit(GOOD_SERVICE)
    assert unit["Service"]["WorkingDirectory"] == ["%h/tradedesk-mcp"]
    (line,) = unit["Service"]["ExecStart"]
    assert "scripts/good.py" in line and "--json /var/lib" in line
    assert "commented_out" not in str(unit) and "also_commented" not in str(unit)


def test_expand_specifiers():
    assert uc.expand("%h/tradedesk-mcp", "/home/x") == "/home/x/tradedesk-mcp"
    assert uc.expand("3.1 %% b %n") == "3.1 % b %n"


def test_command_refs_forms():
    wd = uc.DEPLOY_ROOT
    refs = uc.command_refs("a.service", uc.parse_unit(GOOD_SERVICE)["Service"]["ExecStart"][0], wd)
    # the --json value is an output path outside the repository: not a reference
    assert [(r.kind, r.path) for r in refs] == [("script", "scripts/good.py")]

    refs = uc.command_refs("b.service", "/bin/bash %h/tradedesk-mcp/scripts/backup.sh hourly", None)
    assert [(r.kind, r.path) for r in refs] == [("script", "scripts/backup.sh")]

    shell = uc.parse_unit(SHELL_SERVICE)["Service"]["ExecStart"][0]
    refs = uc.command_refs("c.service", shell, wd)
    assert sorted((r.kind, r.path) for r in refs) == [("file", "docs/NOTE.md"), ("script", "scripts/tg_send.py")]

    refs = uc.command_refs("d.service", uc.parse_unit(MODULE_SERVICE)["Service"]["ExecStart"][0], wd)
    assert [(r.kind, r.path) for r in refs] == [("module", "src/tradedesk/execution/demo_trader")]


def test_check_clean_set():
    units = {
        "tradedesk-good.service": GOOD_SERVICE,
        "tradedesk-good.timer": GOOD_TIMER,
        "tradedesk-abs.service": ABS_SERVICE,
        "tradedesk-hourly.timer": EXPLICIT_TIMER,
        "tradedesk-shell.service": SHELL_SERVICE,
        "tradedesk-mod.service": MODULE_SERVICE,
    }
    r = uc.check(units, exists)
    assert (r.services, r.timers) == (4, 2)
    assert not r.failed
    assert r.missing == [] and r.unresolved == [] and r.timer_orphans == []
    # informational: services that no timer starts
    assert r.untimed_services == ["tradedesk-mod.service", "tradedesk-shell.service"]
    assert "references checked (5)" in uc.render(r)


def test_check_missing_script_orphan_timer_and_no_workdir():
    units = {
        "tradedesk-gone.service": GONE_SERVICE,
        "tradedesk-gone.timer": GOOD_TIMER,
        "tradedesk-lonely.timer": GOOD_TIMER,
        "tradedesk-explicit.timer": "[Timer]\nUnit=tradedesk-nowhere.service\n",
        "tradedesk-nowd.service": NO_WD_SERVICE,
        "tradedesk-empty.service": "[Service]\nType=oneshot\n",
    }
    r = uc.check(units, exists)
    assert r.failed
    assert [(x.unit, x.path) for x in r.missing] == [("tradedesk-gone.service", "scripts/gone.py")]
    assert [(x.unit, x.raw) for x in r.unresolved] == [("tradedesk-nowd.service", "scripts/good.py")]
    assert r.no_exec == ["tradedesk-empty.service"]
    assert sorted(r.timer_orphans) == [
        ("tradedesk-explicit.timer", "tradedesk-nowhere.service"),
        ("tradedesk-lonely.timer", "tradedesk-lonely.service"),
    ]
    text = uc.render(r)
    assert "tradedesk-gone.service: script scripts/gone.py -> scripts/gone.py" in text


def test_main_on_repository_writes_out(tmp_path):
    out = tmp_path / "units.txt"
    uc.main(["--root", str(ROOT), "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    assert text.startswith("deploy/user-units:")
