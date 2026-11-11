"""The server's Claude login guard: scripts/claude_auth.py and the wrapper scripts/claude_run.sh (no network)."""

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import claude_auth as ca  # noqa: E402
import tg_bridge as br  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
NOW = dt.datetime(2026, 11, 11, 9, 0, tzinfo=dt.UTC)
# `claude auth status --json` of 2.1.322 on the server, 2026-11-11 (personal fields dropped)
STATUS = {"loggedIn": True, "authMethod": "claude.ai", "loginExpiresAt": "2026-12-10T22:10:29.205Z"}


def test_parse_status_real_sample():
    logged_in, end = ca.parse_status(json.dumps(STATUS))
    assert logged_in is True
    assert end == dt.datetime(2026, 12, 10, 22, 10, 29, 205000, tzinfo=dt.UTC)


def test_parse_status_garbage_is_not_logged_in():
    assert ca.parse_status("") == (False, None)
    assert ca.parse_status("[1]") == (False, None)
    assert ca.parse_status('{"loggedIn": true, "loginExpiresAt": "soon"}') == (True, None)


def test_login_valid_needs_a_margin():
    assert ca.login_valid(True, NOW + dt.timedelta(days=1), NOW)
    assert ca.login_valid(True, None, NOW)  # older CLI without the field
    assert not ca.login_valid(True, NOW + dt.timedelta(minutes=5), NOW)
    assert not ca.login_valid(True, NOW - dt.timedelta(days=1), NOW)
    assert not ca.login_valid(False, NOW + dt.timedelta(days=9), NOW)


def test_token_end_is_a_year_after_saving():
    assert ca.token_end(None) is None
    assert ca.token_end(NOW.timestamp()) == NOW + dt.timedelta(days=365)


def test_kyiv_time():
    assert ca.kyiv(dt.datetime(2026, 12, 10, 22, 10, tzinfo=dt.UTC)) == "11.12 00:10"  # EET, +2
    assert ca.kyiv(dt.datetime(2026, 7, 1, 9, 0, tzinfo=dt.UTC)) == "01.07 12:00"  # EEST, +3


def test_no_alerts_while_all_is_well():
    assert ca.alerts(True, NOW + dt.timedelta(days=20), NOW + dt.timedelta(days=200), NOW) == []
    assert ca.alerts(True, NOW + dt.timedelta(days=20), None, NOW) == []


def test_login_soon_and_ended():
    soon = ca.alerts(True, NOW + dt.timedelta(days=2, hours=3), None, NOW)
    assert [k for k, _ in soon] == ["login-soon"]
    assert "через 2 дн." in soon[0][1] and "/login" in soon[0][1]

    ended = ca.alerts(True, NOW - dt.timedelta(hours=1), None, NOW)
    assert [k for k, _ in ended] == ["login-ended"]
    assert "Запасного токена немає" in ended[0][1]

    backed = dict(ca.alerts(False, None, NOW + dt.timedelta(days=100), NOW))
    assert "запасному токені" in backed["login-ended"] and "97 %" in backed["login-ended"]


def test_token_nearing_its_year():
    kinds = dict(ca.alerts(True, NOW + dt.timedelta(days=20), NOW + dt.timedelta(days=10), NOW))
    assert "спливає" in kinds["token-soon"] and "claude-token.sh" in kinds["token-soon"]
    gone = dict(ca.alerts(True, NOW + dt.timedelta(days=20), NOW - dt.timedelta(days=1), NOW))
    assert "сплив." in gone["token-soon"]


def test_one_message_per_kind_per_day():
    assert ca.due("login-soon", {}, "2026-11-11")
    assert not ca.due("login-soon", {"login-soon": "2026-11-11"}, "2026-11-11")
    assert ca.due("login-soon", {"login-soon": "2026-11-10"}, "2026-11-11")


def test_status_line():
    line = ca.status_line(True, dt.datetime(2026, 12, 10, 22, 10, tzinfo=dt.UTC), None, NOW)
    assert line == "Claude на сервері: логін діє до 11.12 00:10; запасного токена немає."
    assert "логін сплив; запасний токен до" in ca.status_line(False, None, NOW + dt.timedelta(days=9), NOW)


def test_bridge_turns_a_lost_login_into_instructions():
    text = br.error_answer(1, "Failed to authenticate: OAuth session expired and could not be refreshed")
    assert text.startswith("🔑") and "/login" in text and "claude-token.sh" in text
    other = br.error_answer(1, "API Error: 529 overloaded")
    assert other.startswith("❌ Claude завершився з помилкою (код 1).") and "529" in other


# ---- the wrapper ---------------------------------------------------------------------------------------------


def fake_claude(tmp_path: Path, status: dict) -> Path:
    """A `claude` that answers `auth status` with the given JSON and otherwise prints the token it was given."""
    exe = tmp_path / "claude"
    exe.write_text(
        "#!/bin/bash\n"
        f"if [[ \"$1 $2\" == 'auth status' ]]; then echo '{json.dumps(status)}'; exit 0; fi\n"
        'echo "token=${CLAUDE_CODE_OAUTH_TOKEN:-none} args=$*"\n'
    )
    exe.chmod(0o755)
    return exe


def run_wrapper(tmp_path: Path, status: dict, token: str | None) -> str:
    tok = tmp_path / "claude-token"
    if token is not None:
        tok.write_text(token + "\n")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
    env |= {"TRADEDESK_CLAUDE_REAL": str(fake_claude(tmp_path, status)), "TRADEDESK_CLAUDE_TOKEN_FILE": str(tok)}
    run = subprocess.run(["bash", str(SCRIPTS / "claude_run.sh"), "-p", "hi there"], capture_output=True, text=True, env=env, timeout=60)
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


def test_wrapper_uses_the_login_while_it_is_valid(tmp_path):
    later = (dt.datetime.now(dt.UTC) + dt.timedelta(days=9)).isoformat()
    out = run_wrapper(tmp_path, {"loggedIn": True, "loginExpiresAt": later}, "sk-ant-oat01-backup")
    assert out == "token=none args=-p hi there"


def test_wrapper_falls_back_to_the_token_when_the_login_ended(tmp_path):
    earlier = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
    out = run_wrapper(tmp_path, {"loggedIn": True, "loginExpiresAt": earlier}, "sk-ant-oat01-backup")
    assert out == "token=sk-ant-oat01-backup args=-p hi there"


def test_wrapper_without_a_token_runs_the_cli_as_it_is(tmp_path):
    out = run_wrapper(tmp_path, {"loggedIn": False}, None)
    assert out == "token=none args=-p hi there"
