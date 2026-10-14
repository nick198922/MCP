"""Split the owner's Telegram group into topics: agents, the 2-hourly status and market alerts.

Runs on the server as tradedesk (scripts/server/telegram-topics.sh does it from the Mac). Needs Topics switched on in
the group, which turns it into a supergroup with a new id, and the bot an admin allowed to manage topics. Updates
~/.config/tradedesk/agents.env (0600): the new chat id and TELEGRAM_TOPIC_AGENTS / _STATUS / _ALERTS. Safe to run
again: topics already recorded are kept. The token is never printed.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tg_send import ENV_FILE, load_env  # noqa: E402

# key, topic name, icon colour (one of the six Telegram allows), first message in the topic
TOPICS = [
    ("AGENTS", "🤖 Агенти", 0x6FB9F0, "Тут агенти й лікар пишуть про кожен крок: здано, продовжують, збій, пауза."),
    ("STATUS", "📊 Статус", 0x8EEE98, "Тут кожні 2 години (крім ночі) тихий статус проєкту."),
    ("ALERTS", "📈 Алерти ринку", 0xFB6F5F, "Тут ринкові алерти MCP-сервера (правила додаються інструментами rules_*)."),
]


def api(token: str, method: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except (ValueError, OSError):
            return {"ok": False, "description": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "description": type(e).__name__}


def write_env(env: dict[str, str], path: Path | None = None) -> None:
    path = path or ENV_FILE
    first = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"] + [f"TELEGRAM_TOPIC_{k}" for k, *_ in TOPICS]
    keys = [k for k in first if k in env] + [k for k in env if k not in first]
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.writelines(f"{k}={env[k]}\n" for k in keys)
    os.replace(tmp, path)


def main() -> int:
    env = load_env(ENV_FILE)
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("no bot token or chat id: run scripts/server/telegram-setup.sh first")
        return 1
    info = api(token, "getChat", {"chat_id": chat})
    moved = (info.get("parameters") or {}).get("migrate_to_chat_id")
    if not info.get("ok") and moved:
        print(f"the group became a supergroup: chat id {chat} -> {moved}")
        chat = str(moved)
        for key, *_ in TOPICS:  # topics of the old chat cannot exist
            env.pop(f"TELEGRAM_TOPIC_{key}", None)
        info = api(token, "getChat", {"chat_id": chat})
    if not info.get("ok"):
        print(f"Telegram cannot see the group: {info.get('description')}")
        return 1
    if not info["result"].get("is_forum"):
        print("Topics are off in this group. In Telegram: the group -> Edit -> Topics -> on; then run this again.")
        return 1
    env["TELEGRAM_CHAT_ID"] = chat
    write_env(env)
    for key, name, color, hello in TOPICS:
        if env.get(f"TELEGRAM_TOPIC_{key}", "").isdigit():
            print(f"topic {name}: already there")
            continue
        made = api(token, "createForumTopic", {"chat_id": chat, "name": name, "icon_color": color})
        if not made.get("ok"):
            print(f"cannot create topic {name}: {made.get('description')}")
            print("Make the bot an admin of the group with the right to manage topics, then run this again.")
            return 1
        thread = made["result"]["message_thread_id"]
        env[f"TELEGRAM_TOPIC_{key}"] = str(thread)
        write_env(env)
        api(token, "sendMessage", {"chat_id": chat, "message_thread_id": thread, "text": hello})
        print(f"topic {name}: created")
    print("done: agents, status and alerts go to their topics")
    return 0


if __name__ == "__main__":
    sys.exit(main())
