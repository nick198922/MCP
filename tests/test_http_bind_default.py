"""Guard: the streamable-http mode binds loopback only by default (CLAUDE.md, "Безпека").

A silent change of the default host to 0.0.0.0 would expose the unauthenticated MCP server to the network. No network:
uvicorn.run and build_server are replaced, nothing listens.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
import uvicorn

from tradedesk import server as srv
from tradedesk.config import Settings


def test_settings_default_host_is_loopback(monkeypatch):
    monkeypatch.delenv("TRADEDESK_HOST", raising=False)
    assert Settings().host == "127.0.0.1"
    assert Settings.from_env().host == "127.0.0.1"


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost", "LOCALHOST", " localhost ", "::ffff:127.0.0.1"])
def test_is_loopback_yes(host):
    assert srv.is_loopback(host) is True


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "[::]", "192.168.1.10", "10.0.0.5", "169.58.169.184", "", " ", "example.com", "localhost.evil"],
)
def test_is_loopback_no(host):
    assert srv.is_loopback(host) is False


class _FakeServer:
    def __init__(self):
        self.app_host = None

    def streamable_http_app(self, host):
        self.app_host = host
        return object()

    def run(self, transport):  # stdio path; must not be reached in these tests
        raise AssertionError(f"unexpected run({transport!r})")


def _run_main(monkeypatch, host, tokens=""):
    fake = _FakeServer()
    settings = Settings(transport="streamable-http", host=host, port=8765, http_bearer_tokens=tokens)
    calls = []
    monkeypatch.delenv("TRADEDESK_LOG_FORMAT", raising=False)
    monkeypatch.setattr(srv, "configure_logging", lambda: None)
    monkeypatch.setattr(srv.features, "load", lambda *a, **k: {})  # would otherwise write into os.environ
    monkeypatch.setattr(srv, "build_server", lambda: (fake, SimpleNamespace(settings=settings)))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    srv.main([])
    return fake, calls


def _bind_warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and "non-loopback" in r.getMessage()]


def test_main_on_loopback_host_does_not_warn_about_binding(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="tradedesk")
    fake, calls = _run_main(monkeypatch, "127.0.0.1")
    assert calls == [{"host": "127.0.0.1", "port": 8765, "log_level": "info"}]
    assert fake.app_host == "127.0.0.1"
    assert _bind_warnings(caplog) == []


@pytest.mark.parametrize("tokens, auth", [("", "DISABLED"), ("t-not-a-secret", "enabled")])
def test_main_on_public_host_warns_but_still_starts(monkeypatch, caplog, tokens, auth):
    caplog.set_level(logging.INFO, logger="tradedesk")
    _, calls = _run_main(monkeypatch, "0.0.0.0", tokens)
    assert [c["host"] for c in calls] == ["0.0.0.0"]  # warns, does not refuse (owner may bind behind a proxy)
    (rec,) = _bind_warnings(caplog)
    assert "'0.0.0.0'" in rec.getMessage()
    assert f"auth {auth}" in rec.getMessage()
    assert "t-not-a-secret" not in caplog.text  # the token itself never reaches the log
