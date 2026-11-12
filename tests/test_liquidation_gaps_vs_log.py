"""scripts/liquidation_gaps_vs_log.py: liquidation pauses matched to the stream failure log, on tiny synthetic files."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import liquidation_gaps_vs_log as script  # noqa: E402

BYBIT = "wss://stream.bybit.com/v5/public/linear"
OKX = "wss://ws.okx.com:443/ws/v5/public"
SPAN = ["--log-from", "2026-11-10T09:00:00Z", "--log-to", "2026-11-10T21:00:00Z"]


def line(local: str, url: str, n: int, err: str = "ConnectionClosedError: sent 1011 (internal error) keepalive ping timeout") -> str:
    """A journal line as on the server (short-iso, local time +02:00)."""
    return (
        f"{local}+02:00 tradedesk-mcp[4242]: 2026-11-10 00:00:00,000 WARNING tradedesk.collector: "
        f"liquidation stream {url} failed ({n} in a row): {err}"
    )


def test_parse_log_reads_utc_venue_and_count():
    ev = script.parse_log([line("2026-11-10T12:39:30", BYBIT, 2), "unrelated line", line("2026-11-10T12:39:35", OKX, 1)])
    assert [(script.fmt(e.ts), e.venue, e.in_a_row, e.error, e.pid) for e in ev] == [
        ("2026-11-10 10:39:30", "bybit", 2, "ConnectionClosedError", "4242"),
        ("2026-11-10 10:39:35", "okx", 1, "ConnectionClosedError", "4242"),
    ]


def test_outages_merge_bursts_and_cover_upper_bound():
    t0 = script.parse_time("2026-11-10 10:00:00")
    ev = [script.Event(t0, "bybit", 1, "X"), script.Event(t0 + 60, "bybit", 3, "X"), script.Event(t0 + 1000, "bybit", 1, "X")]
    spans = script.outages(ev)
    # Burst 1: from 50 s before the first line to the reconnect after the 3rd in a row (backoff 4 s + 10 s handshake).
    assert spans == [(t0 - 50, t0 + 60 + 4 + 10), (t0 + 1000 - 50, t0 + 1000 + 1 + 10)]
    assert script.covered(t0, t0 + 100, spans) == pytest.approx(74)
    assert script.covered(t0 - 100, t0 + 2000, spans) == pytest.approx(124 + 61)


def test_classify():
    assert script.classify(0.5, False) == "drop"
    assert script.classify(0.49, True) == "edge"
    assert script.classify(0.0, False) == "quiet"


@pytest.fixture
def files(tmp_path):
    gaps = tmp_path / "gaps.csv"
    gaps.write_text(
        "venue,gap_start_utc,gap_end_utc,minutes,rows_hour_before,rows_hour_after\n"
        "bybit,2026-11-10 07:00:00,2026-11-10 07:40:00,40.0,10,10\n"  # before the log span: ignored
        "bybit,2026-11-10 10:00:00,2026-11-10 10:40:00,40.0,20,30\n"  # failures every minute: drop
        "bybit,2026-11-10 12:00:00,2026-11-10 12:40:00,40.0,40,50\n"  # one failure 30 s before the end: edge
        "bybit,2026-11-10 14:00:00,2026-11-10 14:40:00,40.0,60,70\n"  # nothing logged: quiet
        "bybit,2026-11-10 16:00:00,2026-11-10 16:10:00,10.0,80,90\n"  # shorter than --min-minutes
        "okx,2026-11-10 18:00:00,2026-11-10 18:05:00,5.0,500,600\n",
        encoding="utf-8",
    )
    log_lines = [line(f"2026-11-10T12:{m:02d}:30", BYBIT, i + 1) for i, m in enumerate(range(1, 31))]  # 10:01:30..10:30:30 UTC
    log_lines += [line("2026-11-10T14:39:30", BYBIT, 1), line("2026-11-10T14:39:35", OKX, 1), line("2026-11-10T20:00:00", OKX, 1)]
    log = tmp_path / "warn.log"
    log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return gaps, log


def test_report_classifies_drop_edge_quiet(files, tmp_path):
    gaps, log = files
    out = tmp_path / "report.txt"
    assert script.main(["--gaps", str(gaps), "--log", str(log), *SPAN, "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "pauses >= 30 min inside the span: 3 (2.0 h)" in text
    assert "classes: drop 1, edge 1, quiet 1" in text
    assert "bybit failures: 31 (2.58/h)" in text
    assert "a failure within tol of the pause end: 1" in text
    rows = {ln[:19]: ln.split()[-1] for ln in text.splitlines() if ln.startswith("2026-11-10 ")}
    assert rows == {"2026-11-10 10:00:00": "drop", "2026-11-10 12:00:00": "edge", "2026-11-10 14:00:00": "quiet"}
    # Overview: Bybit and OKX failed together once (5 s apart); OKX has no pause >= 30 min.
    assert any(ln.split() == ["bybit", "31", "1", "3", "2.0"] for ln in text.splitlines())
    assert any(ln.split() == ["okx", "2", "1", "0", "0.0"] for ln in text.splitlines())
    assert "rows/hour around the pauses, median: before 40, after 50" in text


def test_defaults_take_the_log_span_and_since_filters(files, capsys):
    gaps, log = files
    assert script.main(["--gaps", str(gaps), "--log", str(log), "--since", "2026-11-10 11:00:00", "--min-minutes", "5"]) == 0
    text = capsys.readouterr().out
    # Span = first .. last log line (10:01:30 .. 18:00:00 UTC); since 11:00 leaves the 12:00, 14:00 and 16:00 pauses.
    assert "log span 2026-11-10 10:01:30 .. 2026-11-10 18:00:00 UTC" in text
    assert "pauses >= 5 min inside the span: 3" in text
    assert "classes: drop 0, edge 1, quiet 2" in text


def test_empty_log_needs_an_explicit_span(tmp_path, files):
    gaps, _ = files
    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit):
        script.main(["--gaps", str(gaps), "--log", str(empty)])
    out = tmp_path / "r.txt"
    assert script.main(["--gaps", str(gaps), "--log", str(empty), *SPAN, "--out", str(out)]) == 0
    assert "classes: drop 0, edge 0, quiet 3" in out.read_text(encoding="utf-8")
