"""history.tsv stays valid UTF-8, and its readers survive a broken byte (10.11: `head -c` cut a Cyrillic letter in
half and every agents status message after it failed)."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NIGHTLY = (ROOT / "scripts" / "nightly_agents.sh").read_text(encoding="utf-8")


def test_summaries_are_cut_on_character_boundaries(tmp_path):
    assert not re.search(r'head -c \d+ "\$SUMMARY"', NIGHTLY)
    (tmp_path / "s.md").write_text("абв", encoding="utf-8")
    fn = next(line for line in NIGHTLY.splitlines() if line.startswith("head_utf8()"))
    out = subprocess.run(["bash", "-c", f'{fn}\nhead_utf8 3 "$1"', "_", str(tmp_path / "s.md")], capture_output=True, check=True)
    assert out.stdout.decode("utf-8") == "а"  # 3 bytes = one letter and half of the next: the half is dropped


def test_history_readers_replace_broken_bytes():
    for name in ("agents_status.py", "dashboard.py"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert 'history.tsv").read_text(encoding="utf-8", errors="replace")' in text, name
