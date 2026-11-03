"""Provider-health transition alerts (pure, no I/O; time is a parameter).

:class:`~tradedesk.core.health.HealthRegistry` updates a provider's status on every call, so one failed request
already shows ``degraded``. A message on every flip would be spam, so this module decides when a change is worth one
alert:

- hysteresis: a provider turns *bad* after ``min_failures`` consecutive failed calls and *good* again after one
  successful call; 1 .. ``min_failures - 1`` failures keep the previous state;
- one message per transition: the state only changes when it is announced;
- a quiet period per provider after each announcement: a transition inside it waits until the period ends and is
  dropped if the provider flipped back meanwhile, so a flapping provider sends at most one message per period.

The state survives a restart through the stored alerts (:func:`state_from_alerts`), so a provider that stays down
across a deploy is not announced again.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .engine import FiredAlert

RULE_PREFIX = "health:"
KIND = "provider_health"


@dataclass(frozen=True)
class Watch:
    """Last announced state of one provider: ``bad`` and when it was announced (unix seconds)."""

    bad: bool
    ts: int | None = None


def _dur(seconds: int) -> str:
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} хв"
    if seconds < 2 * 86400:
        return f"{seconds / 3600:.1f} год"
    return f"{seconds / 86400:.1f} дн"


def _clean(text: Any, limit: int = 120) -> str:
    # last_detail is already redacted by HealthRegistry; it is still untrusted provider text, so keep it short and flat.
    return " ".join(str(text or "").split())[:limit]


def _bad_message(name: str, h: Mapping[str, Any]) -> str:
    label = "недоступний" if h.get("status") == "down" else "деградував"
    reason = _clean(h.get("last_reason")) or "невідома"
    detail = _clean(h.get("last_detail"))
    tail = f" ({detail})" if detail else ""
    return (
        f"Провайдер {name} {label}: {int(h.get('consecutive_failures') or 0)} збоїв поспіль, причина {reason}{tail}. "
        "Дані з нього не оновлюються, доки він не відновиться."
    )


def _ok_message(name: str, prev: Watch, now_ts: int) -> str:
    was = f" (не працював ≈{_dur(now_ts - prev.ts)})" if prev.ts is not None and now_ts >= prev.ts else ""
    return f"Провайдер {name} знову працює{was}."


def evaluate_health(
    providers: Mapping[str, Mapping[str, Any]],
    state: Mapping[str, Watch],
    *,
    now_ts: int,
    min_failures: int = 3,
    quiet_seconds: int = 900,
) -> tuple[dict[str, Watch], list[FiredAlert]]:
    """New state and the alerts to send now.

    ``providers`` is ``HealthRegistry.snapshot()["providers"]`` (``status``, ``consecutive_failures``, ``last_reason``,
    ``last_detail``). A provider never seen before starts as good, so one that fails from the start is announced. A
    provider with no calls yet (``unknown``) or missing from the snapshot keeps its state.
    """
    min_failures = max(1, int(min_failures))
    out: dict[str, Watch] = dict(state)
    fired: list[FiredAlert] = []
    for name, h in providers.items():
        if h.get("status") == "unknown":
            continue
        prev = out.get(name, Watch(bad=False))
        cf = int(h.get("consecutive_failures") or 0)
        want_bad = True if cf >= min_failures else False if cf == 0 else prev.bad
        if want_bad == prev.bad:
            continue
        if prev.ts is not None and now_ts - prev.ts < quiet_seconds:
            continue  # quiet period: decide again on a later tick
        out[name] = Watch(bad=want_bad, ts=now_ts)
        fired.append(
            FiredAlert(
                rule_id=f"{RULE_PREFIX}{name}",
                ts=now_ts,
                value=float(cf),
                threshold=float(min_failures),
                op=">=" if want_bad else "==",
                metric={"kind": KIND, "provider": name, "status": h.get("status"), "bad": want_bad},
                message=_bad_message(name, h) if want_bad else _ok_message(name, prev, now_ts),
            )
        )
    return out, fired


def state_from_alerts(rows: Iterable[Mapping[str, Any]]) -> dict[str, Watch]:
    """State from the newest stored health alert of each provider (``rule_id``, ``ts``, ``payload`` with ``metric``).
    Rows of other rules or without a boolean ``metric.bad`` are skipped."""
    out: dict[str, Watch] = {}
    for r in rows:
        rule_id = str(r.get("rule_id") or "")
        payload = r.get("payload")
        metric = payload.get("metric") if isinstance(payload, Mapping) else None
        if not rule_id.startswith(RULE_PREFIX) or not isinstance(metric, Mapping) or not isinstance(metric.get("bad"), bool):
            continue
        try:
            ts = int(r["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        name = rule_id[len(RULE_PREFIX) :]
        if name not in out or (out[name].ts or 0) <= ts:
            out[name] = Watch(bad=metric["bad"], ts=ts)
    return out
