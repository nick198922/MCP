"""Polymarket favourites with fresh quotes (IDEA-20261021-polymarket-favorites-fresh-quotes).

Reuses the 24.5 % event sample, quotes and classification of scripts/verify_pm_yes_calibration.py (read-only cache
``~/.cache/tradedesk-agents/pm_yes_calibration``); new files (slim markets, rows, /trades of candidates) go to
``--cache-dir`` (default ``~/.cache/tradedesk-agents/pm_favorites``). Network only through the runtime providers.

Usage:
    uv run python scripts/verify_pm_favorites.py --stage slim     # Yes/No markets of the sample -> markets.json
    uv run python scripts/verify_pm_favorites.py --stage rows     # q at t0 (neg-risk normalised), favourite side
    uv run python scripts/verify_pm_favorites.py --stage trades --limit 200   # timing sample of missing /trades
    uv run python scripts/verify_pm_favorites.py --stage trades   # all missing /trades of basket candidates
    uv run python scripts/verify_pm_favorites.py --stage stage0   # counts, categories, MDE from prices (no outcomes)
Every stage takes ``--log FILE`` (progress lines) and analysis stages ``--out FILE``; no shell redirects needed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import random
import resource
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_pm_yes_calibration import DEFAULT_CACHE as YES_CACHE  # noqa: E402
from verify_pm_yes_calibration import (  # noqa: E402
    _needs_fetch,
    _retry,
    _write,
    load_quotes,
    ok_years,
    sample_universe,
    t0_of,
)
from verify_pm_yes_calibration import tpath as yes_tpath  # noqa: E402

from tradedesk.analytics.pm_calibration import (  # noqa: E402
    bootstrap_ci,
    cluster_bootstrap_ci,
    cluster_means,
    eligible_at,
    reliability,
)
from tradedesk.analytics.pm_favorites import (  # noqa: E402
    CONTROL_BASKET,
    FAV_BASKET,
    PAYOUT_BASKET,
    awaiting_payout,
    cochran_q,
    fav_outcome,
    fav_return,
    favourite,
    in_basket,
    last_print,
    normalised_quotes,
    price_mde,
    worst_losing_streak,
)

DEFAULT_CACHE = Path("~/.cache/tradedesk-agents/pm_favorites").expanduser()
H_STEP0 = (4, 24)
STALE_S = 2 * 3600
SAMPLE_FRAC = 0.245
SLIM_KEYS = (
    "market_id event_id condition_id yes_token no_token outcome T anchor start closed category neg_risk volume fee_rate fee_exp year"
).split()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')} rss={rss_mb():.0f}MB] {msg}", flush=True)


def tpath(cache: Path, mid: str, h: int) -> Path:
    return cache / "trades" / mid[-2:] / f"{mid}_{h}.json"


def trades_file(cache: Path, mid: str, h: int) -> Path | None:
    """Own cache first, then the yes-calibration cache (same request: limit 5, end = t0)."""
    for p in (tpath(cache, mid, h), yes_tpath(YES_CACHE, mid, h)):
        if p.exists() and not _needs_fetch(p):
            return p
    return None


# ----------------------------------------------------------------------------- slim


def stage_slim(cache: Path) -> None:
    log("loading universe_sample.json (yes-calibration, 24.5 % of events)")
    u = sample_universe(YES_CACHE)
    log(f"loaded: markets {len(u['markets'])} events {len(u['events'])}")
    ms = [{k: m[k] for k in SLIM_KEYS} for m in u["markets"] if m["yes_no"]]
    ev = u["events"]
    ok_count = Counter(m["event_id"] for m in u["markets"])  # ok markets of any type per event
    del u
    out = {"markets": ms, "events": {e: {**v, "n_ok": ok_count.get(e, 0)} for e, v in ev.items()}}
    _write(cache / "markets.json", out)
    log(f"slim: yes/no markets {len(ms)}, events {len(ev)} -> {cache / 'markets.json'}")


# ----------------------------------------------------------------------------- rows (state at t0, no outcomes used)


def stage_rows(cache: Path) -> None:
    d = json.loads((cache / "markets.json").read_text())
    ms, evs = d["markets"], d["events"]
    log(f"markets {len(ms)}; reading quotes")
    qs = load_quotes(YES_CACHE, ms)
    log(f"quote files {len(qs)}")
    by_ev: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in ms:
        by_ev[m["event_id"]].append(m)
    for h in H_STEP0:
        rows = []
        n_elig = 0
        neg_status: Counter = Counter()
        for eid, ems in by_ev.items():
            ev = evs.get(eid) or {}
            neg = bool(ev.get("neg")) or any(m["neg_risk"] for m in ems)
            norm: dict[str, float] | None = None
            if neg:
                # all options of the event classified ok as Yes/No, one T; every option open at t0 has a quote
                if ev.get("n", 0) < 2 or len(ems) != ev.get("n") or len({m["T"] for m in ems}) != 1:
                    neg_status["not_normalisable_meta"] += 1
                else:
                    t0 = t0_of(ems[0], h)
                    open_ = [m for m in ems if m["start"] <= t0 and m["closed"] > t0]
                    oq = {}
                    for m in open_:
                        qa = (qs.get(m["market_id"]) or {}).get("q", {}).get(h)
                        oq[m["market_id"]] = qa[0] if qa else None
                    norm = normalised_quotes(oq)
                    neg_status["normalised" if norm is not None else "not_normalisable_quotes"] += 1
            for m in ems:
                t0 = t0_of(m, h)
                if not eligible_at(m, t0):
                    continue
                n_elig += 1
                qa = (qs.get(m["market_id"]) or {}).get("q", {}).get(h)
                r = {k: m[k] for k in SLIM_KEYS}
                r.update(h=h, t0=t0, neg=neg, q_raw=qa[0] if qa else None)
                if qa is None:
                    r["q"] = None
                elif neg:
                    r["q"] = norm.get(m["market_id"]) if norm else None
                else:
                    r["q"] = qa[0]
                if r["q"] is not None:
                    r["side"], r["p_f"] = favourite(r["q"])
                if r["q_raw"] is not None:
                    r["side_raw"], r["p_f_raw"] = favourite(r["q_raw"])
                rows.append(r)
        _write(cache / f"rows_h{h}.json", rows)
        log(f"h={h}: eligible {n_elig}, rows {len(rows)}, neg-risk events {dict(neg_status)}")


def load_rows(cache: Path, h: int) -> list[dict[str, Any]]:
    return json.loads((cache / f"rows_h{h}.json").read_text())


def candidate(r: dict[str, Any], bands: tuple[tuple[float, float], ...]) -> bool:
    return any(
        (r.get("p_f") is not None and in_basket(r["p_f"], b)) or (r.get("p_f_raw") is not None and in_basket(r["p_f_raw"], b))
        for b in bands
    )


# ----------------------------------------------------------------------------- trades (missing only)


async def stage_trades(cache: Path, rate: float, limit: int | None, bands: tuple[tuple[float, float], ...], hs: tuple[int, ...]) -> None:
    from tradedesk.core.ratelimit import TokenBucket
    from tradedesk.server import build_server

    todo: list[tuple[str, str, int, int]] = []
    have = 0
    for h in hs:
        for r in load_rows(cache, h):
            if not candidate(r, bands):
                continue
            if trades_file(cache, r["market_id"], h) is not None:
                have += 1
                continue
            main = r.get("p_f") is not None and any(in_basket(r["p_f"], b) for b in bands) and r["year"] >= 2026
            todo.append((not main, r["market_id"], r["condition_id"], h, r["t0"]))
    todo.sort(key=lambda j: j[0])  # main-q candidates of 2026 first, raw-only neg-risk and 2025 after
    n_main = sum(1 for j in todo if not j[0])
    todo = [j[1:] for j in todo]
    log(f"trades: candidates cached {have}, to fetch {len(todo)} (main 2026 first: {n_main}) (bands {bands}, h {hs}) rate={rate}/s")
    if limit:
        todo = random.Random(7).sample(todo, min(limit, len(todo)))
    _, rt = build_server()
    rt.polymarket.data._limiter = TokenBucket(rate, int(2 * rate))
    sem = asyncio.Semaphore(max(4, int(2 * rate)))
    t_start = time.perf_counter()
    done = errs = 0
    total = len(todo)

    async def one(mid: str, cid: str, h: int, t0: int) -> None:
        nonlocal done, errs
        try:
            async with sem:
                f = await _retry(lambda: rt.polymarket.trades(cid, limit=5, end=t0), "trades")
            obj: dict[str, Any] = {"trades": f.data}
        except Exception as e:  # noqa: BLE001
            errs += 1
            obj = {"error": f"{type(e).__name__}: {str(e)[:200]}", "trades": []}
        _write(tpath(cache, mid, h), obj)
        done += 1
        if done == min(100, total):
            el = time.perf_counter() - t_start
            log(f"  sample: {done} in {el:.1f}s -> {done / el:.1f}/s")
        if done % 2000 == 0:
            el = time.perf_counter() - t_start
            log(f"  trades {done}/{total} {el:.0f}s ({done / el:.1f}/s) errors {errs}")

    try:
        await asyncio.gather(*(one(*j) for j in todo))
    finally:
        await rt.aclose()
    el = time.perf_counter() - t_start
    log(f"trades done {done} in {el:.0f}s ({done / max(el, 1e-9):.1f}/s) errors={errs}")


def attach_fresh(cache: Path, rows: list[dict[str, Any]], h: int) -> None:
    """Freshness on the favourite side of the main q (state at t0; prints after t0 ignored)."""
    for r in rows:
        r["lp"] = r["lp_strict"] = None
        r["trades_fetched"] = False
        if r.get("p_f") is None:
            continue
        p = trades_file(cache, r["market_id"], h)
        if p is None:
            continue
        tr = json.loads(p.read_text()).get("trades") or []
        r["trades_fetched"] = True
        r["lp"] = last_print(tr, r["t0"], r["yes_token"], r["side"])
        fav_tok = r["yes_token"] if r["side"] == "yes" else r["no_token"]
        r["lp_strict"] = last_print(tr, r["t0"], r["yes_token"], r["side"], fav_token=fav_tok)


def is_fresh(r: dict[str, Any], strict: bool = False, stale_s: int = STALE_S) -> bool:
    lp = r.get("lp_strict" if strict else "lp")
    return lp is not None and lp["age"] <= stale_s


# ----------------------------------------------------------------------------- stage 0 (no outcome averages)


def stage0(cache: Path, out: str) -> dict[int, dict[str, Any]]:
    L: list[str] = []
    P = L.append
    P("## Крок 0 (без середніх результатів)\n")
    P(
        "Вибірка: 24.5 % подій `yes-calibration` (стратифікація категорія × місяць, seed 7), лише Yes/No ринки; "
        f"q — знімок `/prices-history` ≤ t0 − 60 с, вік ≤ 15 хв (проксі двосторонньої книги); свіжість — остання угода ринку "
        f"(будь-який токен, у ціні фаворита) ≤ 2 год до t0; кошик фаворита p_f ∈ [{FAV_BASKET[0]}; {FAV_BASKET[1]}]; neg-risk — q/Σq на t0.\n"
    )
    res: dict[int, dict[str, Any]] = {}
    for h in H_STEP0:
        rows = load_rows(cache, h)
        cov = []
        for y in sorted({r["year"] for r in rows}):
            rs = [r for r in rows if r["year"] == y]
            k = sum(1 for r in rs if r["q_raw"] is not None)
            cov.append((y, len(rs), k, k / len(rs) if rs else 0.0))
        yrs = ok_years([{"year": r["year"], "q": r["q_raw"]} for r in rows])
        attach_fresh(cache, rows, h)
        P(f"### h = {h} год\n")
        P("| Рік | придатних на t0 | є q | покриття |\n|---|---|---|---|")
        for y, n, k, c in cov:
            P(f"| {y} | {n} | {k} | {c:.1%} |")
        P(f"\nРоки з покриттям ≥ 70 % (правило `yes-calibration`): {sorted(yrs)}.\n")
        inyr = [r for r in rows if r["year"] in yrs]
        neg_rows = [r for r in inyr if r["neg"]]
        P(
            f"Neg-risk ринків {len(neg_rows)}: q/Σq є у {sum(1 for r in neg_rows if r['q'] is not None)}, "
            f"без нормалізації (не всі варіанти з q або метадані) {sum(1 for r in neg_rows if r['q'] is None and r['q_raw'] is not None)}.\n"
        )

        def ev(rs: list[dict[str, Any]]) -> int:
            return len({r["event_id"] for r in rs})

        fav = [r for r in inyr if r.get("p_f") is not None and in_basket(r["p_f"], FAV_BASKET)]
        fav_raw_neg = [r for r in neg_rows if r.get("p_f_raw") is not None and in_basket(r["p_f_raw"], FAV_BASKET)]
        fetched = [r for r in fav if r["trades_fetched"]]
        fr = [r for r in fav if is_fresh(r)]
        frs = [r for r in fav if is_fresh(r, strict=True)]
        P("| Кошик фаворита (головне q) | ринків | подій |\n|---|---|---|")
        P(f"| усі з q у кошику | {len(fav)} | {ev(fav)} |")
        P(f"| з них з /trades у кеші | {len(fetched)} | {ev(fetched)} |")
        for s in (1800, STALE_S, 6 * 3600):
            fs = [r for r in fav if is_fresh(r, stale_s=s)]
            P(f"| свіжі ≤ {s // 60} хв | {len(fs)} | {ev(fs)} |")
        P(f"| свіжі ≤ 120 хв, лише угоди на токені фаворита (строгий) | {len(frs)} | {ev(frs)} |")
        P(f"| — не neg-risk | {sum(1 for r in fr if not r['neg'])} | {ev([r for r in fr if not r['neg']])} |")
        P(f"| — neg-risk (q/Σq) | {sum(1 for r in fr if r['neg'])} | {ev([r for r in fr if r['neg']])} |")
        P(f"| — сторона YES / NO | {sum(1 for r in fr if r['side'] == 'yes')} / {sum(1 for r in fr if r['side'] == 'no')} | — |")
        P(f"| neg-risk у кошику за сирим q (лише опис) | {len(fav_raw_neg)} | {ev(fav_raw_neg)} |")
        wp = [r for r in fr if awaiting_payout(r["anchor"], r["T"], r["closed"])]
        P(
            f"\n«Чекання виплати» (closedTime < T, не спорт; опис): {len(wp)} ринків, {ev(wp)} подій "
            f"({ev(wp) / max(ev(fr), 1):.1%} свіжих подій кошика).\n"
        )
        # MDE from prices (outcomes not used)
        groups: dict[str, list[float]] = defaultdict(list)
        for r in fr:
            groups[r["event_id"]].append(r["p_f"])
        g_all: dict[str, list[float]] = defaultdict(list)
        for r in fav:
            g_all[r["event_id"]].append(r["p_f"])
        m_all = price_mde(g_all.values(), within="full")
        P(
            f"Межа без фільтра свіжості (усі події кошика): подій {m_all['n']}, MDE {m_all['mde'] * 100:.2f} п.п. "
            f"(вибірка), ≈ {m_all['mde'] * math.sqrt(SAMPLE_FRAC) * 100:.2f} п.п. (повний перелік).\n"
        )
        m_full = price_mde(groups.values(), within="full")
        m_ind = price_mde(groups.values(), within="indep")
        mf_full = m_full["mde"] * math.sqrt(SAMPLE_FRAC)
        dates = len({time.strftime("%Y-%m-%d", time.gmtime(r["T"])) for r in fr})
        P(
            f"MDE H1 з цін (кластер = подія): повна кореляція в події {m_full['mde'] * 100:.2f} п.п. (sd_eff {m_full['sd_eff']:.3f}), "
            f"незалежні ринки {m_ind['mde'] * 100:.2f} п.п.; дат T {dates}. Оцінка повного переліку (n / 0.245): "
            f"подій ≈ {m_full['n'] / SAMPLE_FRAC:.0f}, MDE ≈ {mf_full * 100:.2f} п.п.\n"
        )
        P("| Категорія (свіжі) | ринків | подій | MDE, п.п. (повна кор.) | частка «чекання виплати» |\n|---|---|---|---|---|")
        cats = Counter(r["category"] for r in fr)
        cat_mde = {}
        for c, _ in cats.most_common():
            rs = [r for r in fr if r["category"] == c]
            g: dict[str, list[float]] = defaultdict(list)
            for r in rs:
                g[r["event_id"]].append(r["p_f"])
            mm = price_mde(g.values())
            cat_mde[c] = mm["mde"]
            w = sum(1 for r in rs if awaiting_payout(r["anchor"], r["T"], r["closed"]))
            P(f"| {c} | {len(rs)} | {mm['n']} | {mm['mde'] * 100:.2f} | {w / len(rs):.0%} |")
        lock = sorted((r["closed"] - r["t0"]) / 3600 for r in fr)
        if lock:
            P(f"\nБлокування closedTime − t0 (год), медіана {lock[len(lock) // 2]:.1f}, p90 {lock[int(0.9 * (len(lock) - 1))]:.1f}.\n")
        ctrl = [r for r in inyr if r.get("p_f") is not None and in_basket(r["p_f"], CONTROL_BASKET)]
        pay = [r for r in inyr if r.get("p_f") is not None and in_basket(r["p_f"], PAYOUT_BASKET) and r["p_f"] > FAV_BASKET[1]]
        P(
            f"Контрольний кошик [0.60; 0.80] (до свіжості): {len(ctrl)} ринків, {ev(ctrl)} подій; "
            f"з угодами {sum(1 for r in ctrl if trades_file(cache, r['market_id'], h))}. "
            f"Кошик (0.97; 1.00] (до свіжості): {len(pay)} ринків, {ev(pay)} подій.\n"
        )
        res[h] = {
            "events_fresh": m_full["n"],
            "markets_fresh": len(fr),
            "mde_full": m_full["mde"],
            "mde_indep": m_ind["mde"],
            "mde_full_list": mf_full,
            "events_full_list": m_full["n"] / SAMPLE_FRAC,
            "fetched_share": len(fetched) / max(len(fav), 1),
            "cat_mde": cat_mde,
            "years": sorted(yrs),
        }
    h_main = max(H_STEP0, key=lambda h: (res[h]["events_fresh"], h == 4))
    g = res[h_main]
    P("### Ворота (умова 4: MDE H1 ≤ 1.0 п.п. на кластерному SE)\n")
    counts = ", ".join(f"h={h}: {res[h]['events_fresh']}" for h in H_STEP0)
    P(f"Головне h за кількістю свіжих подій: **{h_main}** ({counts}).\n")
    P("| Ворота | Значення | Пройдено |\n|---|---|---|")
    P(f"| вибірка 24.5 %: MDE (повна кор.) ≤ 1.0 п.п. | {g['mde_full'] * 100:.2f} | {'так' if g['mde_full'] <= 0.01 else 'ні'} |")
    P(f"| повний перелік (оцінка): MDE ≤ 1.0 п.п. | {g['mde_full_list'] * 100:.2f} | {'так' if g['mde_full_list'] <= 0.01 else 'ні'} |")
    report = "\n".join(L)
    print(report, flush=True)
    if out:
        Path(out).expanduser().write_text(report + "\n")
    _write(cache / "stage0.json", {"h_main": h_main, "res": {str(k): v for k, v in res.items()}})
    return res


# ----------------------------------------------------------------------------- full run (only after the gate and the registration commit)


def _ci_row(label: str, pairs: list[tuple[str, float]], k: float = 100.0) -> tuple[str, dict[str, Any]]:
    """Table row: markets, events, event-mean estimate and bootstrap CI over events (10 000, seed 7)."""
    vals = cluster_means(pairs)
    b = bootstrap_ci(vals)
    if not vals:
        return f"| {label} | 0 | 0 | — | — |", b
    lo = f"{b['lo'] * k:+.2f}" if b["lo"] is not None else "—"
    hi = f"{b['hi'] * k:+.2f}" if b["hi"] is not None else "—"
    return f"| {label} | {len(pairs)} | {len(vals)} | {b['mean'] * k:+.2f} | [{lo}; {hi}] |", b


def _date(r: dict[str, Any]) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(r["T"]))


def _yf(r: dict[str, Any]) -> int:
    return fav_outcome(r["side"], r["outcome"])


def _gap(rs: list[dict[str, Any]], p_key: str = "p_f", side_key: str = "side") -> list[tuple[str, float]]:
    return [(r["event_id"], r[p_key] - fav_outcome(r[side_key], r["outcome"])) for r in rs]


def _ret(r: dict[str, Any], half_spread: float = 0.01, use_last: bool = True, fee: tuple[float, float] | None = None) -> float:
    rate, exp = fee if fee is not None else (r["fee_rate"], r["fee_exp"])
    last = r["lp"]["px"] if (use_last and r.get("lp")) else None
    return fav_return(r["p_f"], _yf(r), half_spread=half_spread, last_fav_px=last, fee_rate=rate, fee_exp=exp)["ret"]


def _h2(rs: list[dict[str, Any]], **kw: Any) -> list[tuple[str, float]]:  # noqa: ANN401
    return [(r["event_id"], _ret(r, **kw)) for r in rs]


def stage_full(cache: Path, out: str) -> None:
    s0 = json.loads((cache / "stage0.json").read_text())
    h = int(s0["h_main"])
    yrs = set(s0["res"][str(h)]["years"])
    cat_ok = sorted(c for c, v in s0["res"][str(h)]["cat_mde"].items() if v <= 0.02)
    reg = (cache / "registration.sha256").read_text().strip() if (cache / "registration.sha256").exists() else "?"
    rows = [r for r in load_rows(cache, h) if r["year"] in yrs]
    attach_fresh(cache, rows, h)
    L: list[str] = []
    P = L.append
    hdr = "| Варіант | ринків | подій | оцінка | 95 % CI |\n|---|---|---|---|---|"
    P(f"## Повний прогін (h = {h}, реєстрація sha256 {reg[:12]})\n")
    main = [r for r in rows if r.get("p_f") is not None and in_basket(r["p_f"], FAV_BASKET) and is_fresh(r)]
    main.sort(key=lambda r: (r["t0"], r["market_id"]))
    P("### H1: p_f − частота(y_f), п.п. (від'ємне = фаворит недооцінений)\n")
    P(hdr)
    row, h1 = _ci_row("головне: кошик [0.88; 0.97], свіжі ≤ 2 год, neg-risk q/Σq", _gap(main))
    P(row)
    dc = cluster_bootstrap_ci([(_date(r), r["event_id"], r["p_f"] - _yf(r)) for r in main])
    P(
        f"| те саме, CI двоетапний по датах T ({dc['n_clusters']} дат) | {len(main)} | {dc['n']} | {dc['mean'] * 100:+.2f} | [{dc['lo'] * 100:+.2f}; {dc['hi'] * 100:+.2f}] |"
    )
    med = sorted(_date(r) for r in main)[len(main) // 2] if main else ""
    halves = {"1-а половина": [r for r in main if _date(r) < med], "2-а половина": [r for r in main if _date(r) >= med]}
    for k, rs in halves.items():
        P(_ci_row(f"{k} (медіана T {med})", _gap(rs))[0])
    P(_ci_row("лише не neg-risk", _gap([r for r in main if not r["neg"]]))[0])
    P(_ci_row("лише neg-risk (q/Σq)", _gap([r for r in main if r["neg"]]))[0])
    for c in cat_ok:
        P(_ci_row(f"категорія {c} (MDE ≤ 2 п.п. на кроці 0)", _gap([r for r in main if r["category"] == c]))[0])
    P("\n**H1, чутливість (кожен варіант):**\n")
    P(hdr)
    for s in (1800, 6 * 3600):
        P(
            _ci_row(
                f"свіжість ≤ {s // 60} хв",
                _gap([r for r in rows if r.get("p_f") is not None and in_basket(r["p_f"], FAV_BASKET) and is_fresh(r, stale_s=s)]),
            )[0]
        )
    P(
        _ci_row(
            "свіжість строга (угода на токені фаворита)",
            _gap([r for r in rows if r.get("p_f") is not None and in_basket(r["p_f"], FAV_BASKET) and is_fresh(r, strict=True)]),
        )[0]
    )
    P(_ci_row("без фільтра свіжості (брудно)", _gap([r for r in rows if r.get("p_f") is not None and in_basket(r["p_f"], FAV_BASKET)]))[0])
    for y in sorted(yrs):
        P(_ci_row(f"рік {y}", _gap([r for r in main if r["year"] == y]))[0])
    big = Counter(r["category"] for r in main).most_common(1)
    if big:
        P(_ci_row(f"без найбільшої категорії ({big[0][0]})", _gap([r for r in main if r["category"] != big[0][0]]))[0])
    P(_ci_row("«чекання виплати» (closedTime < T), опис", _gap([r for r in main if awaiting_payout(r["anchor"], r["T"], r["closed"])]))[0])
    P(_ci_row("без «чекання виплати»", _gap([r for r in main if not awaiting_payout(r["anchor"], r["T"], r["closed"])]))[0])

    P("\n### H2: купівля фаворита тейкером, дохід на капітал (вхід + комісія), %\n")
    P(hdr)
    row, h2 = _ci_row("головне: max(p_f + 0.01, остання угода) + комісія feeSchedule", _h2(main))
    P(row)
    for hs in (0.005, 0.02):
        P(_ci_row(f"півспреду {hs}", _h2(main, half_spread=hs))[0])
    P(_ci_row("без останньої угоди (лише p_f + 0.01)", _h2(main, use_last=False))[0])
    P(_ci_row("комісія 0.0175 на всі ринки", _h2(main, fee=(0.0175, 1.0)))[0])
    for k, rs in halves.items():
        P(_ci_row(f"{k}", _h2(rs))[0])
    rets = cluster_means(_h2(main))
    lock_d = [max(r["closed"] - r["t0"], 3600) / 86400 for r in main]
    if rets:
        ev_sorted = sorted(rets)
        cut = max(1, len(rets) // 100)
        P(f"| без топ-1 % подій ({cut}) | — | {len(rets) - cut} | {statistics.fmean(ev_sorted[:-cut]) * 100:+.2f} | — |")
        P(
            f"\nБлокування closedTime − t0: медіана {statistics.median(lock_d):.2f} дн, середнє {statistics.fmean(lock_d):.2f} дн; "
            f"дохід на день блокування (середній дохід / середні дні) {statistics.fmean(rets) / statistics.fmean(lock_d) * 100:+.2f} %/дн."
        )
        ys = [_yf(r) for r in main]
        P(
            f"Частка програшів (ринків) {1 - statistics.fmean(ys):.2%}; найдовша серія програшів у порядку t0 {worst_losing_streak(ys)}; "
            f"середній вхід {statistics.fmean(fav_return(r['p_f'], 1, last_fav_px=r['lp']['px'] if r.get('lp') else None)['entry'] for r in main):.3f}; "
            f"ринків із комісією {sum(1 for r in main if r['fee_rate'] > 0)}.\n"
        )
    P("Мейкерський вхід (бід без комісії): ◯ — історії книги немає.\n")

    P("### H3: одиниця і neg-risk\n")
    P(hdr)
    mk = [r["p_f"] - _yf(r) for r in main]
    b = bootstrap_ci(mk)
    if mk:
        P(
            f"| одиниця = контракт (ринок), CI по ринках | {len(mk)} | — | {b['mean'] * 100:+.2f} | [{b['lo'] * 100:+.2f}; {b['hi'] * 100:+.2f}] |"
        )
    negp = [r for r in rows if r["neg"] and r.get("p_f") is not None and is_fresh(r)]
    raw_in = [r for r in negp if r.get("p_f_raw") is not None and in_basket(r["p_f_raw"], FAV_BASKET)]
    P(_ci_row("neg-risk, сирі q у кошику (опис)", _gap(raw_in, "p_f_raw", "side_raw"))[0])
    both = [r for r in negp if in_basket(r["p_f"], FAV_BASKET) and r.get("p_f_raw") is not None and in_basket(r["p_f_raw"], FAV_BASKET)]
    P(_ci_row("neg-risk, обидва в кошику: сирі", _gap(both, "p_f_raw", "side_raw"))[0])
    P(_ci_row("neg-risk, обидва в кошику: q/Σq", _gap(both))[0])
    P(_ci_row("спорт (опис; Cardozo: ефекту нема)", _gap([r for r in main if r["category"] == "sports"]))[0])

    P("\n### Плацебо і контроль\n")
    P(hdr)
    rng = random.Random(7)
    plac = [(r["event_id"], (r["p_f"] - _yf(r)) * (1 if rng.random() < 0.5 else -1)) for r in main]
    P(_ci_row("плацебо: випадкова сторона (seed 7) тих самих ринків", plac)[0])
    ctrl = [r for r in rows if r.get("p_f") is not None and in_basket(r["p_f"], CONTROL_BASKET) and is_fresh(r)]
    P(_ci_row("контроль H1: кошик [0.60; 0.80], свіжі", _gap(ctrl))[0])
    P(_ci_row("контроль H2: кошик [0.60; 0.80], свіжі, %", _h2(ctrl))[0])

    P("\n### 5a: категорії (неоднорідність і стійкість), H2 нетто, %\n")
    P("| Категорія | ринків | подій | E[R] | 95 % CI |\n|---|---|---|---|---|")
    means, ses, cats = [], [], []
    for c, _ in Counter(r["category"] for r in main).most_common():
        row, bc = _ci_row(c, _h2([r for r in main if r["category"] == c]))
        P(row)
        if bc["lo"] is not None:
            cats.append(c)
            means.append(bc["mean"])
            ses.append((bc["hi"] - bc["lo"]) / (2 * 1.96))
    cq = cochran_q(means, ses)
    P(f"\nCochran Q = {cq['Q']:.1f} (df {cq['df']}), I² = {cq['I2']:.0%}.")
    first, second = halves["1-а половина"], halves["2-а половина"]
    r1 = {
        c: statistics.fmean(cluster_means(_h2([r for r in first if r["category"] == c])))
        for c in cats
        if any(r["category"] == c for r in first)
    }
    top = sorted(r1, key=r1.get, reverse=True)[: max(1, len(r1) // 4)]
    P(_ci_row(f"2-а половина, топ-¼ категорій 1-ї половини ({', '.join(top)})", _h2([r for r in second if r["category"] in top]))[0])
    P(_ci_row("2-а половина, решта категорій", _h2([r for r in second if r["category"] not in top]))[0])

    P("\n### Крива калібрування 0.50–1.00 по 2 п.п. (опис, без фільтра свіжості; свіжі — лише там, де є угоди)\n")
    P("| p_f | n ринків | середнє p_f | частота y_f | розрив, п.п. |\n|---|---|---|---|---|")
    edges = [0.50 + 0.02 * i for i in range(26)]
    rel = reliability([(r["p_f"], _yf(r)) for r in rows if r.get("p_f") is not None], edges)
    for x in rel:
        if x["n"]:
            P(f"| [{x['lo']:.2f}; {x['hi']:.2f}) | {x['n']} | {x['mean_q']:.3f} | {x['freq']:.3f} | {x['gap'] * 100:+.2f} |")
    report = "\n".join(L)
    print(report, flush=True)
    if out:
        Path(out).expanduser().write_text(report + "\n")
    _write(cache / "full.json", {"h": h, "h1": h1, "h2": h2, "h1_dates": dc})


# ----------------------------------------------------------------------------- main


class _Tee:
    def __init__(self, path: Path) -> None:
        self.f = path.open("a")
        self.out = sys.stdout

    def write(self, s: str) -> int:
        self.f.write(s)
        self.f.flush()
        return self.out.write(s)

    def flush(self) -> None:
        self.f.flush()
        self.out.flush()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["slim", "rows", "trades", "stage0", "full"])
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--rate", type=float, default=10.0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--bands", default="fav", choices=["fav", "fav+control"])
    ap.add_argument("--out", default="")
    ap.add_argument("--log", default="")
    ap.add_argument("--hs", default="4,24", help="horizons (h) for --stage trades")
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if a.log:
        sys.stdout = _Tee(Path(a.log).expanduser())  # type: ignore[assignment]
    if a.stage == "slim":
        stage_slim(cache)
    elif a.stage == "rows":
        stage_rows(cache)
    elif a.stage == "trades":
        bands = (FAV_BASKET,) if a.bands == "fav" else (FAV_BASKET, CONTROL_BASKET)
        hs = tuple(int(x) for x in a.hs.split(","))
        asyncio.run(stage_trades(cache, a.rate, a.limit, bands, hs))
    elif a.stage == "stage0":
        stage0(cache, a.out)
    elif a.stage == "full":
        stage_full(cache, a.out)
    log("end")


if __name__ == "__main__":
    main()
