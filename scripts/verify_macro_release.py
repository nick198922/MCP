"""BTC/ETH reaction to US CPI, NFP and FOMC releases in the first minutes to 24 hours (IDEA-20261017-macro, part B2).

The pre-registration is the card section "Попередня реєстрація (B2: реакція на релізи)" in
``docs/ideas/IDEA-20261017-macro.md``. Network only through ``core.http.Http`` (federalreserve.gov, bls.gov) and the
runtime's ``rt.binance_vision`` (5m spot klines); every response is written to the per-file cache under ``--cache-dir``
as it arrives (default ``~/.cache/tradedesk-agents/macro_release``). Nasdaq COMP daily bars and the Fed rate-change
table are read from the B1 cache (``~/.cache/tradedesk-agents/macro_regimes``), read only.

    uv run python scripts/verify_macro_release.py --stage prereg   # hash of the registration -> registration.txt
    uv run python scripts/verify_macro_release.py --stage fetch    # 5m klines, FOMC calendar, BLS schedules (one try)
    uv run python scripts/verify_macro_release.py --stage dates    # event dates vs official calendars (no prices)
    uv run python scripts/verify_macro_release.py --stage gates    # sanity + n, SD(z), MDE (no means)
    uv run python scripts/verify_macro_release.py --stage report --with-gates --out raw.md
    uv run python scripts/verify_macro_release.py --stage report --move-event 2026-10-14:2026-10-15 --out alt.md  # sensitivity
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import resource
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.macro_regimes import parse_fed_changes, parse_nasdaq_historical
from tradedesk.analytics.macro_release import (
    BAR_MS,
    HOUR_MS,
    MIN_MS,
    Event,
    bootstrap_mean,
    bootstrap_two,
    cochran_q,
    continuation_verdict,
    event_window,
    h1_verdict,
    mde,
    parse_bls_schedule,
    parse_events_csv,
    parse_fomc_calendar,
    placebo_days,
    release_ms,
    rule_pnl,
    rule_verdict,
)

ROOT = Path(__file__).resolve().parents[1]
CARD = ROOT / "docs/ideas/IDEA-20261017-macro.md"
EVENTS_CSV = ROOT / "docs/verification/2026-10-15-us-macro-events.csv"
SECTION = "## Попередня реєстрація (B2: реакція на релізи)"
B1_CACHE = Path("~/.cache/tradedesk-agents/macro_regimes").expanduser()
FIRST = date(2021, 1, 1)
LAST_EVENT = date(2026, 10, 15)
LAST_DATA = date(2026, 10, 16)
HALF2 = date(2024, 1, 1)
CRYPTO = ("BTC", "ETH")
KINDS = ("cpi", "nfp", "fomc")
CLAUDE = {"fomc": 2.9, "cpi": 1.9, "nfp": 1.1}
REPS = 2000
REPS_BONF = 20000  # exploratory multiplicity-adjusted CI (added after the first run)
N_MAIN_TESTS = 12  # H1 3 kinds x 2 assets, H3 pooled x 2, H4a x 2, H5 x 2
TOP_PLACEBO = 15  # reverse calendar scan (added after the first run)
SEED = 20261018
COST = 0.0005
SLIP = 0.0002
GATE_POOLED = 0.30
GATE_KIND = 0.45
GATE_RULE = 0.010
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
L: list[str] = []


def emit(s: str = "") -> None:
    L.append(s)
    print(s, flush=True)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _write(p: Path, data: bytes | str) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    if isinstance(data, str):
        tmp.write_text(data)
    else:
        tmp.write_bytes(data)
    tmp.replace(p)


# ----------------------------------------------------------------------------- prereg


def stage_prereg(cache: Path) -> None:
    raw = CARD.read_bytes()
    t = raw.decode()
    a = t.index(SECTION)
    b = t.index("\n## ", a + 1)
    sec = t[a:b].encode()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()  # = git hash-object
    line = f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec).hexdigest()} bytes={len(sec)} card_blob={blob}"
    emit(line)
    with (cache / "registration.txt").open("a") as f:
        f.write(line + "\n")


# ----------------------------------------------------------------------------- fetch


def kline_jobs(cache: Path) -> list[tuple[Path, str]]:
    kdir = cache / "klines5m"
    jobs: list[tuple[Path, str]] = []
    for asset in CRYPTO:
        sym = f"{asset}USDT"
        for y in range(2020, 2027):
            for m in range(1, 13):
                if (y, m) < (2020, 12) or (y, m) > (2026, 9):
                    continue
                name = f"{sym}-5m-{y:04d}-{m:02d}"
                jobs.append((kdir / f"{name}.json", f"/data/spot/monthly/klines/{sym}/5m/{name}.zip"))
        for d in range(1, LAST_DATA.day + 1):
            name = f"{sym}-5m-2026-10-{d:02d}"
            jobs.append((kdir / f"{name}.json", f"/data/spot/daily/klines/{sym}/5m/{name}.zip"))
    return jobs


async def fetch(cache: Path) -> None:
    from tradedesk.core.errors import ProviderError
    from tradedesk.core.http import Http
    from tradedesk.providers.binance_vision import parse_klines_zip
    from tradedesk.server import build_server

    fed = Http("fed", "https://www.federalreserve.gov", rate=0.3, burst=1, timeout=60, retries=1, headers=UA)
    bls = Http("bls", "https://www.bls.gov", rate=0.3, burst=1, timeout=30, retries=0, headers=UA)
    status: dict[str, str] = json.loads((cache / "official_status.json").read_text()) if (cache / "official_status.json").exists() else {}
    try:
        p = cache / "fomccalendars.htm"
        if not p.exists():
            try:
                f = await fed.get_bytes("/monetarypolicy/fomccalendars.htm")
                _write(p, f.data)
                status["fomccalendars"] = f"ok {len(f.data)} bytes"
            except ProviderError as e:
                status["fomccalendars"] = f"error {e}"[:200]
        print(f"fomc calendar: {status.get('fomccalendars')}", flush=True)
        for yr in range(2021, 2027):
            key = f"bls_{yr}"
            p = cache / f"bls_schedule_{yr}.htm"
            if p.exists() or key in status:
                continue
            try:
                f = await bls.get_bytes(f"/schedule/{yr}/home.htm")
                _write(p, f.data)
                status[key] = f"ok {len(f.data)} bytes"
            except ProviderError as e:
                status[key] = f"error {e}"[:200]
            print(f"bls {yr}: {status[key]}", flush=True)
            if status[key].startswith("error"):
                break  # one try: the site blocks scripts or is down, do not hammer it
    finally:
        _write(cache / "official_status.json", json.dumps(status, indent=1))
        await fed.aclose()
        await bls.aclose()

    jobs = kline_jobs(cache)
    todo = [j for j in jobs if not j[0].exists()]
    print(f"klines 5m: {len(jobs)} files, {len(todo)} to fetch", flush=True)
    if not todo:
        print("fetch done", flush=True)
        return
    _, rt = build_server()
    t0 = time.perf_counter()
    try:
        for i, (p, url) in enumerate(todo):
            for k in range(4):
                try:
                    f = await rt.binance_vision.http.get_bytes(url, tool="spot_klines")
                    rows = parse_klines_zip(f.data)
                    break
                except Exception as e:  # noqa: BLE001
                    print(f"  retry {url} after {type(e).__name__}: {str(e)[:120]}", flush=True)
                    await asyncio.sleep(5 * (k + 1))
            else:
                raise RuntimeError(f"giving up on {url}")
            _write(p, json.dumps([[int(r[0]), r[4]] for r in rows]))  # [open_ms, close] only
            del rows
            if i == 1:
                el = time.perf_counter() - t0
                print(f"  sample: 2 files in {el:.1f}s -> estimate {el / 2 * len(todo):.0f}s for all", flush=True)
            if i % 10 == 0 or i == len(todo) - 1:
                print(f"  {i + 1}/{len(todo)} {p.name} {time.perf_counter() - t0:.0f}s rss={rss_mb():.0f}MB", flush=True)
            await asyncio.sleep(0.2)
    finally:
        await rt.aclose()
    print("fetch done", flush=True)


# ----------------------------------------------------------------------------- load


def load_closes(cache: Path, asset: str) -> dict[int, float]:
    out: dict[int, float] = {}
    for p in sorted((cache / "klines5m").glob(f"{asset}USDT-5m-*.json")):
        for t, c in json.loads(p.read_text()):
            out[int(t)] = float(c)
    return out


MOVE: dict[date, date] = {}  # --move-event (sensitivity runs, added after the first run)


def load_events() -> list[Event]:
    ev = parse_events_csv(EVENTS_CSV.read_text())
    if MOVE:
        ev = [Event(MOVE.get(e.day, e.day), e.kind, e.status, e.verification, e.original) for e in ev]
        emit(f"Чутливість: дати подій перенесено {', '.join(f'{a} → {b}' for a, b in MOVE.items())}")
    return ev


def official_dates(cache: Path) -> tuple[dict[int, list[date]] | None, dict[int, dict[str, list[date]]]]:
    fomc = None
    p = cache / "fomccalendars.htm"
    if p.exists():
        fomc = parse_fomc_calendar(p.read_bytes().decode("utf-8", "replace"))
    bls: dict[int, dict[str, list[date]]] = {}
    for q in sorted(cache.glob("bls_schedule_*.htm")):
        yr = int(q.stem.rsplit("_", 1)[1])
        bls[yr] = parse_bls_schedule(q.read_bytes().decode("utf-8", "replace"), yr)
    return fomc, bls


def fed_decisions() -> list[date]:
    ch = parse_fed_changes((B1_CACHE / "fed_openmarket.htm").read_bytes().decode("utf-8-sig"))
    return [d - timedelta(days=1) for d, _ in ch if d >= FIRST]


# ----------------------------------------------------------------------------- dates stage (no prices)


def check_dates(cache: Path, events: Sequence[Event]) -> tuple[list[Event], list[str]]:
    """Events in the window with official corrections applied (registered rule: official date wins)."""
    notes: list[str] = []
    status = json.loads((cache / "official_status.json").read_text()) if (cache / "official_status.json").exists() else {}
    notes.append(f"- статус офіційних джерел: {status}")
    fomc, bls = official_dates(cache)
    ev = [e for e in events if e.kind in KINDS and e.status != "cancelled" and FIRST <= e.day <= LAST_EVENT]
    out: list[Event] = []
    fed_dec = set(fed_decisions())
    csv_fomc = {e.day for e in ev if e.kind == "fomc"}
    notes.append(
        f"- рішення зі зміною ставки (openmarket.htm, дата чинності − 1): {len(fed_dec)}; з них немає в CSV FOMC: {sorted(str(d) for d in fed_dec - csv_fomc)}"
    )
    if fomc is not None:
        off = {d for ds in fomc.values() for d in ds if FIRST <= d <= LAST_EVENT}
        notes.append(f"- fomccalendars.htm: роки {sorted(fomc)}; рішень у вікні {len(off)}")
        miss = sorted(off - csv_fomc)
        extra = sorted(d for d in csv_fomc if d.year in fomc and d not in off)
        notes.append(f"- FOMC: в офіційному, але не в CSV: {[str(d) for d in miss]}; у CSV, але не в офіційному: {[str(d) for d in extra]}")
    for e in ev:
        if e.kind == "fomc":
            ok = fomc is not None and e.day.year in fomc
            if ok and e.day not in fomc[e.day.year]:
                # registered rule: replace with the official decision date of the same month, if exactly one
                cands = [d for d in fomc[e.day.year] if d.month == e.day.month]
                if len(cands) == 1:
                    notes.append(f"- виправлено FOMC {e.day} → {cands[0]}")
                    e = Event(cands[0], e.kind, e.status, "fomccalendars")
                else:
                    notes.append(f"- FOMC {e.day}: в офіційному календарі нема, кандидатів у місяці {len(cands)}; лишено з позначкою")
                    e = Event(e.day, e.kind, e.status, "mismatch")
            else:
                e = Event(e.day, e.kind, e.status, "fomccalendars" if ok else "not_checked")
        else:
            yr = bls.get(e.day.year)
            if yr and yr.get(e.kind):
                if e.day in yr[e.kind]:
                    e = Event(e.day, e.kind, e.status, "bls_schedule")
                else:
                    cands = [d for d in yr[e.kind] if d.month == e.day.month]
                    if len(cands) == 1:
                        notes.append(f"- виправлено {e.kind} {e.day} → {cands[0]}")
                        e = Event(cands[0], e.kind, e.status, "bls_schedule")
                    else:
                        notes.append(f"- {e.kind} {e.day}: у розкладі BLS нема, кандидатів {len(cands)}; лишено з позначкою")
                        e = Event(e.day, e.kind, e.status, "mismatch")
            else:
                e = Event(e.day, e.kind, e.status, e.verification)
        out.append(e)
    if fomc is not None:
        added = sorted(off - csv_fomc - {e.day for e in out if e.kind == "fomc"})
        for d in added:
            notes.append(f"- додано FOMC з офіційного календаря: {d}")
            out.append(Event(d, "fomc", "official_added", "fomccalendars"))
    out.sort(key=lambda e: (e.day, e.kind))
    cnt = Counter((e.kind, e.verification) for e in out)
    notes.append(f"- події за видом і звіркою: {dict(sorted(cnt.items()))}")
    return out, notes


# ----------------------------------------------------------------------------- measurements


class Sample:
    """Per-event and per-placebo measurements for one asset (no aggregate statistics)."""

    def __init__(
        self, closes: dict[int, float], events: Sequence[Event], placebo: Sequence[date], comp: dict[date, tuple[float | None, float]]
    ):
        self.ev: list[dict[str, Any]] = []
        self.pl: dict[str, list[dict[str, Any]]] = {"08:30": [], "14:00": []}
        cdays = sorted(comp)
        prev = {b: a for a, b in zip(cdays[:-1], cdays[1:], strict=True)}
        for e in events:
            t0 = release_ms(e.day, e.kind)
            w = event_window(closes, t0)
            if w is None:
                continue
            w.update(day=e.day, kind=e.kind, status=e.status, verification=e.verification, clock="14:00" if e.kind == "fomc" else "08:30")
            # surprise proxy: COMP move known before the entry (registered H4)
            q = None
            if e.day in comp and e.day in prev:
                o, c = comp[e.day]
                pc = comp[prev[e.day]][1]
                if e.kind == "fomc":
                    q = math.log(c / pc)
                    w["entry_q"] = t0 + 2 * HOUR_MS + 5 * MIN_MS
                elif o is not None and abs(o / pc - 1) > 1e-9:
                    q = math.log(o / pc)
                    w["entry_q"] = t0 + HOUR_MS + 5 * MIN_MS
            w["q"] = q
            if q is not None:
                pa, pb = closes.get(w["entry_q"] - BAR_MS), closes.get(t0 + 24 * HOUR_MS - BAR_MS)
                w["Yq"] = math.log(pb / pa) if pa and pb else None
                w["Rq"] = math.log(pa / w["p0"]) if pa else None
            self.ev.append(w)
        for clock, kind in (("08:30", "cpi"), ("14:00", "fomc")):
            for d in placebo:
                w = event_window(closes, release_ms(d, kind))
                if w is None:
                    continue
                w.update(day=d, kind="placebo", clock=clock)
                self.pl[clock].append(w)


def col(rows: Sequence[dict[str, Any]], key: str) -> np.ndarray:
    return np.array([r[key] for r in rows if r.get(key) is not None], float)


def f(x: float | None, k: float = 1.0, d: int = 2) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * k:+.{d}f}"


def ci(c: tuple[float, float] | None, k: float = 1.0, d: int = 2) -> str:
    return f"[{c[0] * k:+.{d}f}; {c[1] * k:+.{d}f}]" if c else "—"


def h1(S: Sample, asset: str) -> list[tuple[str, str]]:
    emit(f"\n## H1 ({asset}). Аномальна волатильність: середнє |R(k)| подій / плацебо того самого часу")
    emit(
        "| вид | n подій / плацебо | k | |R| подій, % | |R| плацебо, % | ρ | 95 % CI | масштабоване ρ | 95 % CI |\n|---|---|---|---|---|---|---|---|---|"
    )
    verdicts = []
    for kind in KINDS:
        evs = [r for r in S.ev if r["kind"] == kind]
        pls = S.pl["14:00" if kind == "fomc" else "08:30"]
        for k in ("5m", "30m", "1h", "2h", "4h", "24h"):
            a, b = np.abs(col(evs, f"R{k}")), np.abs(col(pls, f"R{k}"))
            sa, sb = np.abs(col(evs, f"Rs{k}")), np.abs(col(pls, f"Rs{k}"))
            r = bootstrap_two(a, b, lambda x, y: float(x.mean() / y.mean()), reps=REPS, seed=SEED)
            rs = bootstrap_two(sa, sb, lambda x, y: float(x.mean() / y.mean()), reps=REPS, seed=SEED)
            main = k == "2h"
            emit(
                f"| {kind}{' **(головний)**' if main else ''} | {len(a)} / {len(b)} | {k} | {a.mean() * 100:.3f} | {b.mean() * 100:.3f} | "
                f"{r['stat']:.2f} | {ci(r['ci95'])} | {rs['stat']:.2f} | {ci(rs['ci95'])} |"
            )
            if main:
                v = h1_verdict(CLAUDE[kind], *r["ci95"])
                verdicts.append((kind, f"ρ(2 год) {r['stat']:.2f} {ci(r['ci95'])}, Claude {CLAUDE[kind]} → {v}"))
    for kind, s in verdicts:
        emit(f"- **{asset} {kind}: {s}**")
    # added after the first run (2026-10-18, critic review items 4 and 5); exploratory, no verdict
    alpha = 0.05 / N_MAIN_TESTS
    emit(
        f"- дослідницьке (додано після першого прогону): ρ(2 год) з CI з поправкою Бонферроні на {N_MAIN_TESTS} головних тестів "
        f"({1 - alpha:.2%}, {REPS_BONF} повторів): "
        + "; ".join(f"{kind} {ci(_ratio_ci(S, kind, '2h', alpha))}" for kind in KINDS)
        + "; те саме для 5 хв: "
        + "; ".join(f"{kind} {ci(_ratio_ci(S, kind, '5m', alpha))}" for kind in KINDS)
    )
    fri = [r for r in S.pl["08:30"] if r["day"].weekday() == 4]
    nfp = [r for r in S.ev if r["kind"] == "nfp"]
    parts = []
    for k in ("2h", "24h"):
        a, b = np.abs(col(nfp, f"R{k}")), np.abs(col(fri, f"R{k}"))
        r = bootstrap_two(a, b, lambda x, y: float(x.mean() / y.mean()), reps=REPS, seed=SEED)
        parts.append(f"{k} ×{r['stat']:.2f} {ci(r['ci95'])}")
    emit(f"- дослідницьке (додано після першого прогону): NFP проти плацебо 08:30 лише з п'ятниць (n {len(fri)}): " + "; ".join(parts))
    return verdicts


def _ratio_ci(S: Sample, kind: str, k: str, alpha: float) -> tuple[float, float] | None:
    """Percentile CI at level 1 - alpha for mean |R(k)| events / placebo (same resampling as bootstrap_two)."""
    x = np.abs(col([r for r in S.ev if r["kind"] == kind], f"R{k}"))
    y = np.abs(col(S.pl["14:00" if kind == "fomc" else "08:30"], f"R{k}"))
    rng = np.random.default_rng(SEED)
    v = np.array([x[rng.integers(0, len(x), len(x))].mean() / y[rng.integers(0, len(y), len(y))].mean() for _ in range(REPS_BONF)])
    return float(np.quantile(v, alpha / 2)), float(np.quantile(v, 1 - alpha / 2))


def h2(S: Sample, asset: str) -> None:
    emit(f"\n## H2 ({asset}). Напрям (описово)")
    emit(
        "| вид | n | середнє R(1 год), % | 95 % CI | частка R(1 год) > 0 | середнє R(24 год), % | 95 % CI |\n|---|---|---|---|---|---|---|"
    )
    for kind in (*KINDS, "плацебо 08:30", "плацебо 14:00"):
        rows = S.pl[kind[-5:]] if kind.startswith("плацебо") else [r for r in S.ev if r["kind"] == kind]
        a, b = col(rows, "R1h"), col(rows, "R24h")
        ba, bb = bootstrap_mean(a, reps=REPS, seed=SEED), bootstrap_mean(b, reps=REPS, seed=SEED)
        emit(
            f"| {kind} | {len(a)} | {f(ba['stat'], 100, 3)} | {ci(ba['ci95'], 100, 3)} | {float((a > 0).mean()):.0%} | {f(bb['stat'], 100, 3)} | {ci(bb['ci95'], 100, 3)} |"
        )


def cont_stats(evs: Sequence[dict[str, Any]], S: Sample, key: str) -> dict[str, Any]:
    """Mean of key over events, and the placebo difference (each event minus the placebo mean at its clock)."""
    rows = [r for r in evs if r.get(key) is not None]
    x = np.array([r[key] for r in rows], float)
    clocks = np.array([r["clock"] == "14:00" for r in rows])
    p8, p14 = col(S.pl["08:30"], key), col(S.pl["14:00"], key)
    b = bootstrap_mean(x, reps=REPS, seed=SEED)

    def diff(xe: np.ndarray, idx_p8: np.ndarray, idx_p14: np.ndarray, ce: np.ndarray) -> float:
        m8 = float(p8[idx_p8].mean()) if len(p8) else 0.0
        m14 = float(p14[idx_p14].mean()) if len(p14) else 0.0
        return float((xe - np.where(ce, m14, m8)).mean())

    rng = np.random.default_rng(SEED)
    pt = diff(x, np.arange(len(p8)), np.arange(len(p14)), clocks)
    vals = []
    for _ in range(REPS):
        i = rng.integers(0, len(x), len(x))
        vals.append(diff(x[i], rng.integers(0, len(p8), len(p8)), rng.integers(0, len(p14), len(p14)), clocks[i]))
    v = np.sort(np.array(vals))
    d = (
        {"stat": pt, "ci95": (float(np.quantile(v, 0.025)), float(np.quantile(v, 0.975)))}
        if len(x) > 2
        else {"stat": float("nan"), "ci95": None}
    )
    sd = float(x.std(ddof=1)) if len(x) > 2 else float("nan")
    return {"n": len(x), "mean": b, "diff": d, "sd": sd, "mde": mde(sd, len(x)), "x": x, "rows": rows}


def h3(S: Sample, asset: str, main: bool) -> dict[str, Any]:
    emit(f"\n## H3 ({asset}). Продовження чи відкат: z = sign R(1 год) · ln P(t0+24 год)/P(t0+1 год) / (σ_h √23)")
    emit(
        "| вибірка | n | SD | MDE | середнє z | 95 % CI | z − плацебо | 95 % CI | 2021–2023 | 2024–2026 | вердикт |\n|---|---|---|---|---|---|---|---|---|---|---|"
    )
    out = {}
    for lab, pred, gate in (
        ("**усі (головний)**", lambda r: True, GATE_POOLED),
        ("CPI", lambda r: r["kind"] == "cpi", GATE_KIND),
        ("NFP", lambda r: r["kind"] == "nfp", GATE_KIND),
        ("FOMC", lambda r: r["kind"] == "fomc", GATE_KIND),
    ):
        evs = [r for r in S.ev if pred(r)]
        c = cont_stats(evs, S, "z")
        h_a = [r["z"] for r in c["rows"] if r["day"] < HALF2]
        h_b = [r["z"] for r in c["rows"] if r["day"] >= HALF2]
        ma, mb = float(np.mean(h_a)), float(np.mean(h_b))
        v = continuation_verdict(c["mean"]["ci95"], c["diff"]["ci95"], (ma, mb), c["mde"], gate)
        out[lab] = (c, v, ma, mb)
        emit(
            f"| {lab} | {c['n']} | {c['sd']:.2f} | {c['mde']:.2f} | {f(c['mean']['stat'], d=3)} | {ci(c['mean']['ci95'], d=3)} | "
            f"{f(c['diff']['stat'], d=3)} | {ci(c['diff']['ci95'], d=3)} | {f(ma, d=3)} (n {len(h_a)}) | {f(mb, d=3)} (n {len(h_b)}) | {v} |"
        )
    for clock in ("08:30", "14:00"):
        p = col(S.pl[clock], "z")
        b = bootstrap_mean(p, reps=REPS, seed=SEED)
        emit(f"- плацебо {clock}: n {len(p)}, середнє z {f(b['stat'], d=3)} {ci(b['ci95'], d=3)}, SD {p.std(ddof=1):.2f}")
    # added after the first run (2026-10-18): the BTC FOMC lower bound printed as +0.000; more digits, no new statistic
    emit("- точні межі CI середнього z: " + "; ".join(f"{lab.strip('*')} {ci(c['mean']['ci95'], d=5)}" for lab, (c, *_rest) in out.items()))
    return out


def cutoff_ok(r: dict[str, Any], events: Sequence[Event]) -> bool:
    t0 = r["t0"]
    for e in events:
        t = release_ms(e.day, e.kind)
        if t0 < t <= t0 + 24 * HOUR_MS:
            return False
    return True


def robustness(S: Sample, asset: str, events: Sequence[Event], closes: dict[int, float]) -> None:
    emit(f"\n### Стійкість H3 ({asset}, усі події)")
    emit("| варіант | n | середнє | 95 % CI | − плацебо | 95 % CI |\n|---|---|---|---|---|---|")
    big = sorted((r for r in S.ev if r.get("C") is not None), key=lambda r: -abs(r["C"]))[:3]
    big_days = {(r["day"], r["kind"]) for r in big}
    variants: list[tuple[str, Callable[[dict[str, Any]], bool], str]] = [
        ("головний (z)", lambda r: True, "z"),
        ("сирий C, %", lambda r: True, "C"),
        (
            "без 3 найбільших |C| (" + ", ".join(f"{r['day']} {r['kind']} C {r['C'] * 100:+.1f} %, z {r['z']:+.1f}" for r in big) + ")",
            lambda r: (r["day"], r["kind"]) not in big_days,
            "z",
        ),
        ("без подій з іншим релізом у вікні 24 год", lambda r: cutoff_ok(r, events), "z"),
        ("без рядків unverified", lambda r: r["status"] != "unverified", "z"),
        ("вхід +30 хв, кінець 24 год", lambda r: True, "z_30m_24h"),
        ("вхід +2 год, кінець 24 год", lambda r: True, "z_2h_24h"),
        ("вхід +1 год, кінець 4 год", lambda r: True, "z_1h_4h"),
        ("вхід +1 год, кінець 8 год", lambda r: True, "z_1h_8h"),
        ("σ_h за 7 діб", lambda r: True, "z7"),
    ]
    for lab, pred, key in variants:
        evs = [r for r in S.ev if pred(r)]
        c = cont_stats(evs, S, key)
        k = 100 if key == "C" else 1
        emit(
            f"| {lab} | {c['n']} | {f(c['mean']['stat'], k, 3)} | {ci(c['mean']['ci95'], k, 3)} | {f(c['diff']['stat'], k, 3)} | {ci(c['diff']['ci95'], k, 3)} |"
        )
    # placebo 08:30 without Thursdays (jobless claims)
    p_all = S.pl["08:30"]
    S.pl["08:30"] = [r for r in p_all if r["day"].weekday() != 3]
    c = cont_stats(S.ev, S, "z")
    emit(
        f"| плацебо 08:30 без четвергів (n плацебо {len(S.pl['08:30'])}) | {c['n']} | {f(c['mean']['stat'], d=3)} | {ci(c['mean']['ci95'], d=3)} | {f(c['diff']['stat'], d=3)} | {ci(c['diff']['ci95'], d=3)} |"
    )
    S.pl["08:30"] = p_all


def rules(S: Sample, asset: str) -> dict[str, Any]:
    emit(f"\n## H5 і H4a ({asset}). Торгові правила, net на нотіонал за угоду (taker 0.05 % на бік)")
    emit(
        "| правило | варіант | n | середнє net, % | 95 % CI | MDE, % | 2021–2023 | 2024–2026 | частка > 0 | вердикт |\n|---|---|---|---|---|---|---|---|---|---|"
    )
    res = {}
    specs = [
        ("H5 продовження s від t0+1 год до t0+24 год", "s", "Y"),
        ("H4a проксі q (Nasdaq) від входу до t0+24 год", "q", "Yq"),
    ]
    for name, sk, yk in specs:
        for lab, cost, pred in (
            ("**головний**", COST, lambda r: True),
            ("витрати ×2", 2 * COST, lambda r: True),
            ("+ проковзування 0.02 % на бік", COST + SLIP, lambda r: True),
            ("без витрат", 0.0, lambda r: True),
            ("лише CPI", COST, lambda r: r["kind"] == "cpi"),
            ("лише NFP", COST, lambda r: r["kind"] == "nfp"),
            ("лише FOMC", COST, lambda r: r["kind"] == "fomc"),
        ):
            rows = [r for r in S.ev if pred(r) and r.get(sk) is not None and r.get(yk) is not None and r[sk] != 0]
            pnl = np.array([rule_pnl(r[sk], r[yk], cost=cost) for r in rows], float)
            if len(pnl) < 5:
                emit(f"| {name} | {lab} | {len(pnl)} | — | — | — | — | — | — | — |")
                continue
            b = bootstrap_mean(pnl, reps=REPS, seed=SEED)
            ha = [p for p, r in zip(pnl, rows, strict=True) if r["day"] < HALF2]
            hb = [p for p, r in zip(pnl, rows, strict=True) if r["day"] >= HALF2]
            m = mde(float(pnl.std(ddof=1)), len(pnl))
            ma = float(np.mean(ha)) if ha else float("nan")
            mb = float(np.mean(hb)) if hb else float("nan")
            v = rule_verdict(b["ci95"], (ma, mb), m, GATE_RULE) if "головний" in lab else ""
            emit(
                f"| {name} | {lab} | {len(pnl)} | {f(b['stat'], 100, 3)} | {ci(b['ci95'], 100, 3)} | {m * 100:.2f} | {f(ma, 100, 3)} | {f(mb, 100, 3)} | "
                f"{float((pnl > 0).mean()):.0%} | {v} |"
            )
            if v:
                res[name] = (b, v, m)
    # added after the first run (2026-10-18, critic review item 3); exploratory, no verdict
    big = sorted((r for r in S.ev if r.get("C") is not None), key=lambda r: -abs(r["C"]))[:3]
    big_days = {(r["day"], r["kind"]) for r in big}
    rows = [r for r in S.ev if (r["day"], r["kind"]) not in big_days and r.get("s") and r.get("Y") is not None]
    pnl = np.array([rule_pnl(r["s"], r["Y"], cost=COST) for r in rows], float)
    b = bootstrap_mean(pnl, reps=REPS, seed=SEED)
    big_lab = ", ".join(f"{r['day']} {r['kind']}" for r in big)
    emit(
        f"- дослідницьке (додано після першого прогону): H5 net без 3 найбільших |C| ({big_lab}): "
        f"n {len(pnl)}, {f(b['stat'], 100, 3)} % {ci(b['ci95'], 100, 3)}"
    )
    emit("- дзеркальне правило (відкат, дослідницьке, без вердикту) = −(валове H5) − 0.10 %")
    return res


def h4b(S: Sample, asset: str) -> None:
    emit(f"\n## H4b ({asset}, описово). Проксі сюрпризу: рух Nasdaq у день релізу")
    rows = [r for r in S.ev if r.get("q") is not None and r.get("Rq") is not None]
    for kind in KINDS:
        rr = [r for r in rows if r["kind"] == kind]
        x, y = np.array([r["q"] for r in rr]), np.array([r["Rq"] for r in rr])
        c = float(np.corrcoef(x, y)[0, 1]) if len(rr) > 3 else float("nan")
        emit(f"- {kind}: n {len(rr)}, кор(R_{asset} t0 → вхід, рух COMP проксі) {c:+.2f}")
    emit(f"| група | n | середнє C (продовження власного знака {asset}), % | 95 % CI |\n|---|---|---|---|")
    for lab, pred in (
        ("q > 0 (Nasdaq вгору)", lambda r: r["q"] > 0),
        ("q < 0 (Nasdaq вниз)", lambda r: r["q"] < 0),
        (f"{asset} за 1 год у бік Nasdaq", lambda r: r["s"] * r["q"] > 0),
        (f"{asset} за 1 год проти Nasdaq", lambda r: r["s"] * r["q"] < 0),
    ):
        x = np.array([r["C"] for r in rows if r.get("C") is not None and pred(r)], float)
        b = bootstrap_mean(x, reps=REPS, seed=SEED)
        emit(f"| {lab} | {len(x)} | {f(b['stat'], 100, 3)} | {ci(b['ci95'], 100, 3)} |")


def per_kind(S: Sample, asset: str) -> None:
    emit(f"\n### 5a ({asset}): неоднорідність за видами подій (z)")
    means, ses, ns = [], [], []
    for kind in KINDS:
        x = col([r for r in S.ev if r["kind"] == kind], "z")
        means.append(float(x.mean()))
        ses.append(float(x.std(ddof=1) / math.sqrt(len(x))))
        ns.append(len(x))
    q = cochran_q(means, ses)
    emit("- " + "; ".join(f"{k}: n {n}, z {m:+.3f} ± {s:.3f}" for k, n, m, s in zip(KINDS, ns, means, ses, strict=True)))
    emit(f"- Q Кохрена {q['Q']:.2f}, df {q['df']}, p {q['p']:.3f}, I² {q['I2']:.0%}")
    first = {k: float(col([r for r in S.ev if r["kind"] == k and r["day"] < HALF2], "z").mean()) for k in KINDS}
    second = {k: float(col([r for r in S.ev if r["kind"] == k and r["day"] >= HALF2], "z").mean()) for k in KINDS}
    order = sorted(KINDS, key=lambda k: -first[k])
    emit("- ранжування за 2021–2023 → 2024–2026: " + "; ".join(f"{k} {first[k]:+.3f} → {second[k]:+.3f}" for k in order))


def calendar_check(S: Sample, asset: str) -> None:
    emit(f"\n### Перевірка календаря даними ({asset}): |R(5 хв)| події як перцентиль плацебо того самого часу")
    for kind in KINDS:
        pl = np.sort(np.abs(col(S.pl["14:00" if kind == "fomc" else "08:30"], "R5m")))
        rows = [r for r in S.ev if r["kind"] == kind and r.get("R5m") is not None]
        pct = [float(np.searchsorted(pl, abs(r["R5m"])) / len(pl)) for r in rows]
        hi = sum(p > 0.75 for p in pct)
        low = [f"{r['day']}" for r, p in zip(rows, pct, strict=True) if p < 0.5]
        emit(
            f"- {kind}: n {len(rows)}, вище 75-го перцентиля плацебо {hi} ({hi / len(rows):.0%}); нижче медіани {len(low)}: {', '.join(low)}"
        )
    # added after the first run (2026-10-18, critic review item 1): reverse scan, placebo days that look like releases
    wd = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")
    for clock in ("08:30", "14:00"):
        pl = [r for r in S.pl[clock] if r.get("R5m") is not None]
        top = sorted(pl, key=lambda r: -abs(r["R5m"]))[:TOP_PLACEBO]
        ev_med = float(np.median(np.abs(col([r for r in S.ev if r["clock"] == clock], "R5m"))))
        emit(
            f"- зворотний скан ({asset}, {clock} ET): топ-{TOP_PLACEBO} плацебо-днів за |R(5 хв)| з {len(pl)} "
            f"(медіана подій цього часу {ev_med * 100:.2f} %): "
            + "; ".join(f"{i + 1}. {r['day']} {wd[r['day'].weekday()]} {abs(r['R5m']) * 100:.2f} %" for i, r in enumerate(top))
        )


# ----------------------------------------------------------------------------- stages


def prepare(cache: Path) -> tuple[list[Event], list[date], dict[date, tuple[float | None, float]], list[str]]:
    events_all = load_events()
    events, notes = check_dates(cache, events_all)
    comp = parse_nasdaq_historical(json.loads((B1_CACHE / "nasdaq_COMP.json").read_text()))
    tdays = sorted(d for d in comp if date(2021, 1, 4) <= d <= LAST_EVENT)
    excl = {e.day for e in events_all} | {e.day for e in events} | {e.original for e in events_all if e.original}
    pl = placebo_days(tdays, excl)
    notes.append(f"- плацебо-днів: {len(pl)} з {len(tdays)} торгових днів COMP")
    return events, pl, comp, notes


def stage_dates(cache: Path) -> None:
    emit("## Звірка дат подій (без цін)")
    events, pl, _, notes = prepare(cache)
    for n in notes:
        emit(n)
    emit(f"- подій у вибірці: {Counter(e.kind for e in events)}; за роками: {dict(sorted(Counter(e.day.year for e in events).items()))}")


def stage_gates(cache: Path) -> None:
    emit("## Санітарні перевірки і ворота потужності (без середніх)")
    events, pl, comp, notes = prepare(cache)
    for n in notes:
        emit(n)
    for asset in CRYPTO:
        closes = load_closes(cache, asset)
        ts = sorted(closes)
        per_day = Counter(datetime.fromtimestamp(t / 1000, UTC).date() for t in ts)
        short = {d: n for d, n in per_day.items() if n != 288 and d < LAST_DATA}
        dup = len(ts) - len(set(ts))
        emit(
            f"- {asset}: 5m свічок {len(ts)}, перша {datetime.fromtimestamp(ts[0] / 1000, UTC)}, остання {datetime.fromtimestamp(ts[-1] / 1000, UTC)}; дублі {dup}; днів ≠ 288 свічок: {len(short)} {dict(list(sorted(short.items()))[:12])}; rss {rss_mb():.0f} MB"
        )
        # consistency with the B1 hourly cache at the NY close
        hourly: dict[int, float] = {}
        for p in sorted((B1_CACHE / "klines").glob(f"{asset}USDT-1h-202[1-6]-*.json")):
            for r in json.loads(p.read_text()):
                hourly[int(r[0]) + HOUR_MS] = float(r[4])
        diffs = [abs(closes[t - BAR_MS] / v - 1) for t, v in hourly.items() if (t - BAR_MS) in closes]
        emit(f"- {asset}: close 5m на межі години проти годинного кешу B1: {len(diffs)} збігів, макс |різниця| {max(diffs):.2e}")
        del hourly
        if asset == "BTC":
            t = int(datetime(2021, 11, 10, 13, 30, tzinfo=UTC).timestamp() * 1000)
            emit(f"- BTC 2021-11-10 13:30 UTC (CPI): {closes.get(t - BAR_MS)} (того дня ATH ≈ 69 000)")
        S = Sample(closes, events, pl, comp)
        miss = len(events) - len(S.ev)
        emit(
            f"- {asset}: подій з повним вікном {len(S.ev)} (без вікна {miss}); плацебо 08:30 {len(S.pl['08:30'])}, 14:00 {len(S.pl['14:00'])}"
        )
        emit(f"- {asset}: подій за видами й роками: {dict(sorted(Counter((r['kind'], r['day'].year) for r in S.ev).items()))}")
        ov = [f"{r['day']} {r['kind']}" for r in S.ev if not cutoff_ok(r, events)]
        emit(f"- {asset}: подій з іншим релізом у вікні 24 год: {len(ov)} {ov}")
        zero = sum(1 for r in S.ev if r.get("s") == 0)
        emit(f"- {asset}: подій з R(1 год) = 0: {zero}; з проксі q: {sum(1 for r in S.ev if r.get('q') is not None)}")
        # power gates (no means)
        for lab, pred, gate in (
            ("усі", lambda r: True, GATE_POOLED),
            ("cpi", lambda r: r["kind"] == "cpi", GATE_KIND),
            ("nfp", lambda r: r["kind"] == "nfp", GATE_KIND),
            ("fomc", lambda r: r["kind"] == "fomc", GATE_KIND),
        ):
            z = col([r for r in S.ev if pred(r)], "z")
            sd = float(z.std(ddof=1))
            emit(
                f"- ворота {asset} {lab}: n {len(z)}, SD(z) {sd:.2f}, MDE {mde(sd, len(z)):.2f} (поріг {gate}) → {'пройдено' if mde(sd, len(z)) <= gate else 'НЕ пройдено'}"
            )
        for sk, yk, lab in (("s", "Y", "H5"), ("q", "Yq", "H4a")):
            pnl = np.array([rule_pnl(r[sk], r[yk], cost=COST) for r in S.ev if r.get(sk) and r.get(yk) is not None], float)
            m = mde(float(pnl.std(ddof=1)), len(pnl))
            emit(
                f"- ворота {asset} {lab}: n {len(pnl)}, SD {pnl.std(ddof=1) * 100:.2f} %, MDE {m * 100:.2f} % (поріг {GATE_RULE * 100:.1f} %)"
            )
        del S, closes


def stage_report(cache: Path, out: str, with_gates: bool = False) -> None:
    t_start = time.perf_counter()
    emit(f"# Сирий вивід verify_macro_release.py ({datetime.now(UTC).isoformat(timespec='seconds')})")
    emit(f"Бутстреп по подіях (iid; події й плацебо незалежно), {REPS} повторів, seed {SEED}. Ціни — close 5m свічок Binance spot.")
    emit(f"Таблиця подій: {EVENTS_CSV.name}, sha256 {hashlib.sha256(EVENTS_CSV.read_bytes()).hexdigest()[:16]}")
    events, pl, comp, notes = prepare(cache)
    summary: dict[str, Any] = {}
    for asset in CRYPTO:
        closes = load_closes(cache, asset)
        S = Sample(closes, events, pl, comp)
        emit(f"\n# {asset}: подій {len(S.ev)}, плацебо 08:30 {len(S.pl['08:30'])}, 14:00 {len(S.pl['14:00'])}; rss {rss_mb():.0f} MB")
        summary[asset] = {"h1": h1(S, asset)}
        h2(S, asset)
        summary[asset]["h3"] = h3(S, asset, asset == "BTC")
        robustness(S, asset, events, closes)
        summary[asset]["rules"] = rules(S, asset)
        h4b(S, asset)
        per_kind(S, asset)
        calendar_check(S, asset)
        if asset == "BTC":
            btc = S
        else:
            emit("\n### 5a: ETH − BTC (ті самі події)")
            for key, lab, k in (("z", "середнє z", 1), ("h5", "правило H5 net, %", 100)):

                def val(r: dict[str, Any], key: str = key) -> float | None:
                    if key == "h5":
                        return rule_pnl(r["s"], r["Y"], cost=COST) if r.get("Y") is not None and r.get("s") else None
                    return r.get(key)

                a = {(r["day"], r["kind"]): val(r) for r in btc.ev if val(r) is not None}
                b = {(r["day"], r["kind"]): val(r) for r in S.ev if val(r) is not None}
                common = sorted(set(a) & set(b))
                d = np.array([b[c] - a[c] for c in common], float)
                bb = bootstrap_mean(d, reps=REPS, seed=SEED)
                emit(f"- {lab} ETH − BTC: {f(bb['stat'], k, 3)} {ci(bb['ci95'], k, 3)} (n {len(d)})")
        del closes
    emit("\n## Зведення вердиктів")
    for asset in CRYPTO:
        for kind, s in summary[asset]["h1"]:
            emit(f"- {asset} H1 {kind}: {s}")
        for lab, (c, v, ma, mb) in summary[asset]["h3"].items():
            emit(
                f"- {asset} H3 {lab}: z {f(c['mean']['stat'], d=3)} {ci(c['mean']['ci95'], d=3)}, − плацебо {f(c['diff']['stat'], d=3)} {ci(c['diff']['ci95'], d=3)}, половини {f(ma, d=3)} / {f(mb, d=3)}, MDE {c['mde']:.2f} → {v}"
            )
        for name, (b, v, m) in summary[asset]["rules"].items():
            emit(f"- {asset} {name}: {f(b['stat'], 100, 3)} % {ci(b['ci95'], 100, 3)}, MDE {m * 100:.2f} % → {v}")
    emit(f"\n_час {time.perf_counter() - t_start:.0f} с, peak RSS {rss_mb():.0f} MB_")
    if with_gates:
        emit("")
        stage_gates(cache)
    if out:
        Path(out).expanduser().write_text("\n".join(L) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", choices=("prereg", "fetch", "dates", "gates", "report"), required=True)
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/macro_release")
    ap.add_argument("--out", default="")
    ap.add_argument("--with-gates", action="store_true", help="report stage: append the sanity/gates block to the output")
    ap.add_argument("--move-event", default="", help="sensitivity run: OLD:NEW moves the CSV row dated OLD (YYYY-MM-DD) to NEW")
    a = ap.parse_args()
    if a.move_event:
        old, new = (date.fromisoformat(x) for x in a.move_event.split(":"))
        MOVE[old] = new
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "prereg":
        stage_prereg(cache)
    elif a.stage == "fetch":
        (cache / "klines5m").mkdir(exist_ok=True)
        asyncio.run(fetch(cache))
    elif a.stage == "dates":
        stage_dates(cache)
    elif a.stage == "gates":
        stage_gates(cache)
    else:
        stage_report(cache, a.out, a.with_gates)
    if a.out and a.stage in ("dates", "gates"):
        Path(a.out).expanduser().write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
