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
    """Last announced state of one provider: ``bad`` and when it was announced (unix seconds); ``pending`` is when it
    started failing while still announced good (the confirmation window), else None."""

    bad: bool
    ts: int | None = None
    pending: int | None = None


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
    confirm_seconds: int = 0,
) -> tuple[dict[str, Watch], list[FiredAlert]]:
    """New state and the alerts to send now.

    ``providers`` is ``HealthRegistry.snapshot()["providers"]`` (``status``, ``consecutive_failures``, ``last_reason``,
    ``last_detail``). A provider never seen before starts as good, so one that fails from the start is announced. A
    provider with no calls yet (``unknown``) or missing from the snapshot keeps its state. With ``confirm_seconds`` a
    failure is announced only when the provider is still failing that long after it crossed ``min_failures``: a short
    network blip of the server (every provider at once, owner 2026-11-04) then sends nothing, neither down nor up.
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
            if prev.pending is not None:
                out[name] = Watch(bad=prev.bad, ts=prev.ts)  # recovered inside the confirmation window
            continue
        if want_bad and confirm_seconds > 0:
            if prev.pending is None:
                out[name] = Watch(bad=False, ts=prev.ts, pending=now_ts)
                continue
            if now_ts - prev.pending < confirm_seconds:
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
                metric={"kind": KIND, "provider": name, "status": h.get("status"), "bad": want_bad}
                | ({} if want_bad or prev.ts is None else {"down_seconds": max(0, now_ts - prev.ts)}),
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


# A recovery after a shorter outage is logged but not sent. 0: every announced failure gets its recovery (failures are
# confirmed for a while first, see evaluate_health's ``confirm_seconds``).
RECOVERY_MIN_DOWN = 0
# This many providers failing in the same tick look like the server's own network, not the venues.
NETWORK_HINT_AT = 4


def digest(fired: list[FiredAlert], *, recovery_min_down: int = RECOVERY_MIN_DOWN) -> FiredAlert | None:
    """One message for all provider-health transitions of a tick (the Telegram text), or None when nothing is worth
    sending: a server network blip flips every provider at once, and one message each was spam. Failures are always
    listed; recoveries only after an outage of at least ``recovery_min_down`` seconds (unknown length counts as long).
    The stored rows stay one per provider (:func:`state_from_alerts` reads them); this only shapes what is sent."""
    bad = [a for a in fired if a.metric.get("bad") is True]
    ok = [a for a in fired if a.metric.get("bad") is False and int(a.metric.get("down_seconds", recovery_min_down)) >= recovery_min_down]
    if not bad and not ok:
        return None
    lines: list[str] = []
    if bad:
        names = ", ".join(str(a.metric.get("provider")) for a in bad)
        lines.append(f"⚠️ Провайдери недоступні ({len(bad)}): {names}. Дані з них не оновлюються, доки не відновляться.")
        if len(bad) >= NETWORK_HINT_AT:
            lines.append("Стільки відразу — схоже на збій мережі сервера, а не бірж.")
        else:
            lines += [f"• {a.message}" for a in bad]
    if ok:
        parts = []
        for a in ok:
            down = a.metric.get("down_seconds")
            parts.append(f"{a.metric.get('provider')}" + (f" (≈{_dur(int(down))})" if down is not None else ""))
        lines.append("✅ Знову працюють: " + ", ".join(parts) + ".")
    ts = max(a.ts for a in fired)
    return FiredAlert(
        rule_id=f"{RULE_PREFIX}digest",
        ts=ts,
        value=float(len(bad)),
        threshold=float(NETWORK_HINT_AT),
        op=">=",
        metric={"kind": KIND, "digest": True, "bad": [a.metric.get("provider") for a in bad], "ok": [a.metric.get("provider") for a in ok]},
        message="\n".join(lines),
    )
