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
    assert br.accept(msg(forward_origin={"type": "user"}), ENV) == "ok"  # forwarded posts pass, marked as data
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
