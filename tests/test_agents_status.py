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
