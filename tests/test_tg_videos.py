"""Pure parts of scripts/tg_videos.py: ffmpeg duration parsing and the order of an export's videos."""

import os
import sys
import unicodedata
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tg_videos  # noqa: E402


def export(tmp_path: Path) -> Path:
    root = tmp_path / "ChatExport_2026-10-21"
    (root / "video_files").mkdir(parents=True)
    (root / "files").mkdir()
    (root / "video_files" / unicodedata.normalize("NFD", "240612_Обратный бычий спред.mp4")).write_bytes(b"v")
    return root


def test_duration_and_broken_video():
    assert tg_videos.parse_duration("  Duration: 00:05:54.67, start: 0.000000, bitrate: 1201 kb/s") == pytest.approx(354.67)
    assert tg_videos.parse_duration("[mov,mp4] moov atom not found\n x.mp4: Invalid data found") is None
    assert tg_videos.parse_duration("no duration line") is None


def test_videos_newest_first(tmp_path):
    root = export(tmp_path)
    old = root / "video_files" / "230322_Fed.mp4"
    old.write_bytes(b"v")
    (root / "files" / "clip.MOV").write_bytes(b"v")
    (root / "files" / "doc.pdf").write_bytes(b"p")
    (root / "video_files" / "230322_Fed.mp4_thumb.jpg").write_bytes(b"t")
    os.utime(old, (1_000_000, 1_000_000))
    names = [unicodedata.normalize("NFC", p.name) for p in tg_videos.videos(root)]
    assert set(names) == {"240612_Обратный бычий спред.mp4", "clip.MOV", "230322_Fed.mp4"}
    assert names[-1] == "230322_Fed.mp4"
