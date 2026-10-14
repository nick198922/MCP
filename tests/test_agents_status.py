"""The Telegram status digest and sender in scripts/ (no network)."""

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agents_status as st  # noqa: E402
import tg_send  # noqa: E402

CARD = """# IDEA-20261013-deribit-put-skew-premium: премія за путовий скью

Статус: testing
<!-- proposed → accepted -->

## Рев'ю критика
**2026-10-13: `approve`.**
**2026-10-14: `request changes`.** Повне рев'ю ...
"""


def test_parse_card_reads_title_status_and_latest_verdict():
    title, status, changes = st.parse_card(CARD)
    assert title == "Премія за путовий скью"
    assert status == "testing"
    assert changes is True
    assert st.parse_card(CARD.replace("`request changes`", "`approve`"))[2] is False


def test_open_questions_only_open_ones():
    text = "# Q\n\n## Q-1: перше\nСтатус: answered\n\n## Q-2: друге\nСтатус: open\nКонтекст: ...\n"
    assert st.open_questions(text) == ["Q-2: друге"]


def test_backlog_next_takes_unchecked_items_in_order():
    text = "- [x] done\n- [ ] **VRP** точніше: `atm` smile\n- [ ] second\n- [ ] third\n- [ ] fourth\n"
    assert st.backlog_next(text) == ["VRP точніше: atm smile", "second", "third"]


def test_history_counts_since():
    lines = ["2026-10-14_100000\tdelivered\tx", "2026-10-14_120000\tidle\ty", "2026-10-14_130000\tdelivered\tz"]
    assert st.history_counts(lines, "2026-10-14_110000") == Counter({"idle": 1, "delivered": 1})


def test_last_agent_call_and_whether_it_finished():
    use = {"type": "tool_use", "name": "Agent", "id": "a1", "input": {"subagent_type": "quant", "description": "Test card"}}
    lines = [json.dumps({"timestamp": "2026-10-14T10:54:00Z", "message": {"content": [use]}})]
    role, desc, _, finished = st.last_agent_call(lines)
    assert (role, desc, finished) == ("квант", "Test card", False)
    lines.append(json.dumps({"message": {"content": [{"type": "tool_result", "tool_use_id": "a1"}]}}))
    assert st.last_agent_call(lines)[3] is True
    assert st.last_agent_call(["not json"]) is None


def test_render_escapes_text_and_adds_pr_buttons():
    text, buttons = st.render(
        {
            "now": datetime(2026, 10, 14, 14, 0),
            "agents_line": "🟢 Працюють <b>",
            "usage": {"five_hour": 27.0, "seven_day": 5.0},
            "cards": [{"title": "A & B", "status": "testing", "changes_requested": True, "branch": "agent/x"}],
            "open_branches": ["agent/2026-10-14-put-skew"],
            "questions": ["Q-1: <що>"],
            "backlog": ["next"],
        }
    )
    assert "&lt;b&gt;" in text and "A &amp; B" in text and "&lt;що&gt;" in text
    assert "▰▰▰▱▱▱▱▱▱▱" in text
    assert "критик просить виправлень" in text
    assert buttons == [("🔀 2026-10-14-put-skew", "https://github.com/nick198922/MCP/compare/main...agent/2026-10-14-put-skew?expand=1")]


def test_md_to_html_escapes_then_formats():
    out = tg_send.md_to_html("# Підсумок\n- **крок** `quant` <x> & y")
    assert out == "<b>Підсумок</b>\n• <b>крок</b> <code>quant</code> &lt;x&gt; &amp; y"


def test_clip_cuts_at_a_line_boundary():
    text = "a" * 10 + "\n" + "<b>" + "b" * 20 + "</b>"
    assert tg_send.clip(text, 15) == "a" * 10 + "\n…"


def test_payload_silent_buttons_and_no_preview():
    body = tg_send.payload("-1", "hi", silent=True, buttons=[("PR", "https://x"), ("bad", "javascript:alert(1)")])
    assert body["disable_notification"] is True and body["parse_mode"] == "HTML"
    assert body["link_preview_options"] == {"is_disabled": True}
    assert body["reply_markup"] == {"inline_keyboard": [[{"text": "PR", "url": "https://x"}]]}


def test_send_without_settings_does_nothing(capsys):
    assert tg_send.send("hi", env={}) is False
    assert "not configured" in capsys.readouterr().err


def test_load_env_parses_key_values(tmp_path):
    f = tmp_path / "agents.env"
    f.write_text("# c\nTELEGRAM_BOT_TOKEN=1:abc\nTELEGRAM_CHAT_ID='-55'\n")
    assert tg_send.load_env(f) == {"TELEGRAM_BOT_TOKEN": "1:abc", "TELEGRAM_CHAT_ID": "-55"}


def test_payload_and_topic_thread():
    env = {"TELEGRAM_TOPIC_STATUS": "42", "TELEGRAM_TOPIC_AGENTS": ""}
    assert tg_send.topic_thread(env, "status") == 42
    assert tg_send.topic_thread(env, "agents") is None
    assert tg_send.topic_thread(env, None) is None
    assert tg_send.payload("-1", "x", thread=42)["message_thread_id"] == 42
    assert "message_thread_id" not in tg_send.payload("-1", "x")


def test_send_falls_back_to_general_chat_when_the_topic_is_gone(monkeypatch):
    calls = []

    def fake_post(token, body):
        calls.append(body.get("message_thread_id"))
        if body.get("message_thread_id"):
            return False, "Bad Request: message thread not found", {}
        return True, "", {}

    monkeypatch.setattr(tg_send, "_post", fake_post)
    env = {"TELEGRAM_BOT_TOKEN": "1:a", "TELEGRAM_CHAT_ID": "-1", "TELEGRAM_TOPIC_AGENTS": "9"}
    assert tg_send.send("hi", topic="agents", env=env) is True
    assert calls == [9, None]


def test_topics_setup_follows_the_supergroup_migration(monkeypatch, tmp_path):
    import telegram_topics as tt

    env_file = tmp_path / "agents.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=1:a\nTELEGRAM_CHAT_ID=-55\n")
    monkeypatch.setattr(tt, "ENV_FILE", env_file)
    made = []

    def fake_api(token, method, body):
        if method == "sendChatAction" and body["chat_id"] == "-55":
            return {"ok": False, "description": "upgraded", "parameters": {"migrate_to_chat_id": -1001}}
        if method == "getChat" and body["chat_id"] == "-55":  # the old group still answers, without topics
            return {"ok": True, "result": {"type": "group", "is_forum": False}}
        if method == "getChat":
            return {"ok": True, "result": {"type": "supergroup", "is_forum": True}}
        if method == "createForumTopic":
            made.append(body["name"])
            return {"ok": True, "result": {"message_thread_id": len(made)}}
        return {"ok": True}

    monkeypatch.setattr(tt, "api", fake_api)
    assert tt.main() == 0
    env = tg_send.load_env(env_file)
    assert env["TELEGRAM_CHAT_ID"] == "-1001"
    assert [env[f"TELEGRAM_TOPIC_{k}"] for k in ("AGENTS", "STATUS", "ALERTS")] == ["1", "2", "3"]
    assert env["TELEGRAM_BOT_TOKEN"] == "1:a"
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert tt.main() == 0 and len(made) == 3  # a second run keeps the topics


def test_topics_setup_stops_when_topics_are_off(monkeypatch, tmp_path, capsys):
    import telegram_topics as tt

    env_file = tmp_path / "agents.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=1:a\nTELEGRAM_CHAT_ID=-1001\n")
    monkeypatch.setattr(tt, "ENV_FILE", env_file)
    monkeypatch.setattr(tt, "api", lambda token, method, body: {"ok": True, "result": {"is_forum": False}})
    assert tt.main() == 1
    assert "Topics are off" in capsys.readouterr().out


def test_send_follows_the_group_to_its_supergroup(monkeypatch, tmp_path):
    env_file = tmp_path / "agents.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=1:a\nTELEGRAM_CHAT_ID=-55\n")
    monkeypatch.setattr(tg_send, "ENV_FILE", env_file)
    chats = []

    def fake_post(token, body):
        chats.append(body["chat_id"])
        if body["chat_id"] == "-55":
            return False, "Bad Request: group chat was upgraded to a supergroup chat", {"migrate_to_chat_id": -1001}
        return True, "", {}

    monkeypatch.setattr(tg_send, "_post", fake_post)
    assert tg_send.send("hi") is True
    assert chats == ["-55", "-1001"]
    assert tg_send.load_env(env_file) == {"TELEGRAM_BOT_TOKEN": "1:a", "TELEGRAM_CHAT_ID": "-1001"}
    assert env_file.stat().st_mode & 0o777 == 0o600
