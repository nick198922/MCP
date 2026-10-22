"""The Telegram bridge to Claude in scripts/tg_bridge.py: who may talk to it and how answers are cut (no network)."""

import json
import sys
from pathlib import Path

import pytest

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
    assert br.accept(msg(forward_origin={"type": "user"}), ENV) == "ok"  # forwarded posts pass, marked as data
    assert br.accept(msg(), {**ENV, "TELEGRAM_OWNER_ID": ""}) == "not the owner"


def test_commands():
    assert br.command("/new") == "/new"
    assert br.command("/status@WWHite2_bot") == "/status"
    assert br.command("що з агентами?") is None
    assert br.command("") is None


def test_trade_command_is_one_argument_without_a_shell():
    assert br.command("/trade BTC long 84000 size 0.1") == "/trade"
    assert br.command("/trade@WWHite2_bot BTC long 84000 size 0.1") == "/trade"
    text = "/trade BTC long 84000 size 0.1 thesis $(rm -rf ~); `id` --db /tmp/x"
    argv = br.trade_argv(text)
    assert argv[-1] == f"--text={text}" and argv[-2].endswith("journal_trade_tg.py")
    assert br.TRADE_SCRIPT.exists()


def test_trade_is_recorded_only_for_the_owner_in_the_topic(monkeypatch):
    calls, sent = [], []
    monkeypatch.setattr(br, "record_trade", lambda text: calls.append(text) or "✅ Записано")
    monkeypatch.setattr(br, "reply", lambda env, text, **k: sent.append((text, k)))
    monkeypatch.setattr(br, "log", lambda line: None)
    monkeypatch.setattr(br, "ask_claude", lambda *a: pytest.fail("a /trade must not go to Claude"))
    t = "/trade BTC long 84000 size 0.1"
    br.handle(ENV, {}, [msg(text=t, **{"from": {"id": 5}})])
    br.handle(ENV, {}, [msg(text=t, message_thread_id=2)])
    br.handle(ENV, {}, [msg(text=t, chat={"id": -42})])
    assert calls == [] and sent == []
    br.handle(ENV, {}, [msg(text=t, message_id=9)])
    assert calls == [t] and sent == [("✅ Записано", {"mode": "html", "to": 9})]


def test_a_forwarded_trade_command_is_not_recorded(monkeypatch):
    calls, asked = [], []
    monkeypatch.setattr(br, "record_trade", lambda text: calls.append(text) or "✅ Записано")
    monkeypatch.setattr(br, "reply", lambda env, text, **k: None)
    monkeypatch.setattr(br, "log", lambda line: None)
    monkeypatch.setattr(br, "ask_claude", lambda env, state, prompt: asked.append(prompt) or "ok")
    monkeypatch.setattr(br, "prune_inbox", lambda: None)
    fwd = msg(text="/trade BTC long 84000 size 0.1", forward_origin={"type": "user", "sender_user": {"first_name": "Ann"}})
    br.handle(ENV, {}, [fwd])
    assert calls == [] and len(asked) == 1 and "недовірені дані" in asked[0]  # data for Claude, not a command
    # the owner's /trade followed by a forwarded post stays two requests
    groups = br.group_media([msg(message_id=1, text="/trade BTC long 84000 size 0.1"), {**fwd, "message_id": 2}])
    assert [[m["message_id"] for m in g] for g in groups] == [[1], [2]]


def test_close_command_is_one_argument_and_only_for_the_owner_in_the_topic(monkeypatch):
    assert br.command("/close 12 86000") == "/close" and br.command("/close@WWHite2_bot 12 86000") == "/close"
    text = "/close 12 86000 note $(rm -rf ~); --db /tmp/x"
    argv = br.close_argv(text)
    assert argv[-1] == f"--text={text}" and argv[-2].endswith("journal_close_tg.py") and br.CLOSE_SCRIPT.exists()
    calls, sent = [], []
    monkeypatch.setattr(br, "close_trade", lambda text: calls.append(text) or "✅ Закрито")
    monkeypatch.setattr(br, "record_trade", lambda text: pytest.fail("a /close must not record a trade"))
    monkeypatch.setattr(br, "reply", lambda env, text, **k: sent.append((text, k)))
    monkeypatch.setattr(br, "log", lambda line: None)
    monkeypatch.setattr(br, "ask_claude", lambda *a: pytest.fail("a /close must not go to Claude"))
    t = "/close 12 86000"
    br.handle(ENV, {}, [msg(text=t, **{"from": {"id": 5}})])
    br.handle(ENV, {}, [msg(text=t, message_thread_id=2)])
    assert calls == [] and sent == []
    br.handle(ENV, {}, [msg(text=t, message_id=9)])
    assert calls == [t] and sent == [("✅ Закрито", {"mode": "html", "to": 9})]
    assert "/close" in br.HELP


def test_close_trade_reports_a_failed_script_in_its_own_words(monkeypatch):
    logged = []
    monkeypatch.setattr(br, "log", logged.append)
    monkeypatch.setattr(br, "close_argv", lambda text: [sys.executable, "-c", "import sys; sys.exit(2)"])
    assert br.close_trade("/close 1 2") == "❌ Угоду не закрито: скрипт журналу завершився з кодом 2."
    assert any(line.startswith("close stderr:") for line in logged)
    monkeypatch.setattr(br, "close_argv", lambda text: [sys.executable, "-c", "pass", text])
    assert br.close_trade("/close 1 2 note a\x00b").startswith("❌ Угоду не закрито: у тексті є недопустимий символ")


def test_cancel_command_is_one_argument_and_only_for_the_owner_in_the_topic(monkeypatch):
    assert br.command("/cancel 12") == "/cancel" and br.command("/cancel@WWHite2_bot 12") == "/cancel"
    text = "/cancel 12 note $(rm -rf ~); --db /tmp/x"
    argv = br.cancel_argv(text)
    assert argv[-1] == f"--text={text}" and argv[-2].endswith("journal_cancel_tg.py") and br.CANCEL_SCRIPT.exists()
    calls, sent = [], []
    monkeypatch.setattr(br, "cancel_trade", lambda text: calls.append(text) or "✅ Скасовано")
    monkeypatch.setattr(br, "close_trade", lambda text: pytest.fail("a /cancel must not close a trade"))
    monkeypatch.setattr(br, "record_trade", lambda text: pytest.fail("a /cancel must not record a trade"))
    monkeypatch.setattr(br, "reply", lambda env, text, **k: sent.append((text, k)))
    monkeypatch.setattr(br, "log", lambda line: None)
    monkeypatch.setattr(br, "ask_claude", lambda *a: pytest.fail("a /cancel must not go to Claude"))
    t = "/cancel 12"
    br.handle(ENV, {}, [msg(text=t, **{"from": {"id": 5}})])
    br.handle(ENV, {}, [msg(text=t, message_thread_id=2)])
    br.handle(ENV, {}, [msg(text=t, chat={"id": -42})])
    assert calls == [] and sent == []
    br.handle(ENV, {}, [msg(text=t, message_id=9)])
    assert calls == [t] and sent == [("✅ Скасовано", {"mode": "html", "to": 9})]
    assert "/cancel" in br.HELP


def test_cancel_trade_reports_a_failed_script_in_its_own_words(monkeypatch):
    logged = []
    monkeypatch.setattr(br, "log", logged.append)
    monkeypatch.setattr(br, "cancel_argv", lambda text: [sys.executable, "-c", "import sys; sys.exit(2)"])
    assert br.cancel_trade("/cancel 1") == "❌ Угоду не скасовано: скрипт журналу завершився з кодом 2."
    assert any(line.startswith("cancel stderr:") for line in logged)
    monkeypatch.setattr(br, "cancel_argv", lambda text: [sys.executable, "-c", "pass", text])
    assert br.cancel_trade("/cancel 1 note a\x00b").startswith("❌ Угоду не скасовано: у тексті є недопустимий символ")


def test_record_trade_refuses_a_nul_byte(monkeypatch):
    monkeypatch.setattr(br, "log", lambda line: None)
    monkeypatch.setattr(br, "trade_argv", lambda text: [sys.executable, "-c", "pass", text])
    assert br.record_trade("/trade BTC long 84000 size 0.1 thesis a\x00b").startswith(
        "❌ Угоду не записано: у тексті є недопустимий символ"
    )


def test_record_trade_reports_a_failed_script(monkeypatch):
    logged = []
    monkeypatch.setattr(br, "log", logged.append)
    monkeypatch.setattr(br, "trade_argv", lambda text: [sys.executable, "-c", "import sys; sys.stderr.write('boom-trace'); sys.exit(3)"])
    assert br.record_trade("/trade x") == "❌ Угоду не записано: скрипт журналу завершився з кодом 3."
    assert any(line.startswith("trade stderr:") and "boom-trace" in line for line in logged)
    monkeypatch.setattr(br, "trade_argv", lambda text: ["/nonexistent/uv"])
    assert br.record_trade("/trade x").startswith("❌ Угоду не записано: скрипт журналу не відповів")


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


def test_image_ref_takes_the_largest_photo_or_an_image_file():
    photo = [{"file_id": "s", "width": 90, "height": 60}, {"file_id": "l", "width": 1280, "height": 853}]
    assert br.image_ref(msg(photo=photo)) == ("l", "jpg")
    assert br.image_ref(msg(document={"file_id": "d", "mime_type": "image/png"})) == ("d", "png")
    assert br.image_ref(msg(document={"file_id": "d", "mime_type": "image/svg+xml"})) == ("d", "svg")
    assert br.image_ref(msg(document={"file_id": "d", "mime_type": "application/pdf"})) is None
    assert br.image_ref(msg()) is None


def test_forward_source_names_the_channel_or_the_user():
    assert br.forward_source(msg()) is None
    assert br.forward_source(msg(forward_origin={"type": "channel", "chat": {"title": "Crypto TA"}})) == "Crypto TA"
    assert br.forward_source(msg(forward_origin={"type": "user", "sender_user": {"first_name": "Ann", "last_name": "B"}})) == "Ann B"
    assert br.forward_source(msg(forward_origin={"type": "hidden_user", "sender_user_name": "anon"})) == "anon"


def test_build_prompt_marks_forwarded_posts_and_lists_images():
    p = br.build_prompt("", ["/x/a.jpg", "/x/b.jpg"], "Crypto TA", "Лонг від 100")
    assert p.startswith("Проаналізуй пересланий пост.")
    assert "недовірені дані" in p and "Crypto TA" in p and "Лонг від 100" in p
    assert "- /x/a.jpg" in p and "- /x/b.jpg" in p
    assert br.build_prompt("", ["/x/a.jpg"]).startswith("Проаналізуй зображення.")
    assert br.build_prompt("що з BTC?", []) == "що з BTC?"
    own = br.build_prompt("Варто розглянути?", [], "Crypto TA", "Лонг від 100")
    assert own.startswith("Варто розглянути?") and own.index("Варто") < own.index("Лонг від 100")


def test_group_media_keeps_an_album_together():
    a1, a2 = msg(message_id=1, media_group_id="g"), msg(message_id=2, media_group_id="g")
    t, b1 = msg(message_id=3), msg(message_id=4, media_group_id="h")
    groups = br.group_media([a1, t, a2, b1])
    assert [[m["message_id"] for m in g] for g in groups] == [[1, 2], [3], [4]]


def test_the_comment_and_the_posts_forwarded_after_it_are_one_request():
    fwd = {"forward_origin": {"type": "channel", "chat": {"title": "C"}}}
    comment = msg(message_id=1, text="Проаналізуй")
    p1 = msg(message_id=2, media_group_id="g", caption="пост", photo=[{"file_id": "a"}], **fwd)
    p2 = msg(message_id=3, media_group_id="g", photo=[{"file_id": "b"}], **fwd)
    nxt = msg(message_id=4, text="інше питання")
    groups = br.group_media([comment, p1, p2, nxt])
    assert [[m["message_id"] for m in g] for g in groups] == [[1, 2, 3], [4]]
    assert [[m["message_id"] for m in g] for g in br.group_media([msg(message_id=1, text="/new"), p1])] == [[1], [2]]
