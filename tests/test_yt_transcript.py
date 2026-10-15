"""YouTube links and transcript layout in scripts/yt_transcript.py (no network, no youtube-transcript-api)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import yt_transcript as yt  # noqa: E402


def test_video_id_from_every_link_form():
    vid = "kdEygb8UPKU"
    for url in (
        f"https://www.youtube.com/watch?v={vid}",
        f"https://youtube.com/watch?feature=share&v={vid}&t=42",
        f"https://youtu.be/{vid}?si=abc",
        f"https://www.youtube.com/shorts/{vid}",
        f"https://www.youtube.com/live/{vid}?si=x",
        f"https://www.youtube.com/embed/{vid}",
        f"https://m.youtube.com/watch?v={vid}",
        vid,
    ):
        assert yt.video_id(url) == vid, url
    assert yt.video_id("https://example.com/watch") is None


def test_paragraphs_start_with_their_time():
    snippets = [(0.0, "Привіт"), (30.5, "сьогодні\nпро байбеки"), (61.0, "перший"), (95.0, " "), (3725.0, "кінець")]
    assert yt.paragraphs(snippets) == ["[00:00] Привіт сьогодні про байбеки", "[01:01] перший", "[1:02:05] кінець"]
    assert yt.paragraphs([]) == []


def test_render_names_the_video_and_kind_of_transcript():
    text = yt.render({"title": "Байбеки", "author_name": "Канал"}, "https://www.youtube.com/watch?v=x", "ru", True, [(0, "текст")])
    assert text.startswith("# Байбеки\n") and "Канал: Канал" in text and "ru, автоматичний" in text and "[00:00] текст" in text


def test_main_rejects_a_non_youtube_link(capsys):
    assert yt.main(["https://example.com"]) == 1
    assert "not a YouTube link" in capsys.readouterr().err
