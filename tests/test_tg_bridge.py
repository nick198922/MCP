"""The Telegram bridge to Claude in scripts/tg_bridge.py: who may talk to it and how answers are cut (no network)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tg_bridge as br  # noqa: E402

ENV = {"TELEGRAM_CHAT_ID": "-1001", "TELEGRAM_TOPIC_CHAT": "4", "TELEGRAM_OWNER_ID": "777"}


def msg(**over):
    m = {"chat": {"id": -1001}, "message_thread_id": 4, "from": {"id": 777}, "text": "привіт", "message_id": 1}
    m.update(over)
    return m


def test_only_the_owner_in_the_claude_topic_is_heard():
    assert br.accept(msg(), ENV) == "ok"
    assert br.accept(msg(**{"from": {"id": 5}}), ENV) == "not the owner"
    assert br.accept(msg(chat={"id": -42}), ENV) == "other chat"
    assert br.accept(msg(message_thread_id=2), ENV) == "other topic"
    assert br.accept(msg(forward_origin={"type": "user"}), ENV) == "forwarded"
    assert br.accept(msg(), {**ENV, "TELEGRAM_OWNER_ID": ""}) == "not the owner"


def test_commands():
    assert br.command("/new") == "/new"
    assert br.command("/status@WWHite2_bot") == "/status"
    assert br.command("що з агентами?") is None
    assert br.command("") is None


def test_chunks_split_at_lines_and_keep_everything():
    text = "\n".join(f"рядок {i}" for i in range(500))
    parts = br.chunks(text, limit=300)
    assert all(len(p) <= 300 for p in parts)
    assert "".join(parts) == text
    assert br.chunks("x" * 700, limit=300) == ["x" * 300, "x" * 300, "x" * 100]


def test_parse_result_reads_claude_json():
    out = json.dumps({"type": "result", "result": "Готово", "session_id": "s1", "is_error": False})
    assert br.parse_result("log line\n" + out) == ("Готово", "s1", False)
    assert br.parse_result("not json")[2] is True
