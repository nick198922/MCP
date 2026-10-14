"""Send a message to the owner's Telegram group (standard library only).

The bot token and chat id come from ~/.config/tradedesk/agents.env (0600, written by scripts/server/telegram-setup.sh);
they never appear on a command line, in the environment of other processes or in logs.

    echo "**bold** and `code`" | python3 scripts/tg_send.py [--topic agents] [--silent] [--html] [--button "Label|https://..."]...

Text is light markdown (what the agents write) unless --html is given; it is escaped and turned into Telegram HTML.
If Telegram rejects the markup, the message is sent again as plain text. --topic NAME posts into the group's forum
topic TELEGRAM_TOPIC_<NAME> (scripts/telegram_topics.py creates them); without it, or if the topic is gone, the message
goes to the general chat.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

ENV_FILE = Path.home() / ".config" / "tradedesk" / "agents.env"
TEXT_LIMIT = 3800  # Telegram allows 4096 characters after entity parsing


def load_env(path: Path | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = (path or ENV_FILE).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip("'\"")
    return out


def write_env(env: dict[str, str], path: Path | None = None) -> None:
    """Rewrite the settings file atomically, readable by its owner only."""
    path = path or ENV_FILE
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.writelines(f"{k}={v}\n" for k, v in env.items())
    os.replace(tmp, path)


def md_to_html(text: str) -> str:
    """Escape the text, then render the marks the agents use: # headings, - bullets, **bold**, `code`."""
    lines = []
    for line in text.splitlines():
        esc = html.escape(line, quote=False)
        heading = re.match(r"^#{1,6}\s+(.*)$", esc)
        if heading:
            esc = f"<b>{heading.group(1)}</b>"
        esc = re.sub(r"^(\s*)[-*]\s+", r"\1• ", esc)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
        esc = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", esc)
        lines.append(esc)
    return "\n".join(lines)


def clip(text: str, limit: int = TEXT_LIMIT) -> str:
    """Cut at a line boundary so no HTML tag is split."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[: cut if cut > 0 else limit] + "\n…"


def payload(
    chat_id: str,
    text: str,
    *,
    html_mode: bool = True,
    silent: bool = False,
    buttons: Sequence[tuple[str, str]] = (),
    thread: int | None = None,
) -> dict:
    body: dict = {
        "chat_id": chat_id,
        "text": text,
        "disable_notification": silent,
        "link_preview_options": {"is_disabled": True},
    }
    if thread:
        body["message_thread_id"] = thread
    if html_mode:
        body["parse_mode"] = "HTML"
    good = [(label, url) for label, url in buttons if url.startswith("https://")]
    if good:
        body["reply_markup"] = {"inline_keyboard": [[{"text": label[:60], "url": url}] for label, url in good[:6]]}
    return body


def _post(token: str, body: dict) -> tuple[bool, str, dict]:
    """(ok, error description, response parameters such as migrate_to_chat_id)."""
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return bool(json.load(resp).get("ok")), "", {}
    except urllib.error.HTTPError as e:  # the body names the problem; the URL (with the token) is never printed
        try:
            data = json.load(e)
            return False, str(data.get("description", e.code)), data.get("parameters") or {}
        except (ValueError, OSError):
            return False, f"HTTP {e.code}", {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, type(e).__name__, {}


def topic_thread(env: dict[str, str], topic: str | None) -> int | None:
    value = env.get(f"TELEGRAM_TOPIC_{topic.upper()}", "") if topic else ""
    return int(value) if value.isdigit() else None


def send(
    text_html: str,
    *,
    silent: bool = False,
    buttons: Sequence[tuple[str, str]] = (),
    topic: str | None = None,
    env: dict[str, str] | None = None,
) -> bool:
    from_file = env is None
    env = load_env() if env is None else env
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("telegram is not configured", file=sys.stderr)
        return False
    thread = topic_thread(env, topic)
    ok, err, params = _post(token, payload(chat, clip(text_html), silent=silent, buttons=buttons, thread=thread))
    if not ok and params.get("migrate_to_chat_id"):
        # The group became a supergroup (e.g. Topics switched on): follow it and remember the new id.
        chat = str(params["migrate_to_chat_id"])
        print(f"the group moved to chat id {chat}; settings updated", file=sys.stderr)
        if from_file:
            write_env({**env, "TELEGRAM_CHAT_ID": chat})
        ok, err, params = _post(token, payload(chat, clip(text_html), silent=silent, buttons=buttons, thread=thread))
    if not ok and thread and "thread" in err.lower():
        print(f"topic {topic} is not usable ({err}); sending to the general chat", file=sys.stderr)
        thread = None
        ok, err, params = _post(token, payload(chat, clip(text_html), silent=silent, buttons=buttons))
    if not ok:
        print(f"telegram rejected the HTML message ({err}); sending plain text", file=sys.stderr)
        plain = re.sub(r"<[^>]+>", "", text_html)
        ok, err, params = _post(
            token, payload(chat, clip(html.unescape(plain)), html_mode=False, silent=silent, buttons=buttons, thread=thread)
        )
        if not ok:
            print(f"telegram send failed: {err}", file=sys.stderr)
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--silent", action="store_true", help="deliver without a sound")
    ap.add_argument("--topic", help="forum topic: agents, status or alerts")
    ap.add_argument("--html", action="store_true", help="the text is Telegram HTML already")
    ap.add_argument("--button", action="append", default=[], help='"Label|https://url" (repeatable)')
    args = ap.parse_args(argv)
    text = sys.stdin.read()
    buttons = [tuple(b.split("|", 1)) for b in args.button if "|" in b]
    return 0 if send(text if args.html else md_to_html(text), silent=args.silent, buttons=buttons, topic=args.topic) else 1


if __name__ == "__main__":
    sys.exit(main())
