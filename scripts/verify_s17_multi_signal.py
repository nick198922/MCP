"""S17 1d, several signals from one breakout candle: which sibling the passport chain takes and which the demo takes.

Registration: card docs/ideas/IDEA-20261030-demo-setups-depth.md, item «S17, кілька сигналів однієї свічки пробою»,
subsection «Попередня реєстрація (квант, 24.11 …)». Accounting, not an edge hypothesis. Read-only:

* passport cache ``~/.cache/tradedesk-agents/setup_passport/S17/cands/`` (1d rows, not rebuilt); the level and the
  close of the breakout candle are re-derived with the same ``setup_signals.s17_all`` on the same 1h -> 1d bars
  (``verify_setup_passport.load_1h_qv`` + ``resample``) and matched to the rows by (side, t_in, stop fraction);
  per-symbol results cached in ``--cache-dir`` (``levels/<sym>.json``);
* demo DB (``demo_trade``, ``exec_audit``) over a ``mode=ro`` SQLite connection; ``execution/`` is only imported for
  constants.

    uv run python scripts/verify_s17_multi_signal.py --stage smoke      # match check on 3 coins, no outcome metric
    uv run python scripts/verify_s17_multi_signal.py --stage run --out ~/.cache/tradedesk-agents/s17-multi-signal/run.txt
"""

from __future__ import annotations

import argparse
import json
import math
import resource
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import verify_setup_passport as vp  # noqa: E402

from tradedesk.analytics import s17_siblings as sib  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics import setup_signals as ss  # noqa: E402
from tradedesk.analytics.course_s14_ema import resample  # noqa: E402
from tradedesk.execution.demo_trader import DEFAULT_DB  # noqa: E402  (read-only constant)

HOME = Path.home() / ".cache/tradedesk-agents"
DAY_MS = vp.DAY_MS
LINES: list[str] = []


def emit(s: str = "") -> None:
    print(s, flush=True)
    LINES.append(s)


def rss() -> str:
    return f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB"


def levels_for(sym: str, cache: Path) -> list[dict[str, Any]]:
    """S17 1d signals of ``sym`` on the passport's bars: side, t_in, stop fraction, level and breakout close."""
    f = cache / "levels" / f"{sym}.json"
    if f.exists():
        return json.loads(f.read_text())
    h1, _ = vp.load_1h_qv(HOME / "course_s14_ema", HOME / "funding_crowding", sym)
    tb = resample(h1, vp.HOURS["1d"])
    out = []
    for sig in ss.s17_all(tb, "1d"):
        i = int(np.searchsorted(tb.t, sig.signal_ts))
        out.append(
            {
                "side": sig.side,
                "t_in": int(sig.signal_ts + tb.bar_ms),
                "stop_pct": abs(sig.entry - sig.stop) / abs(sig.entry),
                "level": float(sig.entry),
                "close": float(tb.c[i]),
            }
        )
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(out))
    return out


def load_passport() -> dict[str, np.ndarray]:
    vp.OUT["tf"] = "1d"
    T = vp.load_rows(HOME / "setup_passport" / "S17")
    T["day"] = T["t_in"] // DAY_MS
    T["key"] = sib.group_ids(list(zip(T["sym"].tolist(), T["side"].tolist(), T["t_in"].tolist(), strict=True)))
    T["pos"] = np.arange(T["t_in"].size)  # file order (the chain's tie order)
    return T


def masks(T: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    period = T["t_in"] >= vp.PASSPORT_MS
    demo = (T["rr"] >= vp.MIN_RR) & (T["stop_pct"] <= vp.MAX_STOP) & (T["score"] >= vp.MIN_SCORE)
    turn = np.nan_to_num(T["turn"], nan=-1) >= vp.SETUPS["S17"][2]
    elig = vp.s1.eligible_syms(HOME / "funding_crowding")
    qv = vp.s1.daily_qv(HOME / "course_s14_ema", HOME / "funding_crowding", elig)
    top_days = vp.top_n_days(qv, vp.top_n_of("S17"))
    E = period & demo & turn & vp.in_prev_day_top(T, top_days)
    chain_ = vp.chain(T, E)
    idx = np.flatnonzero(E)
    keep = sp.busy_chain([str(x) for x in T["sym"][idx]], T["t_in"][idx], T["B_x"][idx], vp.demo_rank(T)[idx])
    demo_ = np.zeros(E.size, dtype=bool)
    demo_[idx[keep]] = True
    return {"E": E, "chain": chain_, "demo": demo_, "OOS": T["t_in"] >= vp.OOS_MS, "period": period}


def attach_levels(T: dict[str, np.ndarray], need: set[str], cache: Path, limit: int | None = None) -> dict[str, int]:
    """Fill T['dist'] (breakout close -> level, fraction) for the rows of ``need`` symbols; match counts."""
    T["dist"] = np.full(T["t_in"].size, math.nan)
    T["level"] = np.full(T["t_in"].size, math.nan)
    cnt: Counter[str] = Counter()
    syms = sorted(need)[:limit] if limit else sorted(need)
    t0 = time.time()
    for n, s in enumerate(syms, 1):
        idx = np.flatnonzero(T["sym"] == s)
        idx = idx[np.argsort(T["pos"][idx])]
        sigs = levels_for(s, cache)
        m = sib.match_subsequence(
            [(str(T["side"][i]), int(T["t_in"][i]), float(T["stop_pct"][i])) for i in idx],
            [(g["side"], g["t_in"], g["stop_pct"]) for g in sigs],
        )
        for i, k in zip(idx, m, strict=True):
            if k < 0:
                cnt["unmatched"] += 1
                continue
            g = sigs[k]
            T["dist"][i] = sib.level_distance(str(T["side"][i]), g["level"], g["close"])
            T["level"][i] = g["level"]
            cnt["matched"] += 1
        if n % 25 == 0 or n == len(syms):
            emit(f"  levels {n}/{len(syms)} {time.time() - t0:.0f}s rss {rss()}")
    return dict(cnt)


def ci_s(v: np.ndarray, days: np.ndarray) -> str:
    ok = np.isfinite(v)
    r = sp.mean_ci(v[ok], days[ok], n_boot=vp.N_BOOT, seed=vp.SEED)
    if r["n"] == 0:
        return "0 | — | —"
    lo = "—" if math.isnan(r["lo"]) else f"{r['lo']:+.3f}"
    hi = "—" if math.isnan(r["hi"]) else f"{r['hi']:+.3f}"
    return f"{int(r['n'])} ({int(r['k'])} дн.) | {r['m']:+.3f} | [{lo}; {hi}]"


def rank_table(T: dict[str, np.ndarray], keep: np.ndarray, multi: np.ndarray, label: str) -> None:
    for plab, pm in (("2021+", np.ones_like(keep)), ("OOS", T["t_in"] >= vp.OOS_MS)):
        m = keep & multi & pm
        rk, sz, rs = T["rank_d"][m], T["size_E"][m], T["rank_stop"][m]
        n = int(m.sum())
        if n == 0:
            emit(f"| {label} | {plab} | 0 | | | | | |")
            continue
        c = Counter(min(int(x), 4) for x in rk)
        far = int(np.sum(rk == sz))
        emit(
            f"| {label} | {plab} | {n} | {c[1] / n:.1%} | {c[2] / n:.1%} | {c[3] / n:.1%} | {c[4] / n:.1%} | "
            f"{far / n:.1%} | {np.mean(rs == 1):.1%} | {int(np.sum(rk == 0))} |"
        )


def demo_part(db: str) -> None:
    emit("\n## (б) Розміщення демо S17 і «брати» того самого циклу (exec_audit 'signal', БД mode=ro)")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        trades = con.execute(
            "SELECT link_id, symbol, side, signal_ts, entry, stop, status FROM demo_trade WHERE setup = 'S17' ORDER BY signal_ts"
        ).fetchall()
        sigs = con.execute("SELECT ts, link_id, payload FROM exec_audit WHERE setup = 'S17' AND kind = 'signal' ORDER BY ts").fetchall()
        sent = {r["link_id"]: int(r["ts"]) for r in con.execute("SELECT ts, link_id FROM exec_audit WHERE setup = 'S17' AND kind = 'sent'")}
        con.close()
    except sqlite3.Error as e:
        emit(f"БД недоступна: {e} → (б) ◯")
        return
    emit(f"demo_trade S17: {len(trades)}; exec_audit signal S17: {len(sigs)}; sent: {len(sent)}")
    # cycles: signal rows of one link_id split where the gap exceeds 120 s
    by_link: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for r in sigs:
        by_link[r["link_id"]].append((int(r["ts"]), json.loads(r["payload"])))
    cycles: list[tuple[str, int, list[dict[str, Any]]]] = []
    for lid, xs in by_link.items():
        cur: list[dict[str, Any]] = []
        last = -(10**15)
        start = 0
        for ts, p in xs:
            if ts - last > 120_000 and cur:
                cycles.append((lid, start, cur))
                cur = []
            if not cur:
                start = ts
            cur.append(p)
            last = ts
        if cur:
            cycles.append((lid, start, cur))

    def sib_rows(ps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen, out = set(), []
        for p in ps:
            s = p.get("signal", {})
            e = float(s.get("entry", math.nan))
            if e in seen:
                continue
            seen.add(e)
            c = sib.parse_break_close(str(s.get("comment", "")))
            out.append(
                {
                    "entry": e,
                    "stop_pct": abs(e - float(s.get("stop", math.nan))) / abs(e),
                    "rr": float(p.get("proposal", {}).get("rr", math.nan)),
                    "dist": sib.level_distance(str(s.get("side")), e, c) if c else math.nan,
                }
            )
        return out

    emit(
        "| монета | бік | свічка (UTC) | статус | «братів» у циклі | ранг узятого за відстанню (1 = найближчий) | узятий = max R:R | відстані, % |"
    )
    emit("|---|---|---|---|---|---|---|---|")
    for t in trades:
        lid = t["link_id"]
        ts_sent = sent.get(lid)
        cands = [c for c in cycles if c[0] == lid and (ts_sent is None or c[1] <= ts_sent)]
        if not cands:
            emit(f"| {t['symbol']} | {t['side']} | {t['signal_ts']} | {t['status']} | — (немає сигналів у exec_audit) | ◯ | | |")
            continue
        rows = sib_rows(cands[-1][2])
        d = np.array([r["dist"] for r in rows])
        rk = sib.rank_within(np.zeros(len(rows), dtype=np.int64), d)
        j = int(np.argmin([abs(r["entry"] - float(t["entry"])) for r in rows]))
        rr = np.array([r["rr"] for r in rows])
        is_max = bool(rr[j] >= np.nanmax(rr) - 1e-12)
        day = time.strftime("%Y-%m-%d", time.gmtime(int(t["signal_ts"]) / 1000))
        emit(
            f"| {t['symbol']} | {t['side']} | {day} | {t['status']} | {len(rows)} | {int(rk[j])} з {len(rows)} | "
            f"{'так' if is_max else 'ні'} | {', '.join(f'{x:.1%}' for x in d)} |"
        )
    # every live cycle with siblings: where the demo rule (max R:R) sits by distance
    multi = [(lid, st, sib_rows(ps)) for lid, st, ps in cycles]
    multi = [(lid, st, r) for lid, st, r in multi if len(r) >= 2]
    ranks = []
    for _, _, r in multi:
        d = np.array([x["dist"] for x in r])
        rr = np.array([x["rr"] for x in r])
        if not np.isfinite(rr).any():
            continue
        rk = sib.rank_within(np.zeros(len(r), dtype=np.int64), d)
        ranks.append(
            (int(rk[int(np.nanargmax(rr))]), len(r), int(np.argmax(np.array([x["stop_pct"] for x in r])) == int(np.nanargmax(rr))))
        )
    if ranks:
        nn = len(ranks)
        emit(
            f"\nУсі цикли демо з ≥ 2 «братами» S17 (ключ = link_id + цикл): {nn}; «брат» з max R:R — найближчий у "
            f"{sum(1 for a, _, _ in ranks if a == 1) / nn:.0%}, найдальший у {sum(1 for a, b, _ in ranks if a == b) / nn:.0%}; "
            f"max R:R = найширший стоп % у {sum(c for _, _, c in ranks) / nn:.0%}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", choices=("smoke", "run"), default="smoke")
    ap.add_argument("--cache-dir", type=Path, default=HOME / "s17-multi-signal")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--db", default=DEFAULT_DB)
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    T = load_passport()
    sz_all = sib.group_sizes(T["key"])
    emit(
        f"S17 1d rows {T['t_in'].size}; keys with ≥ 2 signals {len(set(T['key'][sz_all >= 2].tolist()))}, "
        f"their rows {int((sz_all >= 2).sum())} (звірка: 2 113 / 5 754 з 9 328); rss {rss()}"
    )
    M = masks(T)
    E = M["E"]
    kE = np.where(E, T["key"], -1)
    size_E = np.zeros(E.size, dtype=np.int64)
    size_E[E] = sib.group_sizes(sib.group_ids(T["key"][E].tolist()))
    T["size_E"] = size_E
    multi = E & (size_E >= 2)
    emit(
        f"E (вхід ланцюга вердикту) {int(E.sum())} рядків; у ключах з ≥ 2 сигналами в E: {int(multi.sum())}, ключів {len(set(kE[multi].tolist()))}"
    )
    need = set(T["sym"][multi].tolist())
    if a.stage == "smoke":
        cnt = attach_levels(T, need, a.cache_dir, limit=3)
        emit(f"smoke: {len(need)} монет потребують рівнів; 3 монети за {time.time() - t0:.0f}s; зіставлення {cnt}; rss {rss()}")
        return
    cnt = attach_levels(T, need, a.cache_dir)
    nm = int(np.isfinite(T["dist"][multi]).sum())
    emit(
        f"зіставлення: {cnt}; рядків із кількома сигналами в E з відстанню {nm} з {int(multi.sum())} ({nm / max(1, int(multi.sum())):.1%})"
    )
    gE = np.where(multi, T["key"], -1)
    T["rank_d"] = np.zeros(E.size, dtype=np.int64)
    T["rank_stop"] = np.zeros(E.size, dtype=np.int64)
    idx = np.flatnonzero(multi)
    T["rank_d"][idx] = sib.rank_within(gE[idx], T["dist"][idx])
    T["rank_stop"][idx] = sib.rank_within(gE[idx], -T["stop_pct"][idx])  # 1 = widest stop
    F, OOS, day = T["filled"], M["OOS"], T["day"]

    emit("\n## (а) Ранг узятого рівня за відстанню від закриття свічки пробою (ключі з ≥ 2 сигналами в E)")
    emit("| правило | період | ключів узято | ранг 1 (найближчий) | 2 | 3 | ≥ 4 | найдальший | найширший стоп | без відстані |")
    emit("|---|---|---|---|---|---|---|---|---|---|")
    rank_table(T, M["chain"], multi, "ланцюг паспорта")
    rank_table(T, M["demo"], multi, "правило демо (max R:R)")
    for plab, pm in (("2021+", np.ones_like(E)), ("OOS", OOS)):
        r = sib.pick_comparison(gE[multi & pm], M["chain"][multi & pm], M["demo"][multi & pm])
        share = r["differ"] / max(1, r["both"])
        emit(f"{plab}: вибір різний у {r['differ']} з {r['both']} ключів ({share:.1%}); {r}")
    dist_m = T["dist"][multi & OOS]
    emit(
        f"відстань рівня OOS (усі «брати»): медіана {np.nanmedian(dist_m):.1%}, p10 {np.nanpercentile(dist_m, 10):.1%}, p90 {np.nanpercentile(dist_m, 90):.1%}"
    )

    emit("\n## (в) OOS, усі «брати» в E як незалежні лімітки: найближчий / не найближчий (описово, CI кластерами за днем)")
    emit("| група | розміщень | частка заповнення | заповнені: n (дн.) | E[R] net | 95 % CI | на розміщення: n (дн.) | R | 95 % CI |")
    emit("|---|---|---|---|---|---|---|---|---|")
    single = E & (size_E == 1)
    r0 = np.where(F, np.nan_to_num(T["B"], nan=0.0), 0.0)
    for plab, pm in (("OOS", OOS), ("2021+", np.ones_like(E))):
        for lab, m in (
            ("найближчий", multi & (T["rank_d"] == 1)),
            ("не найближчий", multi & (T["rank_d"] > 1)),
            ("одиночні сигнали", single),
            ("ланцюг паспорта (V_chain)", M["chain"]),
            ("правило демо (V_demo)", M["demo"]),
        ):
            mm = m & pm
            n = int(mm.sum())
            if n == 0:
                continue
            emit(f"| {lab}, {plab} | {n} | {F[mm].mean():.1%} | {ci_s(T['B'][mm & F], day[mm & F])} | {ci_s(r0[mm], day[mm])} |")

    emit("\n## Різниця V_demo − V_chain, OOS (кластери за днем)")
    d_r = sp.diff_ci(np.nan_to_num(T["B"], nan=0.0), M["demo"] & F & OOS, M["chain"] & F & OOS, day, n_boot=vp.N_BOOT, seed=vp.SEED)
    d_f = sp.diff_ci(F.astype(float), M["demo"] & OOS, M["chain"] & OOS, day, n_boot=vp.N_BOOT, seed=vp.SEED)
    emit(
        f"ΔE[R] заповнені {d_r['d']:+.3f} [{d_r['lo']:+.3f}; {d_r['hi']:+.3f}]; Δ частки заповнення {d_f['d']:+.1%} [{d_f['lo']:+.1%}; {d_f['hi']:+.1%}]"
    )

    emit("\n## (г) Смуги r_band n 30 / 50 / 100 (заповнені OOS у часі; i.i.d. / ковзна; смуга рішення)")
    emit("| вибірка | n OOS | E[R] | n | i.i.d. p5 … p95 | ковзна p5 … p95 | смуга рішення |")
    emit("|---|---|---|---|---|---|---|")
    bands: dict[str, dict[int, tuple[float, float]]] = {}
    for lab, k in (("V_chain (паспорт)", "chain"), ("V_demo (правило демо)", "demo")):
        ii = np.flatnonzero(M[k] & F & OOS)
        ii = ii[np.argsort(T["t_in"][ii], kind="stable")]
        v = T["B"][ii]
        v = v[np.isfinite(v)]
        bands[k] = {}
        for n in (30, 50, 100):
            b = sp.r_band(v, n, reps=vp.N_BOOT, seed=vp.SEED)
            lo, hi = min(b["iid_p5"], b["roll_p5"]), max(b["iid_p95"], b["roll_p95"])
            bands[k][n] = (lo, hi)
            emit(
                f"| {lab} | {v.size} | {v.mean():+.3f} | {n} | {b['iid_p5']:+.3f} … {b['iid_p95']:+.3f} | "
                f"{b['roll_p5']:+.3f} … {b['roll_p95']:+.3f} | {lo:+.3f} … {hi:+.3f} |"
            )
    r = sib.pick_comparison(gE[multi & OOS], M["chain"][multi & OOS], M["demo"][multi & OOS])
    share = r["differ"] / max(1, r["both"])
    shift = max(abs(bands["demo"][30][0] - bands["chain"][30][0]), abs(bands["demo"][30][1] - bands["chain"][30][1]))
    big = abs(d_r["d"]) >= 0.05 or abs(d_f["d"]) >= 0.05
    verdict = "✓" if share < 0.10 else ("✗" if big else "≈")
    if nm < 0.99 * int(multi.sum()):
        verdict = "◯ (зіставлено < 99 %)"
    emit(
        f"\nВердикт за реєстрацією: частка різного вибору OOS {share:.1%} (поріг 10 %), |ΔE[R]| {abs(d_r['d']):.3f} (0.05R), "
        f"|Δ заповнення| {abs(d_f['d']):.1%} (5 п. п.) → {verdict}; зсув краю смуги рішення n 30 {shift:.3f}R "
        f"(питання власнику, якщо ✗ або ≥ 0.05R); рядок у CHECKPOINT: {'так' if share >= 0.10 else 'ні'}"
    )
    demo_part(a.db)
    emit(f"\nчас {time.time() - t0:.0f}s, rss {rss()}")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text("\n".join(LINES) + "\n")


if __name__ == "__main__":
    main()
