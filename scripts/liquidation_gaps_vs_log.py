"""Liquidation pauses against the stream failure log: drops or a quiet market? (standing-improvements card, item
«дірки у стрімах ліквідацій», server data of 2026-11-12).

    uv run python scripts/liquidation_gaps_vs_log.py --gaps GAPS.csv --log WARNINGS.log [--out PATH]
        [--venue bybit] [--min-minutes 30] [--tol-seconds 120] [--since ISO] [--log-from ISO] [--log-to ISO]

Inputs (exported on the server; nothing is read from the network or the DB):

- ``--gaps``: CSV ``venue,gap_start_utc,gap_end_utc,minutes,rows_hour_before,rows_hour_after``, one row per pause
  between consecutive rows of the ``liquidations`` table (times UTC, ``YYYY-MM-DD HH:MM:SS``);
- ``--log``: journal lines of ``tradedesk.collector`` warnings ``liquidation stream <url> failed (N in a row): <error>``,
  each starting with an ISO time with offset (``journalctl -o short-iso``).

For every pause of ``--venue`` at least ``--min-minutes`` long that lies inside the log span (``--log-from`` ..
``--log-to``, default first .. last log line; and not before ``--since``) the script counts the venue's logged failures
inside it (widened by ``--tol-seconds``), whether one falls within the tolerance of its start or of its end, and how
much of the pause the logged outages can explain: a burst of failures (each within BURST_SECONDS of the previous) is
taken as a dead link from DETECT_SECONDS before its first line (keepalive: ping every 20 s, pong timeout 30 s) to the
reconnect after its last one (backoff min(60, 2^(n-1)) s + 10 s handshake), an upper bound. A pause is

- ``drop``: the logged outages cover >= half of it;
- ``edge``: less, but it ended within the tolerance of a logged failure (a stalled link that a reconnect cleared is
  possible; so is chance, see the baseline);
- ``quiet``: neither; the log says the link was up (or reconnected within about a minute) for most of the pause.

Baselines: the share of pauses with a failure near their end that the venue's failure rate gives by chance, and the
failures inside pauses against rate x pause time. For every venue in either file: logged failures, failures shared
with another venue within SHARED_SECONDS, and pauses >= ``--min-minutes``. Limits: the log names failures, not
reconnects or the collector's window restarts (every ``TRADEDESK_COLLECT_LIQUIDATIONS_WINDOW_SECONDS``), so a link that
stayed open but delivered nothing would read as ``quiet`` here; only the hourly connection journal
(``liquidation_stream_hour``: connected time and frames) separates that case. Output: ``--out`` or stdout.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Keepalive: the client pings every 20 s and gives up 30 s later (providers/liquidations.py PING_INTERVAL/PING_TIMEOUT).
DETECT_SECONDS = 50.0
# Reconnect after the n-th failure in a row: backoff min(60, 2^(n-1)) s (upper end of the jitter) + 10 s handshake.
BACKOFF_CAP = 60.0
OPEN_TIMEOUT = 10.0
BURST_SECONDS = 120.0  # failures of one venue closer than this are one outage
SHARED_SECONDS = 10.0  # a failure of another venue this close counts as shared
DROP_SHARE = 0.5

_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:[+-]\d\d:?\d\d|Z))\s+\S*?(?:\[(?P<pid>\d+)\])?:?\s.*?"
    r"liquidation stream (?P<url>\S+) failed \((?P<n>\d+) in a row\): (?P<err>\w+)"
)


@dataclass(frozen=True)
class Event:
    ts: float
    venue: str
    in_a_row: int
    error: str
    pid: str | None = None


@dataclass(frozen=True)
class Gap:
    venue: str
    start: float
    end: float
    rows_before: float | None
    rows_after: float | None

    @property
    def minutes(self) -> float:
        return (self.end - self.start) / 60.0


def venue_of(url: str) -> str:
    u = url.lower()
    if "bybit" in u:
        return "bybit"
    if "okx" in u:
        return "okx"
    if "binance" in u:
        return "binanceusdm"
    return u


def parse_time(text: str) -> float:
    """ISO time to epoch seconds; no offset = UTC."""
    t = datetime.fromisoformat(text.strip().replace(" ", "T").replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.timestamp()


def fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S")


def parse_log(lines) -> list[Event]:
    out = []
    for line in lines:
        m = _LINE.search(line)
        if m:
            out.append(Event(parse_time(m["ts"]), venue_of(m["url"]), int(m["n"]), m["err"], m["pid"]))
    return sorted(out, key=lambda e: e.ts)


def _num(text: str | None) -> float | None:
    try:
        return float(text) if text not in (None, "") else None
    except ValueError:
        return None


def parse_gaps(rows) -> list[Gap]:
    out = []
    for r in rows:
        out.append(
            Gap(
                r["venue"].strip(),
                parse_time(r["gap_start_utc"]),
                parse_time(r["gap_end_utc"]),
                _num(r.get("rows_hour_before")),
                _num(r.get("rows_hour_after")),
            )
        )
    return sorted(out, key=lambda g: (g.venue, g.start))


def outages(events: list[Event]) -> list[tuple[float, float]]:
    """Upper-bound dead-link intervals of one venue's failures (sorted), bursts merged."""
    out: list[tuple[float, float]] = []
    burst: list[Event] = []
    for e in [*events, None]:
        if e is not None and (not burst or e.ts - burst[-1].ts <= BURST_SECONDS):
            burst.append(e)
            continue
        if burst:
            last = burst[-1]
            out.append((burst[0].ts - DETECT_SECONDS, last.ts + min(BACKOFF_CAP, 2.0 ** (last.in_a_row - 1)) + OPEN_TIMEOUT))
        burst = [e] if e is not None else []
    return out


def covered(start: float, end: float, spans: list[tuple[float, float]]) -> float:
    """Seconds of [start, end] inside the union of ``spans``."""
    total, cursor = 0.0, start
    for a, b in sorted(spans):
        a, b = max(a, cursor), min(b, end)
        if b > a:
            total += b - a
            cursor = b
    return total


def classify(share: float, near_end: bool) -> str:
    if share >= DROP_SHARE:
        return "drop"
    return "edge" if near_end else "quiet"


def analyse(
    gaps: list[Gap], events: list[Event], *, venue: str, min_minutes: float, tol: float, since: float | None, log_from: float, log_to: float
) -> dict:
    ev = [e for e in events if e.venue == venue]
    spans = outages(ev)
    lo = max(log_from, since) if since is not None else log_from
    sel = [g for g in gaps if g.venue == venue and g.minutes >= min_minutes and g.start >= lo and g.end <= log_to]
    rows = []
    for g in sel:
        inside = [e for e in ev if g.start - tol <= e.ts <= g.end + tol]
        near_start = any(abs(e.ts - g.start) <= tol for e in inside)
        near_end = any(abs(e.ts - g.end) <= tol for e in inside)
        share = covered(g.start, g.end, spans) / (g.end - g.start) if g.end > g.start else 0.0
        rows.append(
            {
                "gap": g,
                "events": len(inside),
                "max_in_a_row": max((e.in_a_row for e in inside), default=0),
                "near_start": near_start,
                "near_end": near_end,
                "share": share,
                "class": classify(share, near_end),
            }
        )
    span_h = max(log_to - log_from, 0.0) / 3600.0
    n_span = sum(1 for e in ev if log_from <= e.ts <= log_to)
    rate = n_span / span_h if span_h > 0 else 0.0
    gap_h = sum(r["gap"].minutes for r in rows) / 60.0
    # Failures strictly inside the selected pauses (no tolerance), against rate x pause time.
    inside_strict = sum(1 for r in rows for e in ev if r["gap"].start < e.ts < r["gap"].end)
    p_end = 1.0 - math.exp(-rate * 2 * tol / 3600.0)
    return {
        "venue": venue,
        "rows": rows,
        "failures": n_span,
        "rate_per_h": rate,
        "span_h": span_h,
        "gap_h": gap_h,
        "inside": inside_strict,
        "inside_expected": rate * gap_h,
        "near_end": sum(r["near_end"] for r in rows),
        "near_end_expected": p_end * len(rows),
        "near_start": sum(r["near_start"] for r in rows),
        "classes": {c: sum(r["class"] == c for r in rows) for c in ("drop", "edge", "quiet")},
        "with_events": sum(r["events"] > 0 for r in rows),
        "covered_h": sum(r["share"] * r["gap"].minutes for r in rows) / 60.0,
    }


def venue_overview(gaps: list[Gap], events: list[Event], *, min_minutes: float, log_from: float, log_to: float) -> list[dict]:
    venues = sorted({g.venue for g in gaps} | {e.venue for e in events})
    out = []
    for v in venues:
        mine = [e for e in events if e.venue == v and log_from <= e.ts <= log_to]
        others = [e.ts for e in events if e.venue != v]
        shared = sum(1 for e in mine if any(abs(e.ts - t) <= SHARED_SECONDS for t in others))
        long_gaps = [g for g in gaps if g.venue == v and g.minutes >= min_minutes and g.start >= log_from and g.end <= log_to]
        out.append(
            {
                "venue": v,
                "failures": len(mine),
                "shared": shared,
                "long_gaps": len(long_gaps),
                "long_gap_h": sum(g.minutes for g in long_gaps) / 60.0,
            }
        )
    return out


def _median(xs: list[float]) -> str:
    return f"{statistics.median(xs):.0f}" if xs else "—"


def render(res: dict, overview: list[dict], *, min_minutes: float, tol: float, log_from: float, log_to: float, pids: int) -> str:
    v = res["venue"]
    rows = res["rows"]
    lines = [
        f"Liquidation pauses vs stream failure log, venue {v}",
        f"log span {fmt(log_from)} .. {fmt(log_to)} UTC ({res['span_h']:.1f} h), process ids in the log: {pids}",
        f"pauses >= {min_minutes:g} min inside the span: {len(rows)} ({res['gap_h']:.1f} h); tolerance {tol:g} s",
        "",
        "venue        failures  shared(<=10s)  pauses>=min  hours",
    ]
    for o in overview:
        lines.append(f"{o['venue']:<12} {o['failures']:>8}  {o['shared']:>13}  {o['long_gaps']:>11}  {o['long_gap_h']:>5.1f}")
    c = res["classes"]
    lines += [
        "",
        f"{v} failures: {res['failures']} ({res['rate_per_h']:.2f}/h)",
        f"pauses with any failure within [start-tol, end+tol]: {res['with_events']} of {len(rows)}",
        f"failures strictly inside pauses: {res['inside']} vs {res['inside_expected']:.1f} expected at the overall rate",
        f"a failure within tol of the pause end: {res['near_end']} vs {res['near_end_expected']:.1f} by chance; of the start: {res['near_start']}",
        f"pause time the logged outages can explain (upper bound): {res['covered_h']:.2f} h of {res['gap_h']:.1f} h",
        f"classes: drop {c['drop']}, edge {c['edge']}, quiet {c['quiet']}",
        f"rows/hour around the pauses, median: before {_median([r['gap'].rows_before for r in rows if r['gap'].rows_before is not None])}, "
        f"after {_median([r['gap'].rows_after for r in rows if r['gap'].rows_after is not None])}",
        "",
        "start UTC            end UTC              min    fails  max_row  start  end  covered  rows_b  rows_a  class",
    ]
    for r in rows:
        g = r["gap"]
        rb = "—" if g.rows_before is None else f"{g.rows_before:.0f}"
        ra = "—" if g.rows_after is None else f"{g.rows_after:.0f}"
        lines.append(
            f"{fmt(g.start)}  {fmt(g.end)}  {g.minutes:5.1f}  {r['events']:>5}  {r['max_in_a_row']:>7}  {'yes' if r['near_start'] else 'no':>5}  "
            f"{'yes' if r['near_end'] else 'no':>3}  {r['share']:>6.0%}  {rb:>6}  {ra:>6}  {r['class']}"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gaps", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--out")
    ap.add_argument("--venue", default="bybit")
    ap.add_argument("--min-minutes", type=float, default=30.0)
    ap.add_argument("--tol-seconds", type=float, default=120.0)
    ap.add_argument("--since", help="ISO time (no offset = UTC): ignore pauses that start earlier")
    ap.add_argument("--log-from", help="ISO time the log starts (default: its first line)")
    ap.add_argument("--log-to", help="ISO time the log ends (default: its last line)")
    a = ap.parse_args(argv)
    if a.min_minutes < 0 or a.tol_seconds < 0:
        ap.error("--min-minutes and --tol-seconds must be >= 0")
    events = parse_log(Path(a.log).read_text(encoding="utf-8", errors="replace").splitlines())
    with Path(a.gaps).open(encoding="utf-8", newline="") as f:
        gaps = parse_gaps(csv.DictReader(f))
    if not events and (a.log_from is None or a.log_to is None):
        ap.error("no failure lines in --log: pass --log-from and --log-to")
    log_from = parse_time(a.log_from) if a.log_from else events[0].ts
    log_to = parse_time(a.log_to) if a.log_to else events[-1].ts
    since = parse_time(a.since) if a.since else None
    res = analyse(gaps, events, venue=a.venue, min_minutes=a.min_minutes, tol=a.tol_seconds, since=since, log_from=log_from, log_to=log_to)
    overview = venue_overview(gaps, events, min_minutes=a.min_minutes, log_from=log_from, log_to=log_to)
    pids = len({e.pid for e in events if e.pid})
    text = render(res, overview, min_minutes=a.min_minutes, tol=a.tol_seconds, log_from=log_from, log_to=log_to, pids=pids)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
