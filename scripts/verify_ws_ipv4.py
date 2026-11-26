"""Item 8a check after the IPv4 deploy: rollback conditions now, the full verdict only from H + 50 h 15 min.

    uv run python scripts/verify_ws_ipv4.py [--db PATH] [--h ISO] [--now ISO] [--no-health] [--url URL] [--out PATH]

Rules: docs/changes/2026-11-25-ws-stream-stalls.md («Критерій перевірки після деплою», «Умова відкату»), registered
before the deploy; the logic is in ``tradedesk.analytics.ws_ipv4_check`` (its docstring lists the readings taken where
the text leaves room). The collector DB is opened READ-ONLY (``mode=ro``); no venue is contacted. ``system_health``
(rollback (a): ``consecutive_failures``, deploy check: ``peer``) is read from the RUNNING service at ``--url`` (as
``scripts/mcp_call.py`` does, but never from an in-process server: that would be another process with its own
streams); unreachable, or a store path other than ``--db`` -> that part is reported as not read. ``--no-health`` skips
it. A triggered rollback ((a) journal, (a) system_health or (b)) withholds the window verdict (rule).

H = 2026-11-25 06:00 UTC: commit 78649a76 (PR #1628, the one that brought ``WS_FAMILY``) was merged into main at
05:22:47 UTC (``git log origin/main --first-parent --format=%ct``), the first ``:20`` update after it pulled it at
06:20:03 UTC (reflog of the service clone). ``--h`` only if the rule moves H (``peer`` still IPv6 after that mark).

Before the full verdict time this prints only the rollback checks and the clean hour count; the verdict (confirmed /
refuted / extend to 168 h / partial effect / invalid window) is computed only once the registered time has passed
and the window has its clean hours. ``--now`` may move the clock back (to repeat an earlier run), never forward.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tradedesk.analytics import ws_ipv4_check as w  # noqa: E402

DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
DEFAULT_H = "2026-11-25T06:00:00+00:00"
DEFAULT_URL = "http://127.0.0.1:8000/mcp"
ROOT = Path(__file__).resolve().parents[1]
OWN_FILES = (Path(__file__).resolve(), ROOT / "src/tradedesk/analytics/ws_ipv4_check.py")


def _ts(s: str) -> int:
    d = datetime.fromisoformat(s)
    return int((d if d.tzinfo else d.replace(tzinfo=UTC)).timestamp())


def _t(ts: float | None) -> str:
    return "-" if ts is None else datetime.fromtimestamp(ts, UTC).strftime("%d.%m %H:%M")


def resolve_now(arg: str | None, clock: float) -> float:
    """``--now`` (ISO) or the clock, never later than the clock: a future --now would count unwritten hours."""
    return clock if arg is None else min(float(_ts(arg)), clock)


def load_journal(db: str, since: int) -> dict[str, dict[int, tuple[float, int]]]:
    """Both hourly connection journals from ``since`` (unix s), READ-ONLY; a DB without ``book_stream_hour`` gives
    no book streams."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    out: dict[str, dict[int, tuple[float, int]]] = {}
    try:
        queries = (
            "SELECT exchange, hour_ts, connected_s, failures FROM liquidation_stream_hour WHERE hour_ts >= ?",
            "SELECT stream, hour_ts, connected_s, failures FROM book_stream_hour WHERE hour_ts >= ?",
        )
        for q in queries:
            try:
                rows = con.execute(q, (since,)).fetchall()
            except sqlite3.OperationalError:  # no such table (a DB from before schema 41)
                continue
            for s, h, c, f in rows:
                out.setdefault(str(s), {})[int(h)] = (float(c), int(f))
    finally:
        con.close()
    return out


def read_health(url: str) -> tuple[dict[str, Any] | None, str]:
    """``system_health`` data of the running service, or (None, why). Never falls back to an in-process server."""
    import mcp_call  # scripts/mcp_call.py: the same client session, refusal and rendering

    try:
        code, text = asyncio.run(mcp_call._over(url, "system_health", {"include_details": True}, False))
    except mcp_call._NoAnswer as e:
        return None, f"the service at {url} did not answer ({e})"
    if code != 0:
        return None, f"system_health failed (exit {code}): {text[:300]}"
    try:
        env = json.loads(text)
    except ValueError:
        return None, "system_health answer is not JSON"
    data = env.get("data") if isinstance(env, dict) else None
    if not isinstance(data, dict):
        return None, "system_health answer has no data"
    return data, f"{url}, as_of {env.get('as_of')}, store {(data.get('store') or {}).get('path')}"


def check_store(health: tuple[dict[str, Any] | None, str], db: str) -> tuple[dict[str, Any] | None, str]:
    """A ``system_health`` answer whose store is not ``--db`` came from another process (e.g. a server from a clone)
    with its own streams: treat it as not read."""
    data = health[0]
    if data is None:
        return health
    path = (data.get("store") or {}).get("path")
    if not path or Path(str(path)).resolve() != Path(db).resolve():
        return None, f"other store: the service at the URL reports {path}, --db is {db}"
    return health


def _r(p: dict[str, Any]) -> str:
    if not p["hours"]:
        return "no hours"
    return f"{p['failures']} failures / {p['hours']} h = {p['rate']:.2f}/h, {p['share']:.0%} of hours"


def _hours(hs: list[int]) -> str:
    return ", ".join(_t(h)[:8] for h in hs) or "none"


def render(journal: w.Journal, h: int, now: float, health: tuple[dict[str, Any] | None, str] | None) -> tuple[str, dict[str, Any]]:
    """The report and a JSON-able summary."""
    sha = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:12] for p in OWN_FILES}
    out = [
        "# 8a check: WebSocket streams over IPv4",
        f"H {_t(h)} UTC; now {_t(now)} UTC; rules docs/changes/2026-11-25-ws-stream-stalls.md; sha256 {sha}",
        "",
        "## Rollback (a)",
    ]
    summary: dict[str, Any] = {"h": h, "now": now, "sha256": sha}
    since_h = w.clean_hours(journal, h, int(now) // w.HOUR * w.HOUR + w.HOUR, now)
    a = w.rollback_a_hours(journal, since_h)
    summary["a_hours"] = {k: a[k] for k in ("hours", "min_ratio", "low", "exempt_hours", "triggered", "shared_incident")}
    mins = ", ".join(f"{k} {v:.3f}" if v is not None else f"{k} -" for k, v in a["min_ratio"].items())
    out.append(f"- clean hours from H: {len(since_h)} ({_hours(since_h)})")
    out.append(f"- min connected_s ratio (liquidations vs binance, books vs binance:depth): {mins}; threshold < {w.RATIO_MIN}")
    if a["low"]:
        out.append("- below threshold: " + "; ".join(f"{_t(x)} {s} {r:.3f}" for x, s, r in a["low"]))
        out.append(
            f"- Binance in those hours: {_r(a['binance_in_flagged'])}; exempt hours (Binance > {w.CONTROL_RATE}/h in that hour): "
            f"{_hours(a['exempt_hours'])}"
        )
    if a["no_reference"]:
        out.append(f"- hours without a reference row (not checked): {len(a['no_reference'])}")
    out.append(f"- journal part triggered: {a['triggered']}")
    if health is None:
        out.append("- system_health: skipped (--no-health)")
        ah = None
    elif health[0] is None:
        out.append(f"- system_health: NOT READ ({health[1]}); consecutive_failures and peer unchecked")
        ah = None
    else:
        streams = w.health_streams(health[0])
        hour = int(now) // w.HOUR * w.HOUR
        ctrl = w.rate(journal.get(w.LIQ_REF, {}), [hour - w.HOUR, hour])
        ah = w.rollback_a_health(streams, ctrl["rate"])
        out.append(f"- system_health from {health[1]}")
        for n, s in streams.items():
            out.append(f"  - {n}: " + ("absent" if s is None else f"consecutive_failures {s['consecutive_failures']}, peer {s['peer']}"))
        out.append(
            f"  - deploy confirmed (peer present, no ':'): {ah['deploy_confirmed']}"
            + ("" if ah["deploy_confirmed"] else "; H may have to move (rule)")
        )
        out.append(
            f"  - consecutive_failures >= {w.HEALTH_MAX_CONSECUTIVE}: {ah['consecutive_hits'] or 'none'}; health part triggered: {ah['triggered']}"
        )
    summary["a_health"] = ah
    b = w.rollback_b(journal, h, now)
    summary["b"] = {k: b[k] for k in ("ready", "per", "binance", "over", "certain", "triggered", "shared_incident")} | {"hours": b["hours"]}
    out += ["", "## Rollback (b)"]
    if b["ready"]:
        out.append(f"- the first {w.B_HOURS} clean hours from H + 2 h: {_hours(b['hours'])}")
    else:
        out.append(f"- PENDING: {len(b['hours'])} of {w.B_HOURS} clean hours from H + 2 h ({_hours(b['hours'])}); so far, not the check:")
    for t in w.TARGETS:
        out.append(f"  - {t}: {_r(b['per'][t])} (limit > {w.B_LIMITS[t]}/h)")
    out.append(f"  - binance: {_r(b['binance'])} (control <= {w.CONTROL_RATE}/h)")
    if b["ready"]:
        out.append(f"- triggered: {b['triggered']}" + (" (shared incident: Binance above control)" if b["shared_incident"] else ""))
    elif b["certain"]:
        out.append(f"- already above the 12-hour limit: {b['certain']}")
    parts = [
        name
        for name, hit in (("(a) journal", a["triggered"]), ("(a) system_health", bool(ah and ah["triggered"])), ("(b)", b["triggered"]))
        if hit
    ]
    rollback = bool(parts)
    summary["rollback"] = rollback
    summary["rollback_parts"] = parts
    summary["health_checked"] = ah is not None
    if rollback:
        line = f"ROLLBACK: TRIGGERED ({', '.join(parts)})"
    elif ah is None:
        line = "ROLLBACK: not triggered (system_health not read: (a) health part not checked)"
    else:
        line = "ROLLBACK: not triggered"
    out += ["", line, "", "## Full verdict"]
    if rollback:
        # Rule («Умова відкату»): after a rollback no window verdict is given, so no window is computed or printed.
        summary["verdict_state"] = "rollback"
        out.append("VERDICT: not issued (rollback triggered: after a rollback no window verdict is given)")
        return "\n".join(out) + "\n", summary
    v = w.verdict(journal, h, now)
    summary["verdict_state"] = v["state"]
    win = v["window"]
    for inv in v["invalid"]:
        out.append(
            f"- invalid window {_t(inv['start'])} .. {_t(inv['end'])}: Binance {_r(inv['binance'])}; new window from {_t(inv['new_start'])}"
        )
    if v["state"] == "wait":
        if win:
            out.append(
                f"- not yet: window from {_t(win['start'])}, {win['clean']} clean hours of {win['min_clean']} so far; "
                f"computed not before {_t(win['earliest'])} UTC and {win['min_clean']} clean hours"
            )
        return "\n".join(out) + "\n", summary
    summary["verdict"] = v
    for key in ("window", "extended"):
        x = v.get(key)
        if not x or not x["ready"]:
            continue
        out.append(f"- {x['length_hours']} h window {_t(x['start'])} .. {_t(x['end'])} UTC, {x['clean']} clean hours: {x['outcome']}")
        out.append(f"  - binance: {_r(x['binance'])}")
        for t in w.TARGETS:
            out.append(f"  - {t}: {_r(x['per'][t])}")
        for s, p in x["books"].items():
            out.append(f"  - {s} (descriptive): {_r(p)}")
    if v["state"] == "extend to 168 h":
        e = v["extended"]
        out.append(
            f"- between the thresholds: extend to 168 h; {e['clean']} of {e['min_clean']} clean hours, not before {_t(e['earliest'])} UTC"
        )
    out.append(
        "- this run sees only its own system_health reading: if any earlier reading after H had consecutive_failures "
        f">= {w.HEALTH_MAX_CONSECUTIVE} (rollback (a), not shared with Binance), the rollback stands and this verdict is void"
    )
    out.append(f"VERDICT: {v['state']}" + ("" if ah is not None else " (system_health not read: (a) health part not checked)"))
    return "\n".join(out) + "\n", summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB, help="collector DB; opened read-only")
    ap.add_argument("--h", default=DEFAULT_H, help="deploy hour H (ISO, UTC)")
    ap.add_argument("--now", default=None, help="ISO clock for a repeat of an earlier run (never later than now)")
    ap.add_argument("--no-health", action="store_true", help="do not read system_health of the running service")
    ap.add_argument("--url", default=DEFAULT_URL, help="the running MCP service")
    ap.add_argument("--out", default=None, help="also write the report here (a .json summary next to it)")
    a = ap.parse_args(argv)
    h = _ts(a.h)
    if h % w.HOUR:
        ap.error("--h must be the start of an hour")
    now = resolve_now(a.now, time.time())
    journal = load_journal(a.db, h - w.HOUR)
    health = None if a.no_health else check_store(read_health(a.url), a.db)
    text, summary = render(journal, h, now, health)
    sys.stdout.write(text)
    if a.out:
        p = Path(a.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        p.with_suffix(".json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
