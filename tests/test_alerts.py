from __future__ import annotations

import json

import pytest

from tradedesk.alerts.engine import Rule, _format_message, evaluate_rules, load_rules
from tradedesk.store import Store


@pytest.fixture
async def store() -> Store:
    s = Store(":memory:")
    await s.init()
    yield s
    await s.aclose()


def test_format_message_substitutes_value_and_threshold():
    out = _format_message("f={value_pct:+.2f}% > {threshold_pct:.0f}%", value=0.42, threshold=0.30)
    assert out == "f=+42.00% > 30%"


def test_format_message_returns_template_on_bad_key():
    assert _format_message("hello {missing}", 1.0, 2.0) == "hello {missing}"


def test_load_rules_filters_bad_rows(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text(
        json.dumps(
            {
                "rules": [
                    {"id": "good", "metric": {"kind": "funding", "venue": "x", "base": "BTC"}, "when": {"op": ">", "value": 0.1}},
                    "not-a-dict",
                    {"id": "no-metric", "when": {"op": ">", "value": 0.1}},
                ]
            }
        )
    )
    rules = load_rules(p, default_cooldown=600)
    assert [r.id for r in rules] == ["good"]
    assert rules[0].cooldown_seconds == 600
    assert rules[0].enabled is True


async def test_evaluate_rules_fires_on_breach(store: Store):
    await store.insert_funding(
        ts=1_000_000,
        rows=[{"venue": "binanceusdm", "base": "BTC", "rate_per_interval": 0.0003, "interval_hours": 8.0, "annualized": 0.32}],
    )
    rules = [
        Rule(
            id="hot_funding",
            enabled=True,
            metric={"kind": "funding", "venue": "binanceusdm", "base": "BTC"},
            when={"op": ">", "value": 0.30},
            cooldown_seconds=3600,
            message="funding is hot: {value_pct:.1f}%",
        )
    ]
    fired = await evaluate_rules(rules, store, now_ts=1_000_000)
    assert len(fired) == 1
    assert fired[0].value == pytest.approx(0.32)
    assert "32" in fired[0].message


async def test_evaluate_rules_respects_cooldown(store: Store):
    await store.insert_funding(
        ts=2_000,
        rows=[{"venue": "binanceusdm", "base": "BTC", "rate_per_interval": 0.0003, "interval_hours": 8.0, "annualized": 0.32}],
    )
    rules = [
        Rule(
            id="hot_funding",
            enabled=True,
            metric={"kind": "funding", "venue": "binanceusdm", "base": "BTC"},
            when={"op": ">", "value": 0.30},
            cooldown_seconds=600,
            message="",
        )
    ]
    # Record a prior alert 300s ago; new one must be skipped (cooldown=600s).
    await store.insert_alert(rule_id="hot_funding", ts=2_000 - 300, payload={"foo": 1}, channel="log", delivered=True)
    fired = await evaluate_rules(rules, store, now_ts=2_000)
    assert fired == []

    # Jump forward past cooldown — should fire again.
    fired2 = await evaluate_rules(rules, store, now_ts=2_000 + 400)
    assert len(fired2) == 1


async def test_evaluate_rules_ignores_disabled_and_missing(store: Store):
    rules = [
        Rule(
            id="off",
            enabled=False,
            metric={"kind": "funding", "venue": "x", "base": "BTC"},
            when={"op": ">", "value": 0.0},
            cooldown_seconds=0,
        ),
        Rule(
            id="no_data",
            enabled=True,
            metric={"kind": "funding", "venue": "x", "base": "BTC"},
            when={"op": ">", "value": 0.0},
            cooldown_seconds=0,
        ),
        Rule(
            id="bad_op",
            enabled=True,
            metric={"kind": "funding", "venue": "x", "base": "BTC"},
            when={"op": "**bad**", "value": 0.0},
            cooldown_seconds=0,
        ),
    ]
    fired = await evaluate_rules(rules, store, now_ts=0)
    assert fired == []


async def test_atm_iv_and_basis_metrics(store: Store):
    expiry = "2026-11-27T08:00:00+00:00"
    await store.insert_atm_term(
        ts=10,
        currency="BTC",
        rows=[{"expiry": expiry, "days": 48.0, "forward": 83000.0, "atm_iv": 0.42}],
    )
    await store.insert_basis(
        ts=10,
        currency="BTC",
        rows=[{"expiry": expiry, "days": 48.0, "basis_pct": 0.025, "annualized_simple": 0.11}],
    )
    rules = [
        Rule(
            id="iv_high",
            enabled=True,
            metric={"kind": "atm_iv", "currency": "BTC", "expiry": expiry},
            when={"op": ">", "value": 0.40},
            cooldown_seconds=0,
        ),
        Rule(
            id="basis_wide",
            enabled=True,
            metric={"kind": "basis", "currency": "BTC", "expiry": expiry},
            when={"op": ">", "value": 0.02},
            cooldown_seconds=0,
        ),
    ]
    fired = await evaluate_rules(rules, store, now_ts=10)
    assert {f.rule_id for f in fired} == {"iv_high", "basis_wide"}


async def test_telegram_channel_posts_into_the_alerts_topic():
    import httpx

    from tradedesk.alerts.channels import TelegramChannel
    from tradedesk.alerts.engine import FiredAlert
    from tradedesk.core.http import Http
    from tradedesk.providers.telegram import BASE_URL, Telegram

    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    tg = Telegram(Http("telegram", BASE_URL, transport=httpx.MockTransport(handler)), "1:abc")
    alert = FiredAlert("r", 0, 0.5, 0.3, ">", {}, "BTC funding high")
    assert await TelegramChannel(tg, "-100", thread_id=7).send(alert) == (True, None)
    assert await TelegramChannel(tg, "-100").send(alert) == (True, None)
    await tg.aclose()
    assert bodies[0] == {"chat_id": "-100", "text": "BTC funding high", "message_thread_id": 7}
    assert "message_thread_id" not in bodies[1]


def test_settings_read_the_alerts_topic(monkeypatch):
    from tradedesk.config import Settings

    monkeypatch.setenv("TELEGRAM_TOPIC_ALERTS", "12")
    assert Settings.from_env().telegram_topic_alerts == 12
    monkeypatch.setenv("TELEGRAM_TOPIC_ALERTS", "")
    assert Settings.from_env().telegram_topic_alerts is None
