"""Liquidation stream gaps: real holes or quiet hours? (standing-improvements card, item «дірки у стрімах ліквідацій»).

    uv run python scripts/liquidation_gaps_report.py [--db PATH] [--cutoff ISO] [--min-ref N] [--top K]
        [--window-seconds W] [--restart-seconds R] [--out PATH]

Reads the ``liquidations`` table through a READ-ONLY connection (``mode=ro``: no migration, no write, no network).
For each target stream (Bybit, OKX) it compares the rows with a reference stream on Binance USD-M, split before / after
``--cutoff`` (default: the commit that added docs/changes/2026-11-09-liquidation-stream-reconnect.md, i.e. the
reconnect/backoff change; the server ran it some minutes later, after the merge and the deploy):

- per full UTC hour: rows of the target and of the reference; an hour with 0 target rows while the reference has
  ``>= N`` rows is flagged as a probable real hole (N = ``--min-ref``, default the median hourly reference count over
  the whole window); zero hours below N are listed as "quiet or unclear";
- the largest pauses between consecutive target rows, each with the reference rows inside it, the target rows the
  window-wide target / reference ratio expects there, the Poisson P(0 rows | expected) and the rows of all Binance
  symbols inside it (0 there = the whole collector was down, e.g. a service restart, not a stream hole);
- long pauses (``--okx-pause-min`` / ``--bybit-pause-min``) before vs after: the count after the cutoff against the
  count the pre-cutoff rate predicts for the same market activity (hours stratified by the quartiles of the hourly
  reference count before the cutoff; a pause belongs to the hour it starts in), with the Poisson P(count <= observed);
- quiet hours (reference below its pre-cutoff lower quartile): zero-target hours before vs after, and how many quiet
  hours after the cutoff without a zero hour would reject the pre-cutoff zero rate (P < 0.05). If the fix removed
  real holes, quiet hours after it stop being empty; if they stay empty at the old rate, the market was quiet;
- zero hours the collector's connection journal covers (``liquidation_stream_hour``, schema 40, from the first FULL
  hour after its first row: the service started inside that one): the connected time against the time the collector
  is meant to be connected, E = 3600 * W / (W + R) s an hour (stream windows of W = ``--window-seconds``, default 600
  as deployed, then a pause of R = ``--restart-seconds``, default 5, so a perfect link never reaches 3600 s), and the
  frames received; >= 90 % of E with frames = "quiet market", < 50 % of E = "drop", else "unclear". Each such line
  shows both the share of the hour and the share of E. A covered hour without a journal row had no connection (0 %).
  Without the table or before its first full hour the report is as before.

The reference for Bybit is Binance on the same symbols (the Bybit job subscribes only to ``--bybit-symbols``); for OKX
(every SWAP) it is Binance on the symbols OKX also lists (``BTC-USDT-SWAP`` -> ``BTCUSDT``), so Binance-only coins do
not inflate it. Caveats: Binance ``!forceOrder@arr`` and OKX ``liquidation-orders`` send at most one liquidation per
instrument per second, so counts are lower bounds; liquidations cluster, so the Poisson P(0) of a single pause is far
too small (a quiet-market explanation is likelier than it says); the long-pause test only compares rates, it does not
say which single pause was a hole. Output: ``--out`` or stdout.
"""

from __future__ import annotations

import argparse
import bisect
import math
import sqlite3
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

from tradedesk.config import Settings

DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
# Commit 33844e53 (author time) that added docs/changes/2026-11-09-liquidation-stream-reconnect.md; merged as PR #949
# at 21:22:30 UTC the same evening.
DEFAULT_CUTOFF = "2026-11-09T21:18:59+00:00"
REFERENCE = "binanceusdm"
BYBIT_SYMBOLS = "BTCUSDT,ETHUSDT,SOLUSDT"
H = 3600
# Collector connection journal (schema 40): a zero-row hour connected >= QUIET_SHARE of the expected time with frames =
# quiet market, < DROP_SHARE = drop.
QUIET_SHARE, DROP_SHARE = 0.9, 0.5
# The collector's cycle: a stream window of W s, then the PeriodicJob pause of R s before the next one, so even a perfect
# link is connected at most 3600 * W / (W + R) s an hour. W as deployed (deploy/features.env
# TRADEDESK_COLLECT_LIQUIDATIONS_WINDOW_SECONDS; the code default is 60), R the code default (not overridden there).
DEFAULT_WINDOW_S = 600.0
DEFAULT_RESTART_S = float(Settings.collect_liquidations_restart_seconds)


def expected_connected_s(window_s: float, restart_s: float) -> float:
    """Seconds of an hour the collector is meant to be connected: 3600 * W / (W + R)."""
    return H * window_s / (window_s + restart_s)


EXPECTED_S = expected_connected_s(DEFAULT_WINDOW_S, DEFAULT_RESTART_S)


def load_ts(con: sqlite3.Connection, venue: str, symbols: tuple[str, ...] | None = None) -> list[int]:
    """Sorted row timestamps (unix s) of one venue, optionally only for ``symbols``."""
    if symbols:
        marks = ", ".join("?" * len(symbols))
        cur = con.execute(f"SELECT ts FROM liquidations WHERE venue = ? AND symbol IN ({marks}) ORDER BY ts", (venue, *symbols))
    else:
        cur = con.execute("SELECT ts FROM liquidations WHERE venue = ? ORDER BY ts", (venue,))
    return [int(r[0]) for r in cur]


def okx_common_symbols(con: sqlite3.Connection) -> tuple[str, ...]:
    """Binance symbols of the OKX instruments in the table (``BTC-USDT-SWAP`` -> ``BTCUSDT``) that Binance also has."""
    okx = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM liquidations WHERE venue = 'okx'")}
    binance = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM liquidations WHERE venue = ?", (REFERENCE,))}
    return tuple(sorted({s.removesuffix("-SWAP").replace("-", "") for s in okx} & binance))


def count_between(ts: list[int], a: int, b: int) -> int:
    """Rows of sorted ``ts`` strictly inside (a, b)."""
    return max(0, bisect.bisect_left(ts, b) - bisect.bisect_right(ts, a))


def hourly(ts: list[int], first_hour: int, end_hour: int) -> list[int]:
    """Row counts per UTC hour index (ts // 3600) for hours first_hour .. end_hour - 1."""
    return [bisect.bisect_left(ts, (h + 1) * H) - bisect.bisect_left(ts, h * H) for h in range(first_hour, end_hour)]


def pauses(ts: list[int], lo: int, hi: int) -> list[tuple[int, int]]:
    """(start, end) of every pause between consecutive rows whose first row lies in [lo, hi)."""
    i0, i1 = bisect.bisect_left(ts, lo), bisect.bisect_left(ts, hi)
    return [(ts[i], ts[i + 1]) for i in range(i0, min(i1, len(ts) - 1))]


def largest_gaps(ts: list[int], lo: int, hi: int, top: int) -> list[tuple[int, int]]:
    """The ``top`` longest pauses (start, end) between consecutive rows whose first row lies in [lo, hi)."""
    return sorted(pauses(ts, lo, hi), key=lambda p: (p[0] - p[1], p[0]))[:top]


def poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k) for X ~ Poisson(lam)."""
    if lam <= 0:
        return 1.0
    term = total = math.exp(-lam)
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return min(1.0, total)


def long_pause_test(target: list[int], ref: list[int], lo: int, cutoff: int, hi: int, min_minutes: float) -> dict:
    """Pauses >= ``min_minutes`` after the cutoff vs the count the pre-cutoff rate predicts at the same activity.

    Full hours of each period are put into four strata by the quartiles of the hourly reference count before the
    cutoff; a pause counts in the hour it starts in (pauses starting outside full hours are dropped)."""
    pre_h = range(-(-lo // H), cutoff // H)
    post_h = range(-(-cutoff // H), hi // H)
    pre_ref = hourly(ref, pre_h.start, pre_h.stop)
    post_ref = hourly(ref, post_h.start, post_h.stop)
    edges = statistics.quantiles(pre_ref, n=4) if len(pre_ref) >= 2 else []

    def stratum(x: int) -> int:
        return bisect.bisect_right(edges, x)

    def starts(a: int, b: int, hours: range) -> list[int]:
        return [p[0] // H for p in pauses(target, a, b) if p[1] - p[0] >= min_minutes * 60 and p[0] // H in hours]

    pre_s, post_s = starts(lo, cutoff, pre_h), starts(cutoff, hi, post_h)
    n_strata = len(edges) + 1
    hours_pre, hours_post, n_pre = [0] * n_strata, [0] * n_strata, [0] * n_strata
    pre_stratum = {h: stratum(x) for h, x in zip(pre_h, pre_ref, strict=True)}
    for s in pre_stratum.values():
        hours_pre[s] += 1
    for x in post_ref:
        hours_post[stratum(x)] += 1
    for h in pre_s:
        n_pre[pre_stratum[h]] += 1
    expected = sum(hours_post[s] * n_pre[s] / hours_pre[s] for s in range(n_strata) if hours_pre[s])
    return {
        "min_minutes": min_minutes,
        "pre": len(pre_s),
        "pre_hours": len(pre_h),
        "post": len(post_s),
        "post_hours": len(post_h),
        "expected_post": expected,
        "p_le": poisson_cdf(len(post_s), expected),
    }


def quiet_hours(target: list[int], ref: list[int], lo: int, cutoff: int, hi: int) -> dict:
    """Zero-target hours among quiet hours (reference below the pre-cutoff lower quartile), before vs after.

    ``needed`` = quiet hours after the cutoff without a single zero hour that would make the pre-cutoff zero rate
    unlikely ((1 - rate) ** needed < 0.05); None when there were no zero quiet hours before."""
    pre_h, post_h = (-(-lo // H), cutoff // H), (-(-cutoff // H), hi // H)
    pre_ref = hourly(ref, *pre_h)
    q = statistics.quantiles(pre_ref, n=4)[0] if len(pre_ref) >= 2 else 0.0
    res: dict = {"threshold": q}
    for key, (h0, h1) in (("pre", pre_h), ("post", post_h)):
        pairs = [(t, r) for t, r in zip(hourly(target, h0, h1), hourly(ref, h0, h1), strict=True) if r < q]
        res[key] = (len(pairs), sum(1 for t, _ in pairs if t == 0))
    n, z = res["pre"]
    rate = z / n if n else 0.0
    res["needed"] = math.ceil(math.log(0.05) / math.log(1 - rate)) if 0 < rate < 1 else None
    return res


def load_stream_hours(con: sqlite3.Connection, exchange: str) -> dict[int, tuple[float, int]]:
    """Hour index (ts // 3600) -> (seconds with an open connection, frames) from the collector's connection journal
    (``liquidation_stream_hour``, schema 40); {} when the DB has no such table yet."""
    try:
        cur = con.execute("SELECT hour_ts, connected_s, frames FROM liquidation_stream_hour WHERE exchange = ?", (exchange,))
    except sqlite3.OperationalError:  # no such table: a DB from before the journal
        return {}
    return {int(h) // H: (float(c), int(f)) for h, c, f in cur}


def classify(connected_s: float, frames: int, expected_s: float = EXPECTED_S) -> str:
    """A zero-row hour by its connection, against ``expected_s`` (the seconds the collector is meant to be connected,
    ``expected_connected_s``): >= 90 % of it connected with frames (heartbeat replies count) = quiet market, < 50 % =
    drop, anything between (or connected without a single frame) = unclear."""
    if connected_s >= QUIET_SHARE * expected_s and frames > 0:
        return "quiet market"
    if connected_s < DROP_SHARE * expected_s:
        return "drop"
    return "unclear"


def connection_notes(
    hours: list[int], journal: dict[int, tuple[float, int]] | None, expected_s: float = EXPECTED_S
) -> dict[int, tuple[float, float, int, str]]:
    """(share of the hour connected, share of ``expected_s``, frames, class) of each hour index in ``hours`` the journal
    covers. Coverage starts at the first FULL hour after the journal's first row (the service started inside that one);
    earlier hours are left out (reported as before); a covered hour without a row had no connection at all."""
    if not journal:
        return {}
    first = min(journal) + 1
    notes = {}
    for h in hours:
        if h >= first:
            c, f = journal.get(h, (0.0, 0))
            notes[h] = (min(1.0, c / H), min(1.0, c / expected_s), f, classify(c, f, expected_s))
    return notes


def period(
    target: list[int],
    ref: list[int],
    ref_all: list[int],
    lo: int,
    hi: int,
    min_ref: int,
    top: int,
    ratio: float,
    journal: dict[int, tuple[float, int]] | None = None,
    expected_s: float = EXPECTED_S,
) -> dict:
    """Hourly and gap figures of one period [lo, hi) (unix s); ``journal``: ``load_stream_hours`` of the target,
    ``expected_s``: ``expected_connected_s`` of the collector that wrote it."""
    h0, h1 = -(-lo // H), hi // H  # full hours only
    t_h, r_h = hourly(target, h0, h1), hourly(ref, h0, h1)
    zero = [(h, r) for h, t, r in zip(range(h0, h1), t_h, r_h, strict=True) if t == 0]
    gaps = []
    for a, b in largest_gaps(target, lo, hi, top):
        n_ref = count_between(ref, a, b)
        exp_rows = ratio * n_ref
        gaps.append(
            {
                "start": a,
                "minutes": (b - a) / 60,
                "ref": n_ref,
                "expected": exp_rows,
                "p0": math.exp(-exp_rows),
                "all": count_between(ref_all, a, b),
            }
        )
    return {
        "hours": h1 - h0,
        "target_rows": sum(t_h),
        "ref_rows": sum(r_h),
        "zero_hours": len(zero),
        "flagged": [(h, r) for h, r in zero if r >= min_ref],
        "quiet": [(h, r) for h, r in zero if r < min_ref],
        "gaps": gaps,
        "conn": connection_notes([h for h, _ in zero], journal, expected_s),
    }


def analyse(
    target: list[int],
    ref: list[int],
    ref_all: list[int],
    lo: int,
    hi: int,
    cutoff: int,
    min_ref: int | None,
    top: int,
    pause_min: float,
    journal: dict[int, tuple[float, int]] | None = None,
    expected_s: float = EXPECTED_S,
) -> dict:
    """Both periods of one target stream; ``min_ref`` None = median hourly reference count over [lo, hi)."""
    ref_h = hourly(ref, -(-lo // H), hi // H)
    if min_ref is not None:
        n = int(min_ref)
    else:
        n = max(1, round(statistics.median(ref_h))) if ref_h else 1
    t_rows, r_rows = count_between(target, lo - 1, hi), count_between(ref, lo - 1, hi)
    ratio = t_rows / r_rows if r_rows else 0.0
    return {
        "min_ref": n,
        "ratio": ratio,
        "pre": period(target, ref, ref_all, lo, cutoff, n, top, ratio, journal, expected_s),
        "post": period(target, ref, ref_all, cutoff, hi, n, top, ratio, journal, expected_s),
        "long": long_pause_test(target, ref, lo, cutoff, hi, pause_min),
        "quiet": quiet_hours(target, ref, lo, cutoff, hi),
    }


def _t(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def _conn(note: tuple[float, float, int, str] | None) -> str:
    """Suffix of a zero-hour line: its connection from the journal (empty without journal data)."""
    if note is None:
        return ""
    share, of_expected, frames, kind = note
    return f"; connected {share:.0%} of the hour ({of_expected:.0%} of the expected), frames {frames}: {kind}"


def render(
    results: dict[str, dict], lo: int, hi: int, cutoff: int, labels: dict[str, str], cycle: tuple[float, float] | None = None
) -> str:
    """The report; ``cycle`` = (window s, pause s) the connection shares are measured against."""
    out = ["# Liquidation stream gaps", f"window {_t(lo)} .. {_t(hi)} UTC; cutoff (reconnect change) {_t(cutoff)} UTC"]
    if cycle and any(res["pre"].get("conn") or res["post"].get("conn") for res in results.values()):
        w, r = cycle
        out.append(
            f"connection journal: collector windows {w:g} s + pause {r:g} s -> expected connected "
            f"{expected_connected_s(w, r):.0f} s an hour; quiet market >= {QUIET_SHARE:.0%} of it with frames, drop < {DROP_SHARE:.0%}"
        )
    out.append("")
    for venue, res in results.items():
        n = res["min_ref"]
        out.append(f"## {venue} vs {labels[venue]}")
        out.append(
            f"target/reference row ratio {res['ratio']:.3f}; N = {n} reference rows/hour (expected target rows at N: {res['ratio'] * n:.1f})"
        )
        for key, name in (("pre", "before cutoff"), ("post", "after cutoff")):
            p = res[key]
            out.append(
                f"- {name}: {p['hours']} full hours, rows {p['target_rows']} (reference {p['ref_rows']}); "
                f"zero hours {p['zero_hours']}: probable holes (reference >= N) {len(p['flagged'])}, quiet or unclear {len(p['quiet'])}"
            )
            conn = p.get("conn") or {}
            if conn:
                kinds = [c[-1] for c in conn.values()]
                out.append(
                    f"  - connection journal ({len(conn)} of the zero hours): "
                    + ", ".join(f"{k} {kinds.count(k)}" for k in ("quiet market", "drop", "unclear"))
                )
            out.extend(f"  - hole? {_t(h * H)} reference {r}{_conn(conn.get(h))}" for h, r in p["flagged"])
            out.extend(f"  - quiet? {_t(h * H)} reference {r}{_conn(conn.get(h))}" for h, r in p["quiet"])
            out.extend(
                f"  - pause {g['minutes']:.0f} min from {_t(g['start'])}: reference {g['ref']}, expected {g['expected']:.1f}, "
                f"P(0) {g['p0']:.2g}, all Binance {g['all']}"
                for g in p["gaps"]
            )
        lp = res["long"]
        out.append(
            f"- pauses >= {lp['min_minutes']:g} min: before {lp['pre']} in {lp['pre_hours']} h; after {lp['post']} in {lp['post_hours']} h, "
            f"expected at the same activity {lp['expected_post']:.1f}, Poisson P(<= {lp['post']}) {lp['p_le']:.3g}"
        )
        qh = res["quiet"]
        (n0, z0), (n1, z1) = qh["pre"], qh["post"]
        need = f"; quiet hours after needed with no zero hour to reject the pre rate: {qh['needed']}" if qh["needed"] else ""
        out.append(
            f"- quiet hours (reference < {qh['threshold']:g}/hour, pre-cutoff lower quartile): before {n0}, zero {z0}; "
            f"after {n1}, zero {z1}{need}"
        )
        out.append("")
    return "\n".join(out)


def build(
    con: sqlite3.Connection,
    cutoff: int,
    min_ref: int | None,
    top: int,
    bybit_symbols: tuple[str, ...],
    bybit_pause: float,
    okx_pause: float,
    window_s: float = DEFAULT_WINDOW_S,
    restart_s: float = DEFAULT_RESTART_S,
) -> tuple[int, str]:
    """(exit code, report text) over the window all three venues cover; ``window_s`` / ``restart_s``: the collector's
    stream window and pause between windows, for the expected connected time of an hour."""
    ref_all = load_ts(con, REFERENCE)
    common = okx_common_symbols(con)
    streams = {
        "bybit": (
            load_ts(con, "bybit", bybit_symbols),
            load_ts(con, REFERENCE, bybit_symbols),
            f"{REFERENCE} {','.join(bybit_symbols)}",
            bybit_pause,
        ),
        "okx": (load_ts(con, "okx"), load_ts(con, REFERENCE, common), f"{REFERENCE}, {len(common)} symbols OKX also lists", okx_pause),
    }
    if not ref_all or any(not s[0] or not s[1] for s in streams.values()):
        return 1, "no liquidation rows for one of bybit / okx / binanceusdm (or no common symbols)\n"
    lo = max(ref_all[0], *(s[0][0] for s in streams.values()))
    hi = min(ref_all[-1], *(s[0][-1] for s in streams.values()))
    if not lo < cutoff < hi:
        return 1, f"cutoff {_t(cutoff)} is outside the common window {_t(lo)} .. {_t(hi)}\n"
    expected = expected_connected_s(window_s, restart_s)
    results = {
        v: analyse(t, r, ref_all, lo, hi, cutoff, min_ref, top, pm, load_stream_hours(con, v), expected)
        for v, (t, r, _, pm) in streams.items()
    }
    return 0, render(results, lo, hi, cutoff, {v: s[2] for v, s in streams.items()}, (window_s, restart_s))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--cutoff", default=DEFAULT_CUTOFF, help="ISO time (no offset = UTC); before/after split")
    ap.add_argument("--min-ref", type=int, default=None, help="reference rows/hour for a zero hour to count as a hole")
    ap.add_argument("--top", type=int, default=5, help="largest pauses per period")
    ap.add_argument("--bybit-symbols", default=BYBIT_SYMBOLS)
    ap.add_argument("--bybit-pause-min", type=float, default=60.0, help="long-pause threshold for Bybit, minutes")
    ap.add_argument("--okx-pause-min", type=float, default=8.0, help="long-pause threshold for OKX, minutes")
    ap.add_argument("--window-seconds", type=float, default=DEFAULT_WINDOW_S, help="collector stream window, s (as deployed: 600)")
    ap.add_argument("--restart-seconds", type=float, default=DEFAULT_RESTART_S, help="collector pause between windows, s (code default: 5)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    if a.window_seconds <= 0 or a.restart_seconds < 0:
        ap.error("--window-seconds must be > 0 and --restart-seconds >= 0")
    when = datetime.fromisoformat(a.cutoff)
    cutoff = int((when if when.tzinfo else when.replace(tzinfo=UTC)).timestamp())
    symbols = tuple(s.strip().upper() for s in a.bybit_symbols.split(",") if s.strip())
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    try:
        code, text = build(con, cutoff, a.min_ref, a.top, symbols, a.bybit_pause_min, a.okx_pause_min, a.window_seconds, a.restart_seconds)
    finally:
        con.close()
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
