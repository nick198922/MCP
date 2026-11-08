from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from conftest import wait_until
from tradedesk.collector.scheduler import PeriodicJob, Scheduler
from tradedesk.logfmt import JsonFormatter
from tradedesk.server import configure_logging


def _record(msg: str, *args: object, exc_info=None, **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("tradedesk.collector", logging.WARNING, __file__, 1, msg, args, exc_info)
    rec.created = datetime(2026, 11, 5, 8, 0, 0, 123_000, tzinfo=UTC).timestamp()  # fixed clock
    rec.__dict__.update(extra)
    return rec


def test_json_line_has_the_fields():
    line = JsonFormatter().format(_record("collector job %s failed: %s", "funding", "boom", job="funding", error="X: y"))
    assert "\n" not in line
    out = json.loads(line)
    assert out == {
        "ts": "2026-11-05T08:00:00.123+00:00",
        "level": "WARNING",
        "logger": "tradedesk.collector",
        "msg": "collector job funding failed: boom",
        "job": "funding",
        "error": "X: y",
    }


def test_json_keeps_non_ascii():
    line = JsonFormatter().format(_record("ринок «Київ» закрито €"))
    assert "ринок «Київ» закрито €" in line  # ensure_ascii=False: readable in journalctl
    assert json.loads(line)["msg"] == "ринок «Київ» закрито €"


def test_json_exception_is_a_string_with_traceback():
    try:
        raise ValueError("bad tick")
    except ValueError:
        rec = _record("job failed", exc_info=sys.exc_info())
    out = json.loads(JsonFormatter().format(rec))
    assert isinstance(out["exc"], str)
    assert out["exc"].startswith("Traceback") and "ValueError: bad tick" in out["exc"]


def test_json_drops_unknown_extras_and_secrets(monkeypatch):
    monkeypatch.setenv("TRADEDESK_HTTP_BEARER_TOKEN", "env-secret-123")
    rec = _record(
        "POST https://api.telegram.org/bot42:TOKENPART/sendMessage failed; api_key=k3y",
        error="HTTP 401 for ?apiKey=abc&signature=deadbeef",
        settings={"token": "env-secret-123"},
    )
    line = JsonFormatter().format(rec)
    out = json.loads(line)
    assert set(out) == {"ts", "level", "logger", "msg", "error"}  # `settings` is not an allow-listed extra
    for secret in ("env-secret-123", "TOKENPART", "k3y", "deadbeef"):
        assert secret not in line


@contextmanager
def _bare_root() -> Iterator[logging.Logger]:
    # pytest puts its capture handlers on the root logger, and basicConfig is a no-op while any handler is there.
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    root.handlers = []
    try:
        yield root
    finally:
        for h in root.handlers:
            h.close()
        root.handlers, root.level = saved, level


def test_configure_logging_json_writes_to_stderr_only(monkeypatch):
    monkeypatch.setenv("TRADEDESK_LOG_FORMAT", "json")
    err, out = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(sys, "stdout", out)  # stdio transport: stdout is the MCP protocol
    with _bare_root() as root:
        configure_logging()
        assert len(root.handlers) == 1 and isinstance(root.handlers[0].formatter, JsonFormatter)
        logging.getLogger("tradedesk.test").info("hello")
    assert out.getvalue() == ""
    assert json.loads(err.getvalue())["msg"] == "hello"


@pytest.mark.parametrize("value", [None, "", "text", "JSONL"])
def test_configure_logging_default_format_unchanged(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("TRADEDESK_LOG_FORMAT", raising=False)
    else:
        monkeypatch.setenv("TRADEDESK_LOG_FORMAT", value)
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    with _bare_root() as root:
        configure_logging()
        assert root.handlers[0].formatter is not None
        assert root.handlers[0].formatter._fmt == "%(asctime)s %(levelname)s %(name)s: %(message)s"
        logging.getLogger("tradedesk.test").info("hello")
    assert err.getvalue().rstrip().endswith("INFO tradedesk.test: hello")


async def test_scheduler_failure_passes_job_and_error(caplog):
    async def boom(_rt) -> int:
        raise RuntimeError("nope")

    sched = Scheduler(None, [PeriodicJob(name="boom", period_seconds=60, fn=boom)])  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
        await sched.start()
        try:
            await wait_until(lambda: sched.jobs[0].metrics.runs > 0)
        finally:
            await sched.stop()
    recs = [r for r in caplog.records if r.getMessage() == "collector job boom failed: nope"]
    assert len(recs) == 1
    out = json.loads(JsonFormatter().format(recs[0]))
    assert out["job"] == "boom" and out["error"] == "RuntimeError: nope"
    assert sched.jobs[0].metrics.failures == 1  # logic unchanged
