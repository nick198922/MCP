"""Split the owner's Telegram group into topics: agents, the 2-hourly status and market alerts.

Runs on the server as tradedesk (scripts/server/telegram-topics.sh does it from the Mac). Needs Topics switched on in
the group, which turns it into a supergroup with a new id, and the bot an admin allowed to manage topics. Updates
~/.config/tradedesk/agents.env (0600): the new chat id and TELEGRAM_TOPIC_AGENTS / _STATUS / _ALERTS / _DEMO. Safe to run
again: topics already recorded are kept. The token is never printed.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tg_send import ENV_FILE, load_env  # noqa: E402
from tg_send import write_env as _write_env  # noqa: E402

# key, topic name, icon colour (one of the six Telegram allows), first message in the topic
TOPICS = [
    ("AGENTS", "🤖 Агенти", 0x6FB9F0, "Тут агенти й лікар пишуть про кожен крок: здано, продовжують, збій, пауза."),
    ("STATUS", "📊 Статус", 0x8EEE98, "Тут кожні 2 години (крім ночі) тихий статус проєкту."),
    ("ALERTS", "📈 Алерти ринку", 0xFB6F5F, "Тут ринкові алерти MCP-сервера (правила додаються інструментами rules_*)."),
    ("CHAT", "💬 Claude", 0xFFD67E, "Пишіть сюди: відповідає Claude на сервері. /new — нова розмова, /status — статус, /help — підказка."),
    ("DEMO", "🧪 Демо-рахунок", 0xCB86DB, "Тут демо-трейдер Bybit пише кожен вхід (сетап, індикатори, рівні) і кожне закриття з P&L у R."),
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
    first = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_OWNER_ID"] + [f"TELEGRAM_TOPIC_{k}" for k, *_ in TOPICS]
    ordered = {k: env[k] for k in first if k in env} | {k: v for k, v in env.items() if k not in first}
    _write_env(ordered, path or ENV_FILE)


def migrated_to(token: str, chat: str) -> str | None:
    """The supergroup id if the group moved. getChat may still answer for the old group, so ask with a harmless
    chat action, which Telegram refuses with the new id; recent updates are a second source."""
    tried = api(token, "sendChatAction", {"chat_id": chat, "action": "typing"})
    new = (tried.get("parameters") or {}).get("migrate_to_chat_id")
    if new:
        return str(new)
    updates = api(token, "getUpdates", {}).get("result")
    for update in updates if isinstance(updates, list) else []:
        msg = (update.get("message") if isinstance(update, dict) else None) or {}
        if str((msg.get("chat") or {}).get("id")) == chat and msg.get("migrate_to_chat_id"):
            return str(msg["migrate_to_chat_id"])
    return None


def main() -> int:
    env = load_env(ENV_FILE)
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("no bot token or chat id: run scripts/server/telegram-setup.sh first")
        return 1
    moved = migrated_to(token, chat)
    if moved and moved != chat:
        print(f"the group became a supergroup: chat id {chat} -> {moved}")
        chat = moved
        for key, *_ in TOPICS:  # topics of the old chat cannot exist
            env.pop(f"TELEGRAM_TOPIC_{key}", None)
    info = api(token, "getChat", {"chat_id": chat})
    if not info.get("ok"):
        print(f"Telegram cannot see the group: {info.get('description')}")
        return 1
    result = info["result"]
    print(f"group: {result.get('title')} ({result.get('type')}, id {chat}), topics {'on' if result.get('is_forum') else 'off'}")
    if not result.get("is_forum"):
        print("Topics are off in this group. In Telegram: the group -> Edit -> Topics -> on; then run this again.")
        return 1
    env["TELEGRAM_CHAT_ID"] = chat
    # The owner is the group's creator; only their messages reach Claude through the "💬 Claude" topic.
    admins = api(token, "getChatAdministrators", {"chat_id": chat}).get("result")
    creator = next((a["user"]["id"] for a in admins or [] if isinstance(a, dict) and a.get("status") == "creator"), None)
    if creator:
        env["TELEGRAM_OWNER_ID"] = str(creator)
        print(f"owner (group creator): {creator}")
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
    print("done: agents, status, alerts and the Claude chat have their topics")
    return 0


if __name__ == "__main__":
    sys.exit(main())
