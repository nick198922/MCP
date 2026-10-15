"""scripts/yt_asr.py without network, audio or Whisper: segment conversion and the shared transcript layout."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import yt_asr  # noqa: E402


def test_segments_become_paragraph_snippets():
    segs = [{"start": 0.0, "text": " перший "}, {"start": 7.5, "text": ""}, {"start": 61.2, "text": "другий"}]
    snippets = yt_asr.segments_to_snippets(segs)
    assert snippets == [(0.0, "перший"), (61.2, "другий")]
    yt = yt_asr._sibling("yt_transcript")
    md = yt.render({"title": "T", "author_name": "C"}, "https://www.youtube.com/watch?v=x", "ru, Whisper", True, snippets, 60.0)
    assert "[00:00] перший" in md and "[01:01] другий" in md and "Whisper" in md
