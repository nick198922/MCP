"""The server's Claude login: is it valid, when does it end, and is there a backup token (standard library only).

The server's headless jobs (the Telegram bridge, the agents, the doctor, the audit, channel_watch, trader_review) run
the CLI through scripts/claude_run.sh. The interactive login (`/login`) lasts about 30 days (`loginExpiresAt` in
`claude auth status`); when it ended on 2026-11-11 every job failed with "OAuth session expired" until the owner logged
in again. Two guards:

* a backup token from `claude setup-token` (one year; scripts/server/claude-token.sh saves it to
  ~/.config/tradedesk/claude-token, 0600): claude_run.sh passes it as CLAUDE_CODE_OAUTH_TOKEN only while the login is
  not valid (`login-ok` below), so the plan usage figures, which need the login, keep working whenever they can;
* `check` (user timer tradedesk-claude-auth, twice a day): a Telegram message to the agents topic, at most once a day per
  kind, when the login has ended, ends within LOGIN_WARN_DAYS, or the backup token nears its year.

    python3 scripts/claude_auth.py login-ok   # exit 0 while the login is valid (used by claude_run.sh)
    python3 scripts/claude_auth.py status     # one line for people
    python3 scripts/claude_auth.py check      # the timer: alerts
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path.home()
CLAUDE = os.environ.get("TRADEDESK_CLAUDE_REAL", str(HOME / ".local" / "bin" / "claude"))
TOKEN_FILE = Path(os.environ.get("TRADEDESK_CLAUDE_TOKEN_FILE", HOME / ".config" / "tradedesk" / "claude-token"))
LOG_DIR = Path(os.environ.get("TRADEDESK_AGENTS_LOG_DIR", HOME / ".local" / "state" / "tradedesk-agents"))
STATE = LOG_DIR / "claude-auth.json"
TG_SEND = Path(__file__).resolve().parent / "tg_send.py"

LOGIN_WARN_DAYS = 3  # warn this many days before the login ends
LOGIN_MARGIN = dt.timedelta(minutes=10)  # a login this close to its end counts as ended (a job may run for hours)
TOKEN_DAYS = 365  # `claude setup-token` lifetime
TOKEN_WARN_DAYS = 14
HOW_TO_LOGIN = "зайди по ssh у сесію `tradedesk`, запусти `claude` і набери `/login`"


# ---- pure ---------------------------------------------------------------------------------------------------


def parse_status(text: str) -> tuple[bool, dt.datetime | None]:
    """(logged in, login end in UTC or None) from `claude auth status --json`; unreadable output = not logged in."""
    try:
        data = json.loads(text)
    except ValueError:
        return False, None
    if not isinstance(data, dict):
        return False, None
    end = None
    raw = data.get("loginExpiresAt")
    if isinstance(raw, str):
        try:
            end = dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(dt.UTC)
        except ValueError:
            end = None
    return data.get("loggedIn") is True, end


def login_valid(logged_in: bool, end: dt.datetime | None, now: dt.datetime) -> bool:
    """A login without a known end counts as valid (older CLIs do not print it); one with an end must outlast MARGIN."""
    return logged_in and (end is None or end - now > LOGIN_MARGIN)


def token_end(mtime: float | None) -> dt.datetime | None:
    """When the backup token ends: a year after it was saved (the file's mtime), None without a token."""
    if mtime is None:
        return None
    return dt.datetime.fromtimestamp(mtime, dt.UTC) + dt.timedelta(days=TOKEN_DAYS)


def kyiv(t: dt.datetime) -> str:
    """dd.mm.yyyy HH:MM in Kyiv time (the year matters: the backup token lasts a year)."""
    return t.astimezone(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M")


def alerts(logged_in: bool, end: dt.datetime | None, tok_end: dt.datetime | None, now: dt.datetime) -> list[tuple[str, str]]:
    """[(kind, message)] for the owner; empty when all is well."""
    out: list[tuple[str, str]] = []
    token_ok = tok_end is not None and tok_end > now
    if not login_valid(logged_in, end, now):
        if token_ok:
            tail = "Бот у ТГ і агенти працюють на запасному токені, але агенти не бачать ліміту Claude (гальмо 97 % не діє)."
        else:
            tail = "Запасного токена немає: бот у ТГ, агенти й лікар не працюють."
        out.append(("login-ended", f"🔑 **Логін Claude на сервері сплив.** {tail}\nЩо зробити: {HOW_TO_LOGIN}."))
    elif end is not None and end - now <= dt.timedelta(days=LOGIN_WARN_DAYS):
        days = max(0, (end - now).days)
        out.append(
            (
                "login-soon",
                f"🔑 **Логін Claude на сервері спливає {kyiv(end)}** (за Києвом, через {days} дн.). Коли зручно: {HOW_TO_LOGIN}.",
            )
        )
    if tok_end is not None and tok_end - now <= dt.timedelta(days=TOKEN_WARN_DAYS):
        word = "сплив" if tok_end <= now else f"спливає {kyiv(tok_end)}"
        out.append(
            (
                "token-soon",
                f"🔑 **Запасний токен Claude на сервері {word}.** Новий: `bash scripts/server/claude-token.sh` у ssh-сесії `tradedesk`.",
            )
        )
    return out


def due(kind: str, sent: dict[str, str], today: str) -> bool:
    """At most one message of a kind per day."""
    return sent.get(kind) != today


def status_line(logged_in: bool, end: dt.datetime | None, tok_end: dt.datetime | None, now: dt.datetime) -> str:
    if login_valid(logged_in, end, now):
        login = f"логін діє до {kyiv(end)}" if end else "логін діє"
    else:
        login = "логін сплив"
    if tok_end is None:
        token = "запасного токена немає"
    elif tok_end > now:
        token = f"запасний токен до {kyiv(tok_end)}"
    else:
        token = "запасний токен сплив"
    return f"Claude на сервері: {login}; {token}."


# ---- I/O ----------------------------------------------------------------------------------------------------


def read_login() -> tuple[bool, dt.datetime | None]:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}  # the login itself, not the token
    try:
        run = subprocess.run([CLAUDE, "auth", "status", "--json"], capture_output=True, text=True, timeout=30, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    return parse_status(run.stdout)


def read_token_end() -> dt.datetime | None:
    try:
        if TOKEN_FILE.stat().st_size == 0:
            return None
        return token_end(TOKEN_FILE.stat().st_mtime)
    except OSError:
        return None


def load_sent() -> dict[str, str]:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def send(text: str) -> bool:
    try:
        run = subprocess.run([sys.executable, "-I", str(TG_SEND), "--topic", "agents"], input=text, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return run.returncode == 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    now = dt.datetime.now(dt.UTC)
    logged_in, end = read_login()
    if cmd == "login-ok":
        return 0 if login_valid(logged_in, end, now) else 1
    tok_end = read_token_end()
    if cmd == "status":
        print(status_line(logged_in, end, tok_end, now))
        return 0
    if cmd == "check":
        today = now.date().isoformat()
        sent = load_sent()
        for kind, text in alerts(logged_in, end, tok_end, now):
            if due(kind, sent, today) and send(text):
                sent[kind] = today
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(sent), encoding="utf-8")
        print(status_line(logged_in, end, tok_end, now))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
