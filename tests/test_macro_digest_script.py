from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import macro_digest as md  # noqa: E402

NOW = datetime(2026, 10, 21, 11, 45, tzinfo=UTC)  # CPI at 12:30 UTC
ROWS = [
    {
        "title": "CPI m/m",
        "country": "USD",
        "date": "2026-10-21T08:30:00-04:00",
        "impact": "High",
        "forecast": "0.3%",
        "previous": "0.4%",
    }
]


def _patch(monkeypatch, results: list[bool]) -> list[str]:
    sent: list[str] = []

    async def fake_fetch():
        return ROWS, ROWS, NOW, []

    def fake_send(text, topic=None):
        sent.append(text)
        return results.pop(0) if results else True

    monkeypatch.setattr(md, "fetch", fake_fetch)
    monkeypatch.setattr(md, "send", fake_send)
    return sent


def test_remind_sends_once_and_records_key(monkeypatch, tmp_path):
    sent = _patch(monkeypatch, [])
    assert md.main(["remind", "--data-dir", str(tmp_path)]) == 0
    assert len(sent) == 1 and "CPI m/m" in sent[0]
    assert (tmp_path / "reminders_sent.json").exists()
    assert md.main(["remind", "--data-dir", str(tmp_path)]) == 0
    assert len(sent) == 1  # repeat run sends nothing


def test_remind_failed_send_records_nothing_and_retries(monkeypatch, tmp_path):
    sent = _patch(monkeypatch, [False, True])
    assert md.main(["remind", "--data-dir", str(tmp_path)]) == 1
    assert not (tmp_path / "reminders_sent.json").exists()
    assert md.main(["remind", "--data-dir", str(tmp_path)]) == 0
    assert len(sent) == 2


def test_remind_corrupt_state_file_does_not_crash(monkeypatch, tmp_path):
    sent = _patch(monkeypatch, [])
    (tmp_path / "reminders_sent.json").write_text("{not json", encoding="utf-8")
    assert md.main(["remind", "--data-dir", str(tmp_path)]) == 0
    assert len(sent) == 1
    assert md.load_sent(tmp_path / "reminders_sent.json")  # rewritten as valid json


def test_week_dry_run_writes_nothing(monkeypatch, tmp_path):
    sent = _patch(monkeypatch, [])
    assert md.main(["week", "--dry-run", "--data-dir", str(tmp_path / "d")]) == 0
    assert sent == [] and not (tmp_path / "d").exists()
