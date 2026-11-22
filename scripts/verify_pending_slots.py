"""Resting S17 / S28 limits in the demo's 25-position cap (IDEA-20261030-demo-setups-depth, audit 17.11).

Registration: the card's section «Попередня реєстрація — лімітки S17 і S28 у ліміті 25 позицій». Reads the demo DB
read-only (``mode=ro``), never R or P&L columns; no network. Stages: ``smoke`` (row counts and interval integrity, no
outcome numbers) and ``run`` (place-days, signals stopped by the cap with a place freed, S17 fill by age).

    uv run python scripts/verify_pending_slots.py --stage smoke
    uv run python scripts/verify_pending_slots.py --stage run
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from common_helpers import connect_ro, rss_mb  # noqa: E402

from tradedesk.analytics import pending_slots as ps  # noqa: E402
from tradedesk.analytics.catalyst_scalp import wilson_ci  # noqa: E402
from tradedesk.analytics.sample_pace import WEEK_MS, poisson_ci, reason_category  # noqa: E402

DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
DEFAULT_OUT = Path.home() / ".cache/tradedesk-agents/pending-slots"
CAP = 25  # guard.max_open_positions
LIMIT_SETUPS = ("S17", "S28")
STRUCT = {"score_low", "stop_wide", "rr_low", "levels", "at_level"}
ACCOUNT_OTHER = {"day_caps", "risk_caps", "setup_slots", "bar_cap", "already_in", "universe", "size_zero", "other"}
DAY = ps.DAY_MS
LINES: list[str] = []


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp() * 1000)


W0 = ms("2026-10-31T00:00")
S28_SCORE1_MS = ms("2026-11-10T00:00")  # demo_trader.MIN_SCORE_BY_SETUP S28 = 1 from 10.11 (ADR 0007 addendum)
AUDIT_AT = ms("2026-11-17T18:00")  # known number of the 17.11 audit (exact boundary unknown)


def emit(s: str = "") -> None:
    print(s, flush=True)
    LINES.append(s)


def iso(t: int | None) -> str:
    return "—" if t is None else datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d %H:%M")


def ms_of(x: Any) -> int | None:
    if x is None:
        return None
    v = int(x)
    return v * 1000 if v < 100_000_000_000 else v


def min_score(setup: str, ts: int) -> int:
    return 1 if setup == "S28" and ts >= S28_SCORE1_MS else 2


def load(db: str, cutoff: int) -> dict[str, Any]:
    con = connect_ro(db)
    try:
        cols = "link_id, symbol, setup, timeframe, status, signal_ts, opened_ts, closed_ts, rest_until, score"
        trades = [dict(zip(cols.split(", "), r, strict=True)) for r in con.execute(f"SELECT {cols} FROM demo_trade")]
        first: dict[str, dict[str, int]] = defaultdict(dict)
        for ts, kind, lid in con.execute(
            "SELECT ts, kind, link_id FROM exec_audit WHERE ts < ? AND kind IN ('signal','sent','filled','missed','cancelled','expired') "
            "AND link_id IS NOT NULL ORDER BY ts",
            (cutoff,),
        ):
            first[lid].setdefault(kind, int(ts))
        decisions: dict[str, dict[str, Any]] = {}
        for ts, kind, lid, setup, payload in con.execute(
            "SELECT ts, kind, link_id, setup, payload FROM exec_audit WHERE ts < ? AND kind IN ('refused','skipped') "
            "AND link_id IS NOT NULL ORDER BY ts",
            (cutoff,),
        ):
            if lid in decisions:
                continue
            p = json.loads(payload or "{}")
            decisions[lid] = {
                "ts": int(ts),
                "kind": kind,
                "setup": setup,
                "violations": [str(v) for v in p.get("violations") or []],
                "score": p.get("score"),
            }
        return {"trades": trades, "first": first, "decisions": decisions, "con": con}
    except Exception:
        con.close()
        raise


def signal_rr(con: Any, lids: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for i in range(0, len(lids), 500):
        chunk = lids[i : i + 500]
        q = f"SELECT link_id, payload FROM exec_audit WHERE kind = 'signal' AND link_id IN ({','.join('?' * len(chunk))})"
        for lid, payload in con.execute(q, chunk):
            if lid in out:
                continue
            s = (json.loads(payload or "{}") or {}).get("signal") or {}
            try:
                e, st, tg = float(s["entry"]), float(s["stop"]), float(s["target"])
                out[lid] = abs(tg - e) / abs(e - st) if e != st else 0.0
            except (KeyError, TypeError, ValueError):
                out[lid] = 0.0
    return out


def intervals_of(trades: list[dict[str, Any]], first: dict[str, dict[str, int]], cutoff: int) -> tuple[dict[str, ps.Interval], int]:
    out: dict[str, ps.Interval] = {}
    none = 0
    for t in trades:
        f = first.get(t["link_id"], {})
        if t["status"] == "rejected" and "sent" not in f:
            continue  # registered: a rejected row without a sent order never rested
        iv = ps.pending_interval(f.get("sent"), f.get("signal"), ms_of(t["opened_ts"]), ms_of(t["closed_ts"]), cutoff)
        if iv is None:
            none += t["status"] != "rejected" and (ms_of(t["signal_ts"]) or 0) < cutoff
            continue
        out[t["link_id"]] = iv
    return out, none


def smoke(a: argparse.Namespace, cutoff: int) -> None:
    t0 = time.time()
    d = load(a.db, cutoff)
    d["con"].close()
    ivs, none = intervals_of(d["trades"], d["first"], cutoff)
    emit(
        f"cut-off {iso(cutoff)} UTC; рядків demo_trade {len(d['trades'])}; link_id з журналу {len(d['first'])}; рядків рішення {len(d['decisions'])}"
    )
    for s in LIMIT_SETUPS:
        st = Counter(t["status"] for t in d["trades"] if t["setup"] == s)
        n_iv = sum(1 for t in d["trades"] if t["setup"] == s and t["link_id"] in ivs)
        emit(f"{s}: рядків {sum(st.values())} {dict(sorted(st.items()))}; з інтервалом {n_iv}")
    bad = sum(1 for iv in ivs.values() if iv.end < iv.start)
    emit(f"інтервалів {len(ivs)}; кінець < початку: {bad}; рядків без sent / signal до cut-off: {none}")
    emit(f"час {time.time() - t0:.1f} с; peak RSS {rss_mb():.0f} MB")


def run(a: argparse.Namespace, cutoff: int) -> None:
    t0 = time.time()
    d = load(a.db, cutoff)
    trades, first, decisions, con = d["trades"], d["first"], d["decisions"], d["con"]
    ivs, none = intervals_of(trades, first, cutoff)
    by_lid = {t["link_id"]: t for t in trades}
    lim = {lid: iv for lid, iv in ivs.items() if by_lid[lid]["setup"] in LIMIT_SETUPS}
    lim_list = list(lim.values())
    days = (cutoff - W0) / DAY
    weeks = (cutoff - W0) / WEEK_MS
    emit(
        f"Вікно {iso(W0)} … {iso(cutoff)} UTC ({days:.2f} діб, {weeks:.2f} тижня); рядків без sent / signal: {none}; peak RSS {rss_mb():.0f} MB"
    )

    # 1. place-days
    emit("\n## 1. Місце-дні незаповнених лімітків (перетин з вікном)")
    cap_days = CAP * days
    for s in (*LIMIT_SETUPS, "S17+S28"):
        sel = [iv for lid, iv in lim.items() if s == "S17+S28" or by_lid[lid]["setup"] == s]
        sd = ps.slot_days(sel, W0, cutoff)
        emit(
            f"{s:8s} рядків з інтервалом у вікні {sum(1 for iv in sel if iv.end > W0)}; місце-днів {sd:.1f}; частка ємності 25 × {days:.1f} діб = {sd / cap_days:.1%}"
        )
    other = Counter()
    for lid, iv in ivs.items():
        s = by_lid[lid]["setup"]
        if s not in LIMIT_SETUPS:
            other[s] += ps.slot_days([iv], W0, cutoff)
    emit(
        "описово, очікування входу інших сетапів (місце-днів): "
        + ", ".join(f"{k} {v:.2f}" for k, v in sorted(other.items(), key=lambda x: -x[1]) if v >= 0.01)
    )
    emit("живі на cut-off лімітки S17 / S28:")
    for lid, iv in sorted(lim.items(), key=lambda x: x[1].start):
        if iv.end >= cutoff and not iv.filled:
            t = by_lid[lid]
            emit(
                f"  {t['setup']} {t['symbol'].split('/')[0]:10s} з {iso(iv.start)}, вік {(cutoff - iv.start) / DAY:.1f} доби, rest_until {iso(ms_of(t['rest_until']))}"
            )
    fills = [iv for iv in lim_list if iv.filled and W0 <= iv.end < cutoff]
    emit(
        f"заповнень лімітків S17 / S28 у вікні: {len(fills)} (S17 {sum(1 for l_, iv in lim.items() if iv in fills and by_lid[l_]['setup'] == 'S17')})"
    )

    # 2. evaluations
    evals_only: list[ps.Evaluation] = []
    cap_any = 0
    cap_other = Counter()
    sanity: list[int] = []
    cap_rows = [(lid, r) for lid, r in decisions.items() if W0 <= r["ts"] < cutoff and ps.parse_open_count(r["violations"]) is not None]
    rr = signal_rr(con, [lid for lid, _ in cap_rows])
    con.close()
    occupied = []  # (start, end, symbol) of a place held by a demo row: resting or open position
    for lid, t in by_lid.items():
        iv = ivs.get(lid)
        if iv is not None:
            occupied.append((iv.start, iv.end, t["symbol"]))
        op, cl = ms_of(t["opened_ts"]), ms_of(t["closed_ts"])
        if op is not None and op < cutoff:
            occupied.append((op, cl if cl is not None and cl < cutoff else cutoff, t["symbol"]))
    for lid, r in cap_rows:
        n_open = ps.parse_open_count(r["violations"]) or 0
        recon = len({sym for a_, b_, sym in occupied if a_ <= r["ts"] < b_})
        sanity.append(n_open - recon)
        cats = {reason_category(v) for v in r["violations"]} - {"decided"}
        setup = r["setup"] or ""
        if r["kind"] != "skipped" or cats & STRUCT or setup in LIMIT_SETUPS:
            continue
        if r["score"] is None or int(r["score"]) < min_score(setup, r["ts"]):
            continue
        cap_any += 1
        ev = ps.Evaluation(r["ts"], lid, n_open, int(r["score"]), rr.get(lid, 0.0))
        if cats == {"positions_max"}:
            evals_only.append(ev)
        else:
            for c in sorted(cats - {"positions_max"}):
                cap_other[c] += 1
    emit("\n## 2. Перевірки цілісності")
    dist = Counter(sanity)
    emit(
        f"рядків рішення з лімітом позицій у вікні (усі сетапи, усі види): {len(cap_rows)}; N з тексту − реконструкція з demo_trade: "
        + ", ".join(f"{k:+d}: {v}" for k, v in sorted(dist.items()))
        + f"; |різниця| > 2: {sum(v for k, v in dist.items() if abs(k) > 2)}"
    )
    snap = AUDIT_AT - 6 * 3_600_000
    act = [(by_lid[l_]["setup"], by_lid[l_]["symbol"].split("/")[0]) for l_, iv in ivs.items() if iv.start <= snap < iv.end]
    emit(f"очікують входу на {iso(snap)}: {len(act)} {sorted(act)}")
    wk = [e for e in evals_only if AUDIT_AT - 7 * DAY <= e.ts < AUDIT_AT]
    wk_any = sum(
        1
        for lid, r in cap_rows
        if AUDIT_AT - 7 * DAY <= r["ts"] < AUDIT_AT
        and r["kind"] == "skipped"
        and not ({reason_category(v) for v in r["violations"]} & STRUCT)
    )
    emit(
        f"придатні, зупинені лише лімітом позицій, {iso(AUDIT_AT - 7 * DAY)} … {iso(AUDIT_AT)}: {len(wk)} (з іншими лімітами рахунку і без перевірки балу: {wk_any}); аудит 17.11: 139"
    )

    # 3. stopped and freed
    emit("\n## 3. Придатні оцінки інших сетапів, зупинені лімітом позицій")
    emit(
        f"зупинені лише лімітом позицій: {len(evals_only)}; з ним і ще іншим лімітом рахунку: {cap_any - len(evals_only)} ({dict(cap_other)})"
    )
    by_setup = Counter(by_lid.get(e.link_id, {}).get("setup") or decisions[e.link_id]["setup"] for e in evals_only)
    emit("за сетапом (лише ліміт): " + ", ".join(f"{k} {v}" for k, v in sorted(by_setup.items(), key=lambda x: -x[1])))
    holds = sorted(
        (ms_of(t["closed_ts"]) or 0) - (ms_of(t["opened_ts"]) or 0)
        for t in trades
        if t["opened_ts"] is not None
        and W0 <= (ms_of(t["opened_ts"]) or 0)
        and t["closed_ts"] is not None
        and (ms_of(t["closed_ts"]) or 0) < cutoff
    )
    h = int(statistics.median(holds)) if holds else 0
    emit(f"H = медіана утримання закритих угод, відкритих у вікні: {h / 3_600_000:.1f} год (n {len(holds)})")
    variants = [
        ("A верхня межа", ps.freed_upper(evals_only, lim_list, CAP)),
        ("B цикл, без утримання", ps.admit_freed(evals_only, lim_list, CAP, None)),
        ("C основний, утримання H", ps.admit_freed(evals_only, lim_list, CAP, h)),
        ("C×0.5", ps.admit_freed(evals_only, lim_list, CAP, h // 2)),
        ("C×2", ps.admit_freed(evals_only, lim_list, CAP, 2 * h)),
        ("D утримання до cut-off", ps.admit_freed(evals_only, lim_list, CAP, cutoff)),
    ]
    emit(f"{'варіант':24s} {'n':>5s} {'на тиждень':>11s} {'95 % CI Пуассона':>20s}")
    res: dict[str, tuple[int, float, float, float]] = {}
    for name, got in variants:
        lo, hi = poisson_ci(len(got))
        res[name] = (len(got), len(got) / weeks, lo / weeks, hi / weeks)
        emit(f"{name:24s} {len(got):5d} {len(got) / weeks:11.2f} [{lo / weeks:6.2f}; {hi / weeks:6.2f}]")
    c_ids = set(variants[2][1])
    per_day = Counter(iso(e.ts)[:10] for e in evals_only if e.link_id in c_ids)
    emit("C за днями: " + ", ".join(f"{k[5:]} {v}" for k, v in sorted(per_day.items())))
    c_setup = Counter(decisions[lid]["setup"] for lid in c_ids)
    emit("C за сетапом: " + ", ".join(f"{k} {v}" for k, v in sorted(c_setup.items(), key=lambda x: -x[1])))
    cycles = {e.ts // 300_000 for e in evals_only}
    cyc_p = {e.ts // 300_000 for e in evals_only if ps.count_active(lim_list, e.ts) >= 1}
    emit(f"циклів із зупиненими лише лімітом: {len(cycles)}; з них з ≥ 1 лімітки S17 / S28 в очікуванні: {len(cyc_p)}")

    # 4. fill by age
    emit("\n## 4. Заповнення лімітків за віком (початок інтервалу у вікні)")
    for s, ages in (("S17", (1, 3, 7, 14, 30)), ("S28", (1 / 6, 1, 3, 7))):
        sel = [iv for lid, iv in lim.items() if by_lid[lid]["setup"] == s and W0 <= iv.start < cutoff]
        filled_age = sorted((iv.end - iv.start) / DAY for iv in sel if iv.filled)
        emit(f"{s}: лімітків {len(sel)}, заповнено {len(filled_age)}; вік заповнення, доби: {[round(x, 2) for x in filled_age]}")
        for r in ps.fill_by_age(sel, [int(x * DAY) for x in ages], cutoff):
            n = r["filled"] + r["unfilled"]
            lo, hi = wilson_ci(r["filled"], n)
            share = r["filled"] / n if n else math.nan
            mark = "" if n >= 10 else "  ◯ n < 10"
            emit(
                f"  вік ≤ {r['age_ms'] / DAY:5.2f} д: заповнено {r['filled']} з {n} ({share:.0%}) [{lo:.0%}; {hi:.0%}], цензуровано {r['censored']}{mark}"
            )

    # 5. verdict
    emit("\n## 5. Вердикт за реєстрацією (варіант C)")
    k, rate, lo, hi = res["C основний, утримання H"]
    if len(evals_only) < 10:
        v = "◯ ліміт позицій майже не зупиняв придатних оцінок (n < 10)"
    elif lo >= 1:
        v = "✓ лімітки S17 / S28 витісняють помітну кількість придатних входів"
    elif hi < 1:
        v = "✗ помітного витіснення немає"
    else:
        v = "≈ невизначено"
    emit(f"C: {k} входів, {rate:.2f} / тиждень [{lo:.2f}; {hi:.2f}] → {v}")
    emit(f"час {time.time() - t0:.1f} с; peak RSS {rss_mb():.0f} MB")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", choices=("smoke", "run"), required=True)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--cutoff", default="2026-11-18T18:00", help="registered cut-off, UTC ISO")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    cutoff = ms(a.cutoff)
    (smoke if a.stage == "smoke" else run)(a, cutoff)
    a.out_dir.mkdir(parents=True, exist_ok=True)
    (a.out_dir / f"{a.stage}.txt").write_text("\n".join(LINES) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
