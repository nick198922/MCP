"""IDEA-20261025-deribit-expiry-pin-mirror-control: does the Deribit BTC settlement land closer to the
max-volume strike K* than to its mirror M = 2 S0 - K*?

Stages (outputs cached under --cache-dir, default ~/.cache/tradedesk-agents/expiry_pin_mirror; no network):
  build  one pass over put_skew/trades24 (2021-01-01 ... 2026-10-13): K* of the D+1 08:00 expiry at D 09:00 UTC
         -> pin_rows.json                                                              (local, minutes)
  gates  step 0: G1 coverage, G2 n, G3 power (SD of Delta, cluster-bootstrap SE, MDE); prints no means
  test   H1-H3, placebo, controls, exploratory sensitivity (after pre-registration in the card)

uv run python scripts/verify_expiry_pin_mirror.py --stage build [--limit-files 30]
uv run python scripts/verify_expiry_pin_mirror.py --stage gates --out ~/.cache/tradedesk-agents/expiry_pin_mirror/gates.txt
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import resource
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.expiry_pin import (
    attach_placebo,
    balanced_mean,
    cluster_bootstrap,
    holm,
    in_band,
    iso_week,
    mde,
    mirror_delta,
    ols_cluster,
    pin_row,
    summarize_boot,
)
from tradedesk.analytics.gex_flow import HOUR_MS, FlowBook
from tradedesk.analytics.put_skew import dvol_known_at_entry

CACHE_ROOT = Path.home() / ".cache/tradedesk-agents"
TRADES = CACHE_ROOT / "put_skew/trades24"
DELIVERY = CACHE_ROOT / "put_skew/delivery_btc_usd.json"
DVOL = CACHE_ROOT / "put_skew/dvol_BTC.json"

START, END = date(2021, 4, 1), date(2026, 10, 13)  # D range; expiry = D + 1
SPLIT = date(2025, 3, 1)  # expiry date: IS < SPLIT <= OOS
BAND = (0.005, 0.06)
THRESH = 0.0015  # in Delta units (= shift p ~ 0.11 % of spot, since delta ~ 1.32 p)
MDE_GATE = 0.003
MIN_TRADES = 2000
MAX_AGE_MS = HOUR_MS
BLOCK_BTC = 100.0
N_BOOT = 10_000
N_BOOT_SENS = 2_000
SEED = 20261025


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def t0_of(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 9, tzinfo=UTC).timestamp() * 1000)


# ----------------------------------------------------------------------------- build


def stage_build(cache: Path, limit: int | None) -> None:
    files = sorted(TRADES.glob("*.json.gz"))
    if limit:
        files = files[:limit]
    book, book_nb = FlowBook(), FlowBook(max_amount=BLOCK_BTC)
    prev_ids: set[str] = set()
    carry: list[Any] = []
    rows: list[dict[str, Any]] = []
    n_dup = 0
    t_start = time.perf_counter()
    for i, p in enumerate(files):
        d = date.fromisoformat(p.name[:10])
        t0 = t0_of(d)
        tr = json.loads(gzip.decompress(p.read_bytes()))["trades"]
        ids = {str(t["trade_id"]) for t in tr}
        new = [t for t in tr if str(t["trade_id"]) not in prev_ids]
        n_dup += len(tr) - len(new)
        prev_ids = ids
        new.sort(key=lambda t: int(t["timestamp"]))
        batch = carry + new
        carry = book.add(batch, t0)
        book_nb.add(batch, t0)
        exp = t0 + 23 * HOUR_MS  # D+1 08:00 UTC
        r = pin_row(book, t0, exp)
        row: dict[str, Any] = {"d": d.isoformat(), "n_file": len(tr)}
        if r is not None:
            row.update(
                s0=r.spot,
                age_ms=r.spot_age_ms,
                k_star=r.k_star,
                share=r.share,
                k_atm=r.k_atm,
                total=r.total,
                n_strikes=r.n_strikes,
            )
            rnb = pin_row(book_nb, t0, exp)
            if rnb is not None:
                row.update(k_star_nb=rnb.k_star, share_nb=rnb.share)
        rows.append(row)
        book.drop_expired(t0)
        book_nb.drop_expired(t0)
        if i % 200 == 0 or i == len(files) - 1:
            print(
                f"  {d}: {len(tr)} prints, live series {len(book.series)}; {i + 1}/{len(files)} in "
                f"{time.perf_counter() - t_start:.0f}s, rss {rss_mb():.0f} MB",
                flush=True,
            )
    (cache / "pin_rows.json").write_text(json.dumps(rows))
    print(f"build done: {len(rows)} days, dup prints {n_dup}, carried {len(carry)}; rss {rss_mb():.0f} MB", flush=True)


# ----------------------------------------------------------------------------- sample


def load_inputs(cache: Path) -> tuple[list[dict[str, Any]], dict[str, float], dict[int, float]]:
    rows = json.loads((cache / "pin_rows.json").read_text())
    deliv = {str(r["date"]): float(r["delivery_price"]) for r in json.loads(DELIVERY.read_text()) if r.get("delivery_price")}
    dv = dvol_known_at_entry([(int(t), float(v)) for t, v in json.loads(DVOL.read_text())])
    return rows, deliv, dv


def valid_rows(rows: list[dict[str, Any]], deliv: dict[str, float], max_age_ms: int = MAX_AGE_MS) -> list[dict[str, Any]]:
    """Rows of the period with a K*, >= MIN_TRADES prints, a delivery price and a fresh spot (before the d band)."""
    out = []
    for r in rows:
        d = date.fromisoformat(r["d"])
        if not (START <= d <= END) or "k_star" not in r or r["n_file"] < MIN_TRADES or r["age_ms"] > max_age_ms:
            continue
        settle = deliv.get((d + timedelta(days=1)).isoformat())
        if settle is None:
            continue
        out.append({**r, "settle": settle})
    return out


def sample(
    rows: list[dict[str, Any]],
    deliv: dict[str, float],
    dv: dict[int, float],
    *,
    band: tuple[float, float] = BAND,
    kkey: str = "k_star",
    max_age_ms: int = MAX_AGE_MS,
) -> dict[str, np.ndarray]:
    plc = dict(
        zip([r["d"] for r in rows if kkey in r], attach_placebo([{"d": r["d"], "k_star": r[kkey]} for r in rows if kkey in r]), strict=True)
    )
    cols: dict[str, list[Any]] = {
        k: [] for k in ("delta", "above", "dist", "week", "is", "fri", "share", "dvol", "dp", "dp_ok", "datm", "atm_ok", "k_is_atm", "pos")
    }
    for r in valid_rows(rows, deliv, max_age_ms):
        if kkey not in r:
            continue
        s0, settle, k = r["s0"], r["settle"], r[kkey]
        if not in_band(k, s0, *band):
            continue
        d = date.fromisoformat(r["d"])
        e = d + timedelta(days=1)
        kp = plc.get(r["d"])
        dp_ok = kp is not None and in_band(kp, s0, *band)
        atm_ok = in_band(r["k_atm"], s0, *band)
        dms = int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)
        cols["delta"].append(mirror_delta(k, s0, settle))
        cols["above"].append(k > s0)
        cols["dist"].append(abs(k - s0) / s0)
        cols["week"].append(iso_week(e))
        cols["is"].append(e < SPLIT)
        cols["fri"].append(e.weekday() == 4)
        cols["share"].append(r["share"] if kkey == "k_star" else r.get("share_nb", math.nan))
        cols["dvol"].append(dv.get(dms, math.nan))
        cols["dp"].append(mirror_delta(kp, s0, settle) if dp_ok else math.nan)  # type: ignore[arg-type]
        cols["dp_ok"].append(dp_ok)
        cols["datm"].append(mirror_delta(r["k_atm"], s0, settle) if atm_ok else math.nan)
        cols["atm_ok"].append(atm_ok)
        cols["k_is_atm"].append(k == r["k_atm"])
        cols["pos"].append(abs(settle - k) < abs(settle - (2 * s0 - k)))
    return {k: np.asarray(v) for k, v in cols.items()}


def subset(s: dict[str, np.ndarray], mask: np.ndarray) -> dict[str, np.ndarray]:
    return {k: v[mask] for k, v in s.items()}


def boot_bal(s: dict[str, np.ndarray], vkey: str = "delta", n_boot: int = N_BOOT) -> dict[str, float]:
    v, a = s[vkey], s["above"]
    pt = balanced_mean(v, a)
    b = cluster_bootstrap(lambda ix: balanced_mean(v[ix], a[ix]), list(s["week"]), n_boot=n_boot, seed=SEED)
    return summarize_boot(pt, b)


def boot_mean(v: np.ndarray, weeks: np.ndarray, n_boot: int = N_BOOT) -> dict[str, float]:
    b = cluster_bootstrap(lambda ix: float(v[ix].mean()), list(weeks), n_boot=n_boot, seed=SEED)
    return summarize_boot(float(v.mean()), b)


def fmt(x: dict[str, float], k: int = 4) -> str:
    return f"{x['point']:+.{k}f} [{x['lo']:+.{k}f}; {x['hi']:+.{k}f}] se {x['se']:.{k}f} p {x['p']:.3g}"


# ----------------------------------------------------------------------------- gates


def stage_gates(cache: Path, L: list[str]) -> None:
    rows, deliv, dv = load_inputs(cache)
    n_days = (END - START).days + 1
    ok = valid_rows(rows, deliv)
    in_period = [r for r in rows if START <= date.fromisoformat(r["d"]) <= END]
    no_k = sum(1 for r in in_period if "k_star" not in r)
    thin = sum(1 for r in in_period if r["n_file"] < MIN_TRADES)
    old = sum(1 for r in in_period if "k_star" in r and r["age_ms"] > MAX_AGE_MS)
    L.append(
        f"G1 coverage: {len(ok)}/{n_days} days valid = {len(ok) / n_days:.4f} (gate >= 0.95) -> {'PASS' if len(ok) / n_days >= 0.95 else 'FAIL'}"
    )
    L.append(f"   files in period {len(in_period)}, no D+1 series volume {no_k}, < {MIN_TRADES} prints {thin}, spot older than 1h {old}")
    s = sample(rows, deliv, dv)
    n = len(s["delta"])
    L.append(
        f"G2 n(H1) after band {BAND[0]:.3f}-{BAND[1]:.2f}: {n} (above {int(s['above'].sum())}, below {int((~s['above']).sum())}); gate >= 1000 -> {'PASS' if n >= 1000 else 'FAIL'}"
    )
    L.append(
        f"   share K* == ATM strike {s['k_is_atm'].mean():.3f}; median d {np.median(s['dist']):.4f}; IS n {int(s['is'].sum())}, OOS n {int((~s['is']).sum())}"
    )
    c_thr = float(np.percentile(s["share"][s["is"]], 67))
    gates: dict[str, Any] = {"c_thr": c_thr}
    L.append(f"H2 concentration threshold (q67 of IS share among H1 rows): {c_thr:.4f}")
    subsets = {"H1": np.ones(n, bool), "H2": s["share"] >= c_thr, "H3": s["fri"]}
    for h, m in subsets.items():
        ss = subset(s, m)
        v = ss["delta"]
        b = cluster_bootstrap(lambda ix, v=v, a=ss["above"]: balanced_mean(v[ix], a[ix]), list(ss["week"]), n_boot=N_BOOT, seed=SEED)
        se = float(b[np.isfinite(b)].std(ddof=1))
        m_ = mde(se)
        gates[h] = {"n": int(m.sum()), "sd": float(v.std(ddof=1)), "se": se, "mde": m_, "confirmatory": m_ <= MDE_GATE}
        L.append(
            f"G3 {h}: n {int(m.sum())}, weeks {len(set(ss['week']))}, SD(Delta) {v.std(ddof=1):.4f}, SE_boot(Delta_bal) {se:.5f}, "
            f"MDE {m_:.4f} (gate <= {MDE_GATE}) -> {'PASS' if m_ <= MDE_GATE else 'FAIL (exploratory)'}"
        )
    gates["pass"] = len(ok) / n_days >= 0.95 and n >= 1000 and gates["H1"]["confirmatory"]
    L.append(f"step-0 gates overall (G1, G2, G3-H1): {'PASS' if gates['pass'] else 'FAIL'}")
    (cache / "gates.json").write_text(json.dumps(gates))


# ----------------------------------------------------------------------------- test


def describe(label: str, s: dict[str, np.ndarray], L: list[str], n_boot: int = N_BOOT) -> dict[str, float]:
    a = s["above"]
    bal = boot_bal(s, n_boot=n_boot)
    L.append(f"== {label}: n {len(a)} (above {int(a.sum())}, below {int((~a).sum())}), weeks {len(set(s['week']))}")
    L.append(f"   Delta_bal {fmt(bal)}")
    for side, m in (("above", a), ("below", ~a)):
        L.append(f"   Delta | K* {side} S0: {fmt(boot_mean(s['delta'][m], s['week'][m], n_boot))}")
    L.append(f"   raw mean Delta: {fmt(boot_mean(s['delta'], s['week'], n_boot))}; median Delta {np.median(s['delta']):+.4f}")
    pos = s["pos"].astype(float) - 0.5  # centred so that p tests H0 share = 0.5
    L.append(f"   share settle closer to K* than M, minus 0.5 (H0 0): {fmt(boot_mean(pos, s['week'], n_boot), 3)}")
    split = {}
    for nm, m in (("IS", s["is"]), ("OOS", ~s["is"])):
        split[nm] = boot_bal(subset(s, m), n_boot=n_boot)
        L.append(f"   {nm}: n {int(m.sum())}, Delta_bal {fmt(split[nm])}")
    pm = s["dp_ok"]
    sp = subset(s, pm)
    plc = boot_bal(sp, "dp", n_boot)
    pair = boot_mean(sp["delta"] - sp["dp"], sp["week"], n_boot)
    L.append(f"   placebo (K* of D-1, same S0/settle): n {int(pm.sum())}, Delta_p bal {fmt(plc)}")
    L.append(f"   paired Delta - Delta_p: {fmt(pair)}")
    am = s["atm_ok"]
    sa = subset(s, am)
    if am.sum() > 20:
        L.append(f"   ATM control (strike nearest S0, same band): n {int(am.sum())}, Delta_atm bal {fmt(boot_bal(sa, 'datm', n_boot))}")
    return {
        **bal,
        "dp_lo": plc["lo"],
        "dp_hi": plc["hi"],
        "paired": pair["point"],
        "is": split["IS"]["point"],
        "oos": split["OOS"]["point"],
    }


def stage_test(cache: Path, L: list[str]) -> None:
    rows, deliv, dv = load_inputs(cache)
    gates = json.loads((cache / "gates.json").read_text())
    s = sample(rows, deliv, dv)
    subsets = {"H1": np.ones(len(s["delta"]), bool), "H2": s["share"] >= gates["c_thr"], "H3": s["fri"]}
    res = {
        h: describe(f"{h} ({'confirmatory' if gates[h]['confirmatory'] else 'exploratory: MDE > gate'})", subset(s, m), L)
        for h, m in subsets.items()
    }
    conf = [h for h in subsets if gates[h]["confirmatory"]]
    adj = dict(zip(conf, holm([res[h]["p"] for h in conf]), strict=True))
    allp = holm([res[h]["p"] for h in subsets])
    L.append(
        "== Holm over all three (informational, not the registered family): "
        + ", ".join(f"{h} {p:.3g}" for h, p in zip(subsets, allp, strict=True))
    )
    L.append("== verdict rules (pre-registered)")
    for h in subsets:
        r = res[h]
        ok = (
            gates["pass"]
            and gates[h]["confirmatory"]
            and r["point"] >= THRESH
            and r["lo"] > 0
            and adj.get(h, 1.0) < 0.05
            and r["is"] * r["oos"] > 0
            and r["paired"] > 0
            and r["dp_lo"] <= 0 <= r["dp_hi"]
        )
        rej = gates["pass"] and gates[h]["confirmatory"] and r["hi"] < THRESH
        v = "CONFIRMED" if ok else "REJECTED" if rej else "INCONCLUSIVE"
        L.append(
            f"   {h}: Delta_bal {r['point']:+.4f} CI [{r['lo']:+.4f}; {r['hi']:+.4f}], Holm p {adj.get(h, math.nan):.3g}, IS {r['is']:+.4f} OOS {r['oos']:+.4f}, "
            f"paired {r['paired']:+.4f}, placebo CI [{r['dp_lo']:+.4f}; {r['dp_hi']:+.4f}] -> {v}"
        )
    # regression (descriptive): Delta on d and DVOL(D-1), week clusters
    m = np.isfinite(s["dvol"])
    X = np.column_stack([np.ones(int(m.sum())), s["dist"][m], s["dvol"][m]])
    beta, se = ols_cluster(s["delta"][m], X, list(s["week"][m]))
    L.append(
        f"== regression Delta ~ 1 + d + DVOL(D-1) (n {int(m.sum())}, cluster SE): "
        + ", ".join(f"{nm} {b:+.4f} ({e:.4f})" for nm, b, e in zip(("const", "d", "dvol"), beta, se, strict=True))
    )
    Xs = np.column_stack([X, np.where(s["above"][m], 1.0, -1.0)])
    beta, se = ols_cluster(s["delta"][m], Xs, list(s["week"][m]))
    L.append(
        "   + side (+1 above / -1 below): "
        + ", ".join(f"{nm} {b:+.4f} ({e:.4f})" for nm, b, e in zip(("const", "d", "dvol", "side"), beta, se, strict=True))
    )
    # exploratory sensitivity (pre-listed; does not change the verdict)
    L.append("== exploratory sensitivity (pre-listed; H1 only, n_boot 2000)")
    variants: list[tuple[str, dict[str, Any]]] = [
        ("band 0.3-6 %", {"band": (0.003, 0.06)}),
        ("band 0.5-4 %", {"band": (0.005, 0.04)}),
        ("band 1-6 %", {"band": (0.01, 0.06)}),
        ("K* without blocks >= 100 BTC", {"kkey": "k_star_nb"}),
        ("spot age <= 10 min", {"max_age_ms": 600_000}),
    ]
    for nm, kw in variants:
        sv = sample(rows, deliv, dv, **kw)
        b = boot_bal(sv, n_boot=N_BOOT_SENS)
        pos = boot_mean(sv["pos"].astype(float), sv["week"], N_BOOT_SENS)
        L.append(f"   {nm}: n {len(sv['delta'])}, Delta_bal {fmt(b)}; share closer {pos['point']:.3f}")
    q1, q99 = np.percentile(s["delta"], [1, 99])
    tm = (s["delta"] >= q1) & (s["delta"] <= q99)
    L.append(f"   trimmed 1 % each side: n {int(tm.sum())}, Delta_bal {fmt(boot_bal(subset(s, tm), n_boot=N_BOOT_SENS))}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["build", "gates", "test"], required=True)
    ap.add_argument("--cache-dir", type=Path, default=CACHE_ROOT / "expiry_pin_mirror")
    ap.add_argument("--limit-files", type=int)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.stage == "build":
        stage_build(a.cache_dir, a.limit_files)
        return
    L: list[str] = []
    t = time.perf_counter()
    (stage_gates if a.stage == "gates" else stage_test)(a.cache_dir, L)
    L.append(f"done in {time.perf_counter() - t:.0f}s, rss {rss_mb():.0f} MB")
    text = "\n".join(L)
    print(text, flush=True)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text + "\n")


if __name__ == "__main__":
    main()
