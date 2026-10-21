"""scripts/tg_export.py on a small synthetic Telegram Desktop HTML export (skipped without beautifulsoup4)."""

import sys
import unicodedata
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

pytest.importorskip("bs4")
import tg_export  # noqa: E402

MSG = """
<div class="message service" id="message1"><div class="body details">Channel created</div></div>
<div class="message default clearfix" id="message2">
 <div class="body">
  <div class="pull_right date details" title="22.01.2021 14:29:43 UTC+02:00">14:29</div>
  <div class="media_wrap clearfix">
   <a class="photo_wrap clearfix pull_left" href="photos/photo_1@22-01-2021_14-29-43.jpg"><img class="photo"/></a>
  </div>
  <div class="text"><strong>BTC/USDT<br></strong>Stop-limit 34224<br>
   <a href="" onclick="return ShowHashtag(&quot;crypto&quot;)">#crypto</a>
   <a href="https://example.com/x">link</a></div>
 </div>
</div>
<div class="message default clearfix joined" id="message5">
 <div class="body">
  <div class="pull_right date details" title="14.06.2024 11:15:08 UTC+03:00">11:15</div>
  <div class="reply_to details">In reply to <a href="#go_to_message2" onclick="return GoToMessage(2)">this message</a></div>
  <div class="media_wrap clearfix">
   <a class="video_file_wrap clearfix pull_left" href="video_files/240612_Обратный бычий спред.mp4">
    <div class="video_duration">11:00</div></a>
  </div>
 </div>
</div>
<div class="message default clearfix" id="message6">
 <div class="body">
  <div class="pull_right date details" title="15.06.2024 09:00:00 UTC+03:00">09:00</div>
  <div class="media_wrap clearfix">
   <a class="media clearfix pull_left block_link media_file" href="files/missing.pdf"><div class="title bold">missing.pdf</div></a>
  </div>
 </div>
</div>
"""


def export(tmp_path: Path) -> Path:
    root = tmp_path / "ChatExport_2026-10-21"
    (root / "photos").mkdir(parents=True)
    (root / "video_files").mkdir()
    (root / "photos" / "photo_1@22-01-2021_14-29-43.jpg").write_bytes(b"x")
    # the Mac writes the name decomposed (NFD); the HTML has it composed
    (root / "video_files" / unicodedata.normalize("NFD", "240612_Обратный бычий спред.mp4")).write_bytes(b"v")
    (root / "messages.html").write_text(f"<html><body>{MSG}</body></html>", encoding="utf-8")
    (root / "messages2.html").write_text("<html><body></body></html>", encoding="utf-8")
    return root


def test_posts_text_dates_replies_and_media(tmp_path):
    posts = tg_export.parse(export(tmp_path))
    assert [p["id"] for p in posts] == [2, 5, 6]
    first, video, missing = posts
    assert first["date"] == "2021-01-22T14:29:43+02:00"
    assert first["text"].splitlines()[:2] == ["BTC/USDT", "Stop-limit 34224"]
    assert "link (https://example.com/x)" in first["text"] and first["hashtags"] == ["crypto"]
    assert first["media"] == [{"kind": "photo", "path": "photos/photo_1@22-01-2021_14-29-43.jpg", "present": True}]
    assert video["reply_to"] == 2 and video["joined"]
    assert video["media"][0] | {} == {
        "kind": "video",
        "path": "video_files/240612_Обратный бычий спред.mp4",
        "duration": "11:00",
        "present": True,
    }
    assert missing["media"][0]["present"] is False


def test_markdown_links_a_finished_transcript(tmp_path):
    root = export(tmp_path)
    out = tmp_path / "out"
    (out / "videos" / "240612_Обратный бычий спред").mkdir(parents=True)
    (out / "videos" / "240612_Обратный бычий спред" / "transcript.md").write_text("t", encoding="utf-8")
    md = tg_export.render(tg_export.parse(root), root, out)
    assert "## #5 · 2024-06-14 11:15 · відповідь на #2" in md
    assert "[транскрипт](videos/240612_Обратный бычий спред/transcript.md)" in md
    assert "`files/missing.pdf` (нема в експорті)" in md


def test_html_files_in_number_order(tmp_path):
    root = export(tmp_path)
    (root / "messages10.html").write_text("", encoding="utf-8")
    assert [p.name for p in tg_export.html_files(root)] == ["messages.html", "messages2.html", "messages10.html"]
