from __future__ import annotations

import json
import logging

from conftest import NOW, FakeEx, make_transports
from tradedesk.alerts import engine
from tradedesk.alerts.engine import load_rules
from tradedesk.alerts.health import RULE_PREFIX, Watch, evaluate_health, state_from_alerts
from tradedesk.collector.scheduler import _evaluate_alerts
from tradedesk.config import Settings
from tradedesk.core.health import HealthRegistry
from tradedesk.runtime import Runtime

T0 = 1_800_000_000


def _fail(h: HealthRegistry, name: str, n: int) -> None:
    for _ in range(n):
        h.error(name, "backend_error", "HTTP 502: bad gateway")


def _providers(h: HealthRegistry) -> dict:
    return h.snapshot()["providers"]


def test_goes_bad_after_min_failures_once():
    h = HealthRegistry()
    h.ok("deribit", 50.0)
    _fail(h, "deribit", 2)  # degraded, below the threshold: no alert
    state, fired = evaluate_health(_providers(h), {}, now_ts=T0)
    assert fired == [] and state.get("deribit", Watch(False)).bad is False

    _fail(h, "deribit", 1)  # 3 in a row: down
    state, fired = evaluate_health(_providers(h), state, now_ts=T0 + 60)
    (a,) = fired
    assert a.rule_id == f"{RULE_PREFIX}deribit" and a.ts == T0 + 60
    assert a.metric == {"kind": "provider_health", "provider": "deribit", "status": "down", "bad": True}
    assert "deribit недоступний" in a.message and "3 збоїв поспіль" in a.message and "backend_error" in a.message
    assert state["deribit"] == Watch(bad=True, ts=T0 + 60)

    # Still failing, long after the quiet period: no repeat.
    _fail(h, "deribit", 10)
    state2, fired = evaluate_health(_providers(h), state, now_ts=T0 + 86_400)
    assert fired == [] and state2 == state


def test_recovery_sends_one_message_with_duration():
    h = HealthRegistry()
    _fail(h, "hyperliquid", 3)
    state, fired = evaluate_health(_providers(h), {}, now_ts=T0)
    assert len(fired) == 1  # failing from the start is announced too

    h.ok("hyperliquid", 40.0)
    state, fired = evaluate_health(_providers(h), state, now_ts=T0 + 1800)
    (a,) = fired
    assert a.metric["bad"] is False and a.op == "=="
    assert a.message == "Провайдер hyperliquid знову працює (не працював ≈30 хв)."
    assert state["hyperliquid"] == Watch(bad=False, ts=T0 + 1800)

    state2, fired = evaluate_health(_providers(h), state, now_ts=T0 + 7200)
    assert fired == [] and state2 == state


def test_hysteresis_band_keeps_bad_state():
    h = HealthRegistry()
    _fail(h, "ccxt:okx", 3)
    state, _ = evaluate_health(_providers(h), {}, now_ts=T0)
    h.ok("ccxt:okx", 10.0)
    _fail(h, "ccxt:okx", 1)  # one failure after a success: neither ok nor bad enough, stays bad, no recovery message
    state2, fired = evaluate_health(_providers(h), state, now_ts=T0 + 3600)
    assert fired == [] and state2["ccxt:okx"].bad is True


def test_quiet_period_swallows_flapping():
    h = HealthRegistry()
    _fail(h, "coingecko", 3)
    state, fired = evaluate_health(_providers(h), {}, now_ts=T0, quiet_seconds=900)
    assert len(fired) == 1
    h.ok("coingecko", 10.0)  # back within the quiet period: wait
    state, fired = evaluate_health(_providers(h), state, now_ts=T0 + 300, quiet_seconds=900)
    assert fired == [] and state["coingecko"].bad is True
    _fail(h, "coingecko", 3)  # bad again before the period ended: the recovery is dropped, nothing to say
    state, fired = evaluate_health(_providers(h), state, now_ts=T0 + 1000, quiet_seconds=900)
    assert fired == []
    h.ok("coingecko", 10.0)  # ok after the period: one recovery message
    state, fired = evaluate_health(_providers(h), state, now_ts=T0 + 1200, quiet_seconds=900)
    assert [a.metric["bad"] for a in fired] == [False]


def test_unknown_and_min_failures_one():
    h = HealthRegistry()
    h.register("polymarket")  # no calls yet
    h.ok("deribit", 5.0)
    _fail(h, "deribit", 1)
    state, fired = evaluate_health(_providers(h), {}, now_ts=T0, min_failures=1)
    (a,) = fired
    assert a.metric["provider"] == "deribit" and "deribit деградував" in a.message
    assert "polymarket" not in state


def test_detail_is_flattened_and_short():
    h = HealthRegistry()
    h.error("x", "backend_error", "line1\nline2 " + "y" * 500)
    _fail(h, "x", 2)
    _, fired = evaluate_health(_providers(h), {}, now_ts=T0)
    assert "\n" not in fired[0].message and len(fired[0].message) < 300


def test_secret_in_detail_never_reaches_message():
    h = HealthRegistry()
    _fail(h, "x", 2)
    h.error("x", "auth_error", "HTTP 401 GET https://api.example/v1?token=abc123&apiKey=k9 {'secret': 'zz77'}")
    _, fired = evaluate_health(_providers(h), {}, now_ts=T0)
    (a,) = fired
    assert "auth_error" in a.message and "token=***" in a.message
    for secret in ("abc123", "k9", "zz77"):
        assert secret not in a.message and secret not in str(a.payload())


def test_json_rule_with_reserved_health_prefix_is_skipped(tmp_path):
    path = tmp_path / "rules.json"
    rule = {"metric": {"kind": "funding", "venue": "binanceusdm", "base": "BTC"}, "when": {"op": ">", "value": 0.3}}
    path.write_text(json.dumps({"rules": [{"id": "health:deribit", **rule}, {"id": "funding_hot", **rule}]}))
    assert [r.id for r in load_rules(path)] == ["funding_hot"]


def test_reserved_health_prefix_warning_logged_once_per_id(tmp_path, monkeypatch, caplog):
    # load_rules runs on every collector tick: the reserved-id warning must not repeat, the rule must stay skipped.
    monkeypatch.setattr(engine, "_WARNED_RESERVED_IDS", set())
    path = tmp_path / "rules.json"
    rule = {"metric": {"kind": "funding", "venue": "binanceusdm", "base": "BTC"}, "when": {"op": ">", "value": 0.3}}
    path.write_text(json.dumps({"rules": [{"id": "health:deribit", **rule}, {"id": "funding_hot", **rule}]}))
    with caplog.at_level(logging.WARNING, logger=engine.__name__):
        first = load_rules(path)
        second = load_rules(path)
    assert [r.id for r in first] == ["funding_hot"]
    assert [r.id for r in second] == ["funding_hot"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "health:deribit" in r.getMessage()]
    assert len(warnings) == 1


async def test_health_failure_does_not_block_other_alerts(state, tmp_path):
    # Review 2026-11-03, remark 1: a DB error while restoring the health state must not drop the tick's other alerts.
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "id": "funding_hot",
                        "metric": {"kind": "funding", "venue": "binanceusdm", "base": "BTC"},
                        "when": {"op": ">", "value": 0.30},
                    }
                ]
            }
        )
    )
    settings = Settings(
        http_retries=1,
        http_retry_backoff=0.0,
        cache_scale=0.0,
        db_path=":memory:",
        collector_enabled=True,
        alerts_enabled=True,
        alerts_rules_path=str(rules_path),
    )
    rt = Runtime(settings, transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()

    async def broken(_prefix: str) -> list:
        raise RuntimeError("database is locked")

    rt.store.last_alerts_with_prefix = broken  # type: ignore[method-assign]
    try:
        await rt.store.insert_funding(
            ts=int(NOW.timestamp()),
            rows=[{"venue": "binanceusdm", "base": "BTC", "rate_per_interval": 0.0003, "interval_hours": 8.0, "annualized": 0.32}],
        )
        _fail(rt.health, "deribit", 3)
        assert await _evaluate_alerts(rt) == 1
        assert [r["rule_id"] for r in await rt.store.recent_alerts()] == ["funding_hot"]
        assert rt.health_watch is None  # nothing was announced, so the next tick retries the restore
    finally:
        await rt.aclose()


def test_state_from_alerts():
    rows = [
        {"rule_id": "health:deribit", "ts": T0, "payload": {"metric": {"bad": True}}},
        {"rule_id": "health:deribit", "ts": T0 - 10, "payload": {"metric": {"bad": False}}},  # older: ignored
        {"rule_id": "funding_hot", "ts": T0, "payload": {"metric": {"bad": True}}},  # another rule
        {"rule_id": "health:broken", "ts": T0, "payload": "not json"},
    ]
    assert state_from_alerts(rows) == {"deribit": Watch(bad=True, ts=T0)}


async def test_alerts_job_announces_once_survives_restart_and_recovers(state):
    settings = Settings(
        http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=":memory:", collector_enabled=True, alerts_enabled=True
    )
    rt = Runtime(settings, transports=make_transports(state), exchange_factory=FakeEx)
    clock = [NOW.timestamp()]
    rt.now = lambda: NOW.fromtimestamp(clock[0], NOW.tzinfo)
    assert rt.store is not None
    await rt.store.init()
    try:
        rt.health.ok("deribit", 20.0)
        _fail(rt.health, "deribit", 3)
        assert await _evaluate_alerts(rt) == 0  # failing, but not yet for the confirmation window (10 min)
        clock[0] += 600
        assert await _evaluate_alerts(rt) == 1
        recent = await rt.store.recent_alerts()
        assert [r["rule_id"] for r in recent] == ["health:deribit"] and recent[0]["channel"] == "log"
        assert recent[0]["payload"]["metric"]["bad"] is True

        clock[0] += 3600
        assert await _evaluate_alerts(rt) == 0  # still down: no repeat

        rt.health_watch = None  # a restart: the state comes back from the alerts table
        clock[0] += 3600
        assert await _evaluate_alerts(rt) == 0

        rt.health.ok("deribit", 20.0)
        clock[0] += 60
        assert await _evaluate_alerts(rt) == 1
        recent = await rt.store.recent_alerts()
        assert recent[0]["payload"]["metric"]["bad"] is False and "знову працює" in recent[0]["payload"]["message"]
    finally:
        await rt.aclose()


async def test_alerts_job_health_switch_off(state):
    settings = Settings(
        http_retries=1,
        http_retry_backoff=0.0,
        cache_scale=0.0,
        db_path=":memory:",
        collector_enabled=True,
        alerts_enabled=True,
        alerts_health_enabled=False,
    )
    rt = Runtime(settings, transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    try:
        _fail(rt.health, "deribit", 5)
        assert await _evaluate_alerts(rt) == 0
        assert await rt.store.recent_alerts() == []
    finally:
        await rt.aclose()


def test_digest_one_message_for_a_network_blip_and_no_short_recoveries():
    from tradedesk.alerts.health import digest

    h = HealthRegistry()
    names = ["deribit", "bybit", "binance_spot", "binance_futures", "hyperliquid"]
    for n in names:
        h.ok(n, 10.0)
        _fail(h, n, 3)
    state, fired = evaluate_health(_providers(h), {}, now_ts=T0)
    d = digest(fired)
    assert d is not None and d.rule_id == "health:digest"
    assert "недоступні (5)" in d.message and "мережі сервера" in d.message and all(n in d.message for n in names)
    assert state_from_alerts([{"rule_id": d.rule_id, "ts": d.ts, "payload": d.payload()}]) == {}  # never read as state

    for n in names[:4]:
        h.ok(n, 10.0)
    _, short = evaluate_health(_providers(h), state, now_ts=T0 + 3600 + 60)  # quiet period over, down ≈ 1 h
    assert digest(short) is not None and "Знову працюють" in digest(short).message
    assert digest(short, recovery_min_down=7200) is None  # a shorter outage than the threshold: logged only


async def test_alerts_job_sends_one_telegram_message_for_many_providers(state, monkeypatch):
    import tradedesk.alerts as alerts_pkg
    from tradedesk.alerts.channels import LogChannel

    sent: list[str] = []

    class FakeTg:
        name = "telegram"

        async def send(self, alert):
            sent.append(alert.message)
            return True, None

    monkeypatch.setattr(alerts_pkg, "build_channels", lambda s: [LogChannel(), FakeTg()])
    settings = Settings(
        http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=":memory:", collector_enabled=True, alerts_enabled=True
    )
    rt = Runtime(settings, transports=make_transports(state), exchange_factory=FakeEx)
    clock = [NOW.timestamp()]
    rt.now = lambda: NOW.fromtimestamp(clock[0], NOW.tzinfo)
    assert rt.store is not None
    await rt.store.init()
    try:
        for n in ("deribit", "bybit", "coinbase"):
            rt.health.ok(n, 20.0)
            _fail(rt.health, n, 3)
        assert await _evaluate_alerts(rt) == 0
        clock[0] += 600
        assert await _evaluate_alerts(rt) == 3
        assert len(sent) == 1 and "недоступні (3)" in sent[0]
        rows = await rt.store.recent_alerts()
        assert sorted(r["channel"] for r in rows) == ["log"] * 3 + ["telegram"] * 3

        for n in ("deribit", "bybit", "coinbase"):
            rt.health.ok(n, 20.0)
        clock[0] += 3600 + 60
        assert await _evaluate_alerts(rt) == 3
        assert len(sent) == 2 and "Знову працюють" in sent[1]
    finally:
        await rt.aclose()


def test_a_blip_shorter_than_the_confirmation_window_sends_nothing():
    h = HealthRegistry()
    h.ok("bybit", 10.0)
    _fail(h, "bybit", 5)
    state, fired = evaluate_health(_providers(h), {}, now_ts=T0, confirm_seconds=600)
    assert fired == [] and state["bybit"] == Watch(bad=False, ts=None, pending=T0)
    state, fired = evaluate_health(_providers(h), state, now_ts=T0 + 300, confirm_seconds=600)
    assert fired == []  # still inside the window
    h.ok("bybit", 10.0)  # back before 10 min: no "down", so no "works again" either
    state, fired = evaluate_health(_providers(h), state, now_ts=T0 + 420, confirm_seconds=600)
    assert fired == [] and state["bybit"] == Watch(bad=False, ts=None)
    _fail(h, "bybit", 3)
    state, _ = evaluate_health(_providers(h), state, now_ts=T0 + 500, confirm_seconds=600)
    state, fired = evaluate_health(_providers(h), state, now_ts=T0 + 1100, confirm_seconds=600)
    assert [a.metric["bad"] for a in fired] == [True]
