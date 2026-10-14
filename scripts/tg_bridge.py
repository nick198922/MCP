"""Talk to Claude on the server from the owner's Telegram topic "💬 Claude".

A long-polling loop (systemd service tradedesk-tgbridge, user tradedesk). It takes only text messages written by the
owner (TELEGRAM_OWNER_ID: the creator of the group) in the group TELEGRAM_CHAT_ID, topic TELEGRAM_TOPIC_CHAT; anything
else is ignored and logged. Each message goes to `claude -p` in the chat's own clone (~/tradedesk-chat, with the
owner's server permissions from its .claude/settings.local.json), continuing one conversation; the answer is sent back
to the topic. Commands: /new (a fresh conversation), /status (the project status), /help.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tg_send import clip, load_env, md_to_html  # noqa: E402

HOME = Path.home()
CHAT_DIR = Path(os.environ.get("TRADEDESK_CHAT_CLONE", HOME / "tradedesk-chat"))
LOG_DIR = Path(os.environ.get("TRADEDESK_AGENTS_LOG_DIR", HOME / ".local" / "state" / "tradedesk-agents"))
STATE = LOG_DIR / "tg-bridge.json"
LOG = LOG_DIR / "tg-bridge.log"
CLAUDE = os.environ.get("TRADEDESK_CLAUDE_BIN", str(HOME / ".local" / "bin" / "claude"))
MAX_SECONDS = int(os.environ.get("TRADEDESK_CHAT_MAX_SECONDS", "1800"))
HELP = (
    "Пишіть сюди, як у розмові з Claude: питання про проєкт, рішення для агентів, доручення.\n"
    "• /new — почати нову розмову\n• /status — статус проєкту\n• /help — ця підказка\n"
    "Зміни приходять PR-гілками chat/…; зливаєте ви."
)


# ---- pure parts (tested) ----------------------------------------------------------------------------------


def accept(msg: dict, env: dict[str, str]) -> str:
    """'ok' for a message to act on, otherwise why it is ignored."""
    if str((msg.get("chat") or {}).get("id")) != env.get("TELEGRAM_CHAT_ID"):
        return "other chat"
    topic = env.get("TELEGRAM_TOPIC_CHAT", "")
    if topic and str(msg.get("message_thread_id", "")) != topic:
        return "other topic"
    owner = env.get("TELEGRAM_OWNER_ID", "")
    if not owner or str((msg.get("from") or {}).get("id")) != owner:
        return "not the owner"
    if msg.get("forward_origin") or msg.get("forward_from"):
        return "forwarded"
    return "ok"


def command(text: str) -> str | None:
    word = text.strip().split()[0].lower() if text.strip() else ""
    word = word.split("@", 1)[0]
    return word if word in ("/new", "/status", "/help", "/start") else None


def chunks(text: str, limit: int = 3800) -> list[str]:
    """Split a long answer at line boundaries."""
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            out.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        out.append(cur)
    return out or ["(порожня відповідь)"]


def parse_result(stdout: str) -> tuple[str, str | None, bool]:
    """(answer, session id, is_error) from `claude -p --output-format json`."""
    try:
        data = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return stdout.strip()[-3000:] or "(немає відповіді)", None, True
    return str(data.get("result") or ""), data.get("session_id"), bool(data.get("is_error"))


# ---- I/O ----------------------------------------------------------------------------------------------------


def log(line: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%F %T} {line}\n")


def api(token: str, method: str, body: dict, timeout: float = 70) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except (ValueError, OSError):
            return {"ok": False, "description": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "description": type(e).__name__}


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))


def reply(env: dict[str, str], text: str, *, mode: str = "md", to: int | None = None) -> None:
    """mode: "md" (light markdown from Claude), "html" (already Telegram HTML) or "plain"."""
    token, chat = env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"]
    thread = env.get("TELEGRAM_TOPIC_CHAT", "")
    for i, part in enumerate(chunks(text)):
        shown = clip(md_to_html(part)) if mode == "md" else part
        body = {"chat_id": chat, "text": shown, "link_preview_options": {"is_disabled": True}}
        if mode != "plain":
            body["parse_mode"] = "HTML"
        if thread.isdigit():
            body["message_thread_id"] = int(thread)
        if to and i == 0:
            body["reply_parameters"] = {"message_id": to, "allow_sending_without_reply": True}
        r = api(token, "sendMessage", body)
        if not r.get("ok") and mode != "plain":  # bad markup: send the same part as plain text
            body.pop("parse_mode", None)
            body["text"] = part
            api(token, "sendMessage", body)


def ask_claude(env: dict[str, str], state: dict, text: str) -> str:
    token, chat = env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"]
    thread = env.get("TELEGRAM_TOPIC_CHAT", "")
    done = threading.Event()

    def typing() -> None:  # Telegram shows "typing…" for 5 s per call
        while not done.is_set():
            body = {"chat_id": chat, "action": "typing"}
            if thread.isdigit():
                body["message_thread_id"] = int(thread)
            api(token, "sendChatAction", body, timeout=10)
            done.wait(4.5)

    threading.Thread(target=typing, daemon=True).start()
    prompt_file = Path(__file__).resolve().parent.parent / ".claude" / "telegram.md"  # from the main clone, kept current
    cmd = [CLAUDE, "-p", text, "--output-format", "json", "--permission-mode", "acceptEdits"]
    if prompt_file.exists():
        cmd += ["--append-system-prompt", prompt_file.read_text(encoding="utf-8")]
    if state.get("session"):
        cmd += ["--resume", state["session"]]
    else:
        state["session"] = str(uuid.uuid4())
        cmd += ["--session-id", state["session"]]
    env_run = {**os.environ, "PATH": f"{HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"}
    try:
        run = subprocess.run(cmd, cwd=CHAT_DIR, capture_output=True, text=True, timeout=MAX_SECONDS, env=env_run)
        answer, session, is_error = parse_result(run.stdout)
        if session:
            state["session"] = session
        if run.returncode != 0 or is_error:
            answer = f"❌ Claude завершився з помилкою (код {run.returncode}).\n{answer or run.stderr[-1500:]}"
    except subprocess.TimeoutExpired:
        answer = f"⌛ Не встиг за {MAX_SECONDS // 60} хв. Робота в клоні лишилась; напишіть «продовжуй», щоб довести її."
    finally:
        done.set()
    save_state(state)
    return answer


def handle(env: dict[str, str], state: dict, msg: dict) -> None:
    verdict = accept(msg, env)
    sender = (msg.get("from") or {}).get("id")
    if verdict != "ok":
        if verdict != "other topic":
            log(f"ignored ({verdict}) from {sender}")
        return
    text = msg.get("text") or ""
    if not text:
        reply(env, "Поки що розумію лише текст.", to=msg.get("message_id"))
        return
    cmd = command(text)
    log(f"owner: {text[:200]!r}")
    if cmd in ("/help", "/start"):
        reply(env, HELP)
    elif cmd == "/new":
        state.pop("session", None)
        save_state(state)
        reply(env, "🆕 Нова розмова. Пишіть.")
    elif cmd == "/status":
        status = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("agents_status.py")), "--print"], capture_output=True, text=True, timeout=120
        )
        reply(env, status.stdout or "статус недоступний", mode="html")
    else:
        answer = ask_claude(env, state, text)
        log(f"claude: {answer[:200]!r}")
        reply(env, answer, to=msg.get("message_id"))


def main() -> int:
    state = load_state()
    log("bridge started")
    while True:
        env = load_env()
        if not env.get("TELEGRAM_BOT_TOKEN") or not env.get("TELEGRAM_OWNER_ID"):
            log("no bot token or owner id yet; waiting")
            time.sleep(60)
            continue
        got = api(
            env["TELEGRAM_BOT_TOKEN"], "getUpdates", {"offset": state.get("offset", 0), "timeout": 50, "allowed_updates": ["message"]}
        )
        if not got.get("ok"):
            log(f"getUpdates failed: {got.get('description')}")
            time.sleep(15)
            continue
        for update in got.get("result") or []:
            state["offset"] = update["update_id"] + 1
            save_state(state)
            if update.get("message"):
                try:
                    handle(env, state, update["message"])
                except Exception as e:  # noqa: BLE001 - one bad message must not stop the bridge
                    log(f"error: {type(e).__name__}: {e}")
                    reply(env, f"❌ Помилка містка: {type(e).__name__}")


if __name__ == "__main__":
    sys.exit(main())
