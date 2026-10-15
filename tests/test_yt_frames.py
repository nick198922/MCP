"""Frame selection and the index of scripts/yt_frames.py (no network, no yt-dlp, ffmpeg or Pillow)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import yt_frames as yf  # noqa: E402


def test_names_and_stamps():
    assert yf.frame_name(590) == "t00950.jpg" and yf.frame_name(3725) == "t06205.jpg"
    assert yf.stamp(590) == "09:50" and yf.stamp(3725) == "1:02:05"


def test_select_keeps_changes_and_limits_evenly():
    diffs = [255, 1, 9, 2, 30, 7, 0.5, 12]
    assert yf.select(diffs, 6.0, 40) == [0, 2, 4, 5, 7]
    assert yf.select(diffs, 6.0, 3) == [0, 4, 7]  # first, middle, last of the kept ones
    assert yf.select([255.0], 6.0, 1) == [0]


def test_transcript_paragraph_lookup_and_index():
    text = "# T\n\n## Транскрипт\n\n[00:00] intro\n\n[01:01] weekly support\n\n[1:02:03] late\n"
    paras = yf.transcript_paragraphs(text)
    assert paras == [(0, "intro"), (61, "weekly support"), (3723, "late")]
    assert yf.paragraph_at(paras, 59) == "intro" and yf.paragraph_at(paras, 70) == "weekly support"
    md = yf.render_index("Title", "https://youtu.be/x", [(70.0, "t00110.jpg")], paras, ["sheet-1.jpg"])
    assert "## 01:10 — [t00110.jpg](t00110.jpg)" in md and "> weekly support" in md and "[sheet-1.jpg](sheet-1.jpg)" in md


def test_moments_and_bursts():
    assert yf.parse_times("4:05, 0:30,4:05,1:02:03,45") == [30.0, 45.0, 245.0, 3723.0]
    assert yf.burst_times([0.0, 245.0], [0.0, 4.0, -2.0]) == [[0.0, 4.0, 0.0], [245.0, 249.0, 243.0]]
    try:
        yf.parse_times("4m05")
        raise AssertionError("bad time accepted")
    except ValueError:
        pass
    assert yf.video_id("https://youtu.be/8YKAUGz0HnM?si=x") == "8YKAUGz0HnM" and yf.video_id("nope") is None
