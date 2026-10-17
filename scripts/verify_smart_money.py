"""Smart-money persistence, stage 0 (IDEA-20261015-smart-money-persistence).

Rules and thresholds are fixed in the card's section "Попередня реєстрація". Stages:
  uv run python scripts/verify_smart_money.py --stage fetch [--limit 20]   # portfolio + clearinghouseState + fills
  uv run python scripts/verify_smart_money.py --stage report --out ~/.cache/tradedesk-agents/smart_money/report.md
  uv run python scripts/verify_smart_money.py --stage align   # exploratory: live leaderboard vs portfolio, 60 accounts
Inputs: the collector tables in a *backup copy* of the database (``--db``: a ``.sqlite`` or ``.sqlite.gz``; default the
newest file in /var/lib/tradedesk-mcp/backups/, decompressed into the cache dir), and per-account JSON files from the
Hyperliquid info API (``portfolio``, ``clearinghouseState``, ``userFillsByTime``) fetched through the runtime provider
with weight pacing (HL: 1200 weight / min per IP; we keep 1100, the collector's hourly positions job needs ~350 per hour). Read-only, no keys.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import math
import resource
import shutil
import sqlite3
import statistics
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tradedesk.analytics.smart_money import (  # noqa: E402
    EXCLUDED_CLASSES,
    classify_account,
    detect_delta_start,
    ic_null_sd,
    interp,
    mde,
    mean_t_ci,
    net_flow,
    parse_portfolio,
    period_metrics,
    periods_needed,
    realized_pnl_windows,
    replay_positions,
    sd_ci95,
    step_at,
    step_change,
    window_change,
    window_edges,
)
from tradedesk.core.errors import ProviderError  # noqa: E402

DAY_MS = 86_400_000
E0 = int(datetime(2026, 10, 16, tzinfo=UTC).timestamp() * 1000)  # end of the latest evaluation window
SNAP_DAY = "2026-10-16"
MIN_VALUE = 500_000.0
N_MONTHS, N_WEEKS = 12, 4
WEIGHT_PER_MIN = 1100.0
FILLS_PAGE, FILLS_CAP = 2000, 10_000
IC_TARGET, D_TARGET = 0.05, 0.02
ICW_TARGET, DW_TARGET = 0.03, 0.005
H2_IC_TARGET, H3_EDGE_TARGET = 0.03, 0.03
BACKUPS = Path("/var/lib/tradedesk-mcp/backups")
OUT: dict[str, Any] = {"fh": None}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def f4(x: float | None, pct: bool = False) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "—"
    return f"{x * 100:+.2f} %" if pct else f"{x:+.4f}"


# ---------------------------------------------------------------------------------------------------------------------
# Database copy


def open_db(db: str | None, cache: Path) -> sqlite3.Connection:
    """Read-only connection to a backup copy; a .gz backup is decompressed once into the cache dir."""
    src = Path(db) if db else max(BACKUPS.glob("*.sqlite.gz"), key=lambda p: p.name)
    if src.suffix == ".gz":
        dst = cache / src.name[: -len(".gz")]
        if not dst.exists():
            tmp = dst.with_suffix(".part")
            with gzip.open(src) as f, open(tmp, "wb") as g:
                shutil.copyfileobj(f, g, 1 << 20)
            tmp.rename(dst)
        src = dst
    emit(f"db: {src}")
    return sqlite3.connect(f"file:{src}?mode=ro", uri=True)


def snapshot(conn: sqlite3.Connection, day: str, venue: str = "hyperliquid") -> dict[str, dict[str, Any]]:
    cur = conn.execute(
        "SELECT account, name, account_value, pnl_day, pnl_week, pnl_month, pnl_all, roi_month, vlm_month FROM trader_snapshot "
        "WHERE venue = ? AND day = ?",
        (venue, day),
    )
    cols = [d[0] for d in cur.description]
    return {r[0]: dict(zip(cols, r, strict=True)) for r in cur.fetchall()}


def tracked_accounts(conn: sqlite3.Connection, venue: str = "hyperliquid") -> list[str]:
    return [r[0] for r in conn.execute("SELECT account FROM trader_position_state WHERE venue = ? ORDER BY account", (venue,))]


# ---------------------------------------------------------------------------------------------------------------------
# Fetch


class Pacer:
    """Sleeps so that the info-API weight stays under WEIGHT_PER_MIN."""

    def __init__(self, per_min: float) -> None:
        self.per_s = per_min / 60.0
        self.next_at = time.monotonic()

    async def spend(self, weight: float) -> None:
        now = time.monotonic()
        if self.next_at > now:
            await asyncio.sleep(self.next_at - now)
        self.next_at = max(now, self.next_at) + weight / self.per_s


async def fetch(conn: sqlite3.Connection, cache: Path, limit: int | None) -> None:
    from tradedesk.server import build_server  # noqa: PLC0415

    snap = snapshot(conn, SNAP_DAY)
    accounts = sorted(snap, key=lambda a: -(snap[a]["pnl_month"] or 0.0))
    tracked = sorted(tracked_accounts(conn), key=lambda a: -((snap.get(a) or {}).get("pnl_month") or 0.0))  # best first
    if limit:
        accounts, tracked = accounts[:limit], tracked[: max(1, limit // 4)]
    for sub in ("portfolio", "state", "fills"):
        (cache / sub).mkdir(parents=True, exist_ok=True)
    _, rt = build_server()
    pacer = Pacer(WEIGHT_PER_MIN)
    t0, done, failed = time.time(), 0, Counter()
    try:
        for i, a in enumerate(accounts):
            for sub, payload, weight in (
                ("portfolio", {"type": "portfolio", "user": a}, 20),
                ("state", {"type": "clearinghouseState", "user": a}, 2),
            ):
                path = cache / sub / f"{a}.json"
                if path.exists():
                    continue
                await pacer.spend(weight)
                try:
                    data = (await rt.hyperliquid._info(payload, ttl=0)).data
                except ProviderError as e:
                    failed[sub] += 1
                    if failed[sub] <= 3:
                        emit(f"  {sub} {a}: {e}")
                    continue
                if sub == "portfolio":
                    data = {k: v for k, v in parse_portfolio(data).items() if k in ("allTime", "month", "week")}
                path.write_text(json.dumps(data, separators=(",", ":")))
                done += 1
            if (i + 1) % 100 == 0 or i + 1 == len(accounts):
                el = time.time() - t0
                emit(f"[accounts {i + 1}/{len(accounts)}] new files {done}, failed {dict(failed)}, {el:.0f}s, rss {rss_mb():.0f} MB")
        start = E0 - (N_MONTHS + 1) * 30 * DAY_MS
        for i, a in enumerate(tracked):
            path = cache / "fills" / f"{a}.json"
            if path.exists():
                continue
            # Pages go forward in time from ``start``; our own FILLS_CAP truncates the *end* of the history, so a capped
            # file is complete only over [start, last loaded fill] (``coverage_end``). The page boundary is re-requested
            # from the last fill's time (not +1 ms: fills sharing a millisecond would be lost) and de-duplicated by tid.
            # Note: the 2026-10-17 cache was fetched by the earlier version (since = last time + 1, no coverage_end).
            fills: list[dict[str, Any]] = []
            seen: set[Any] = set()
            since, ok = start, True
            while len(fills) < FILLS_CAP:
                await pacer.spend(20)
                try:
                    page = (await rt.hyperliquid._info({"type": "userFillsByTime", "user": a, "startTime": since}, ttl=0)).data or []
                except ProviderError as e:
                    failed["fills"] += 1
                    emit(f"  fills {a}: {e}")
                    ok = False
                    break
                await pacer.spend(len(page) / 20)
                new = 0
                for f in page:
                    if not isinstance(f, dict):
                        continue
                    key = f.get("tid") if f.get("tid") is not None else (f.get("hash"), f.get("time"), f.get("oid"), f.get("sz"))
                    if key in seen:
                        continue
                    seen.add(key)
                    new += 1
                    fills.append(
                        {
                            "time": f.get("time"),
                            "tid": f.get("tid"),
                            "coin": f.get("coin"),
                            "closedPnl": f.get("closedPnl"),
                            "fee": f.get("fee"),
                        }
                    )
                if len(page) < FILLS_PAGE or new == 0:
                    break
                since = int(page[-1]["time"])
            if ok:
                capped = len(fills) >= FILLS_CAP
                end = int(fills[-1]["time"]) if capped and fills else None
                path.write_text(
                    json.dumps({"start": start, "capped": capped, "coverage_start": start, "coverage_end": end, "fills": fills})
                )
            if (i + 1) % 20 == 0 or i + 1 == len(tracked):
                emit(f"[fills {i + 1}/{len(tracked)}] failed {dict(failed)}, {time.time() - t0:.0f}s, rss {rss_mb():.0f} MB")
    finally:
        await rt.aclose()
    emit(f"fetch done in {time.time() - t0:.0f}s, failed {dict(failed)}")


# ---------------------------------------------------------------------------------------------------------------------
# Analysis


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def classes(snap: dict[str, dict[str, Any]], cache: Path) -> dict[str, str]:
    out = {}
    for a, row in snap.items():
        st = load_json(cache / "state" / f"{a}.json")
        positions = None
        if isinstance(st, dict):
            positions = []
            for ap in st.get("assetPositions") or []:
                p = ap.get("position") if isinstance(ap, dict) else None
                if isinstance(p, dict):
                    szi = float(p.get("szi") or 0.0)
                    if szi:
                        positions.append({"side": "long" if szi > 0 else "short", "value_usd": float(p.get("positionValue") or 0.0)})
        out[a] = classify_account(row["account_value"], row["vlm_month"], positions)
    return out


def section_classes(snap: dict[str, dict[str, Any]], cls: dict[str, str], cache: Path) -> None:
    emit("## 1. Класи акаунтів HL (знімок 2026-10-16, позиції — clearinghouseState на момент прогону)")
    emit()
    tot_av = sum(r["account_value"] or 0 for r in snap.values())
    emit("| Клас | Акаунтів | Частка | Вартість, $ млн | Медіана turnover | Медіана lev |")
    emit("|---|---|---|---|---|---|")
    for c in ("system", "mm", "hft_directional", "hedger", "directional", "idle", "unknown"):
        accs = [a for a in snap if cls[a] == c]
        if not accs:
            emit(f"| {c} | 0 | 0 % | 0 | — | — |")
            continue
        av = sum(snap[a]["account_value"] or 0 for a in accs)
        to = statistics.median((snap[a]["vlm_month"] or 0) / snap[a]["account_value"] for a in accs if snap[a]["account_value"])
        levs = []
        for a in accs:
            st = load_json(cache / "state" / f"{a}.json")
            if isinstance(st, dict) and snap[a]["account_value"]:
                g = float((st.get("marginSummary") or {}).get("totalNtlPos") or 0.0)
                levs.append(g / snap[a]["account_value"])
        emit(
            f"| {c} | {len(accs)} | {len(accs) / len(snap) * 100:.1f} % | {av / 1e6:,.0f} | {to:.1f} "
            f"| {statistics.median(levs) if levs else float('nan'):.2f} |"
        )
    emit(
        f"\nУсього {len(snap)} акаунтів, ${tot_av / 1e9:.2f} млрд. Без позицій у кеші: {sum(1 for a in snap if not (cache / 'state' / f'{a}.json').exists())}."
    )
    top = sorted(snap, key=lambda a: -(snap[a]["pnl_month"] or 0))[: len(snap) // 5]
    emit(f"Топ-квінтиль за PnL 30 днів ({len(top)}): " + ", ".join(f"{k} {v}" for k, v in Counter(cls[a] for a in top).most_common()))
    t100 = top[:100]
    emit("Топ-100 за PnL 30 днів: " + ", ".join(f"{k} {v}" for k, v in Counter(cls[a] for a in t100).most_common()))
    emit()


def _dist(name: str, rel: list[float]) -> None:
    if not rel:
        emit(f"- {name}: n = 0")
        return
    rel = sorted(rel)
    emit(
        f"- {name}: n = {len(rel)}; медіана відносної розбіжності {statistics.median(rel) * 100:.1f} %, "
        f"p75 {rel[int(0.75 * len(rel))] * 100:.1f} %, p90 {rel[int(0.9 * len(rel))] * 100:.1f} %, частка < 10 %: {sum(x < 0.1 for x in rel) / len(rel):.2f}"
    )


def sanity(conn: sqlite3.Connection, snap: dict[str, dict[str, Any]], cache: Path) -> None:
    """Registered check (median < 10 %) plus the diagnostic variants added after it failed (exploratory)."""
    emit("## 0. Перевірка узгодженості: portfolio.month проти pnl_month знімка 16.10")
    emit()
    row = conn.execute("SELECT MIN(ts), MAX(ts) FROM trader_snapshot WHERE venue = 'hyperliquid' AND day = ?", (SNAP_DAY,)).fetchone()
    snap_ms = int(row[1]) * 1000 if row and row[1] else None
    rel, rel_al, rel_w, rel_a, shift_h = [], [], [], [], []
    for a, r in snap.items():
        pf = load_json(cache / "portfolio" / f"{a}.json")
        if not pf or "month" not in pf or not pf["month"]["pnl"] or r["pnl_month"] is None:
            continue
        pts = pf["month"]["pnl"]
        if snap_ms:
            shift_h.append((pts[-1][0] - snap_ms) / 3.6e6)
        if abs(r["pnl_month"]) >= 50_000:
            rel.append(abs(pts[-1][1] - pts[0][1] - r["pnl_month"]) / abs(r["pnl_month"]))
            if snap_ms:
                at = step_at(pts, snap_ms)  # last month point at or before the snapshot time
                if at is not None:
                    rel_al.append(abs(at - pts[0][1] - r["pnl_month"]) / abs(r["pnl_month"]))
        w = (pf.get("week") or {}).get("pnl") or []
        if w and r["pnl_week"] is not None and abs(r["pnl_week"]) >= 50_000:
            rel_w.append(abs(w[-1][1] - w[0][1] - r["pnl_week"]) / abs(r["pnl_week"]))
        al = (pf.get("allTime") or {}).get("pnl") or []
        if al and r["pnl_all"] is not None and abs(r["pnl_all"]) >= 50_000:
            rel_a.append(abs(al[-1][1] - r["pnl_all"]) / abs(r["pnl_all"]))
    _dist("**зареєстрована** (місяць, |pnl_month| ≥ $50k, поріг медіани < 10 %)", rel)
    if rel:
        emit(f"  → зареєстрована перевірка: {'✓' if statistics.median(rel) < 0.10 else '✗ ПРОВАЛЕНА'}")
    emit("Розвідувально (додано після провалу зареєстрованої перевірки):")
    if shift_h:
        emit(f"- зсув останньої точки portfolio.month відносно знімка: медіана {statistics.median(shift_h):+.1f} год")
    _dist("місяць, кінець вирівняно на час знімка (остання точка ≤ ts знімка; початок вікна не вирівняно)", rel_al)
    _dist("тиждень (|pnl_week| ≥ $50k, без вирівнювання)", rel_w)
    _dist("увесь час (|pnl_all| ≥ $50k)", rel_a)
    emit()


async def align_live(n: int = 60, seed: int = 20261017) -> None:
    """Exploratory: the live leaderboard vs ``portfolio`` fetched right after, for ``n`` random accounts with
    |pnl_month| >= $50k (the same moment, so a window shift cannot explain a gap). Network via the runtime provider."""
    import random  # noqa: PLC0415

    from tradedesk.providers.hyperliquid import parse_leaderboard  # noqa: PLC0415
    from tradedesk.server import build_server  # noqa: PLC0415

    _, rt = build_server()
    pacer = Pacer(WEIGHT_PER_MIN)
    try:
        lb = parse_leaderboard((await rt.hyperliquid.leaderboard()).data, MIN_VALUE)
        t0 = time.time()
        sample = random.Random(seed).sample([r for r in lb if r.get("pnl_month") and abs(r["pnl_month"]) >= 50_000], n)
        rel_m, rel_w, rel_a = [], [], []
        for r in sample:
            await pacer.spend(20)
            try:
                pf = parse_portfolio((await rt.hyperliquid._info({"type": "portfolio", "user": r["account"]}, ttl=0)).data)
            except ProviderError as e:
                emit(f"  portfolio {r['account']}: {e}")
                continue
            m, w, a = pf["month"]["pnl"], pf["week"]["pnl"], pf["allTime"]["pnl"]
            rel_m.append(abs((m[-1][1] - m[0][1]) - r["pnl_month"]) / abs(r["pnl_month"]))
            if r.get("pnl_week") and abs(r["pnl_week"]) >= 50_000:
                rel_w.append(abs((w[-1][1] - w[0][1]) - r["pnl_week"]) / abs(r["pnl_week"]))
            if r.get("pnl_all") and abs(r["pnl_all"]) >= 50_000:
                rel_a.append(abs(a[-1][1] - r["pnl_all"]) / abs(r["pnl_all"]))
        emit(f"## Розвідувальна перевірка узгодженості з живим лідербордом ({datetime.now(UTC):%Y-%m-%d %H:%M} UTC, seed {seed})")
        emit(f"leaderboard rows {len(lb)}, sample fetched over {time.time() - t0:.0f}s, rss {rss_mb():.0f} MB")
        _dist("місяць", rel_m)
        _dist("тиждень (|pnl_week| ≥ $50k)", rel_w)
        _dist("увесь час (|pnl_all| ≥ $50k)", rel_a)
    finally:
        await rt.aclose()


def _edge_fns(method: str) -> tuple[Any, Any]:
    """'step' (point-in-time: last point <= edge; no look-ahead) or 'interp' (the registered linear interpolation,
    which leaks the segment after a formation edge into the signal; kept only for comparison)."""
    return (step_at, step_change) if method == "step" else (interp, window_change)


def retro_monthly(
    snap: dict[str, dict[str, Any]], cls: dict[str, str], cache: Path, drop: set[str], method: str = "step"
) -> list[dict[str, Any]]:
    """Per pair (formation k+1 -> evaluation k): metrics over the point-in-time universe."""
    at, change = _edge_fns(method)
    edges = window_edges(E0, 30 * DAY_MS, N_MONTHS + 1)  # ascending, len N+2
    per: list[dict[str, list[float]]] = [{"sig": [], "out": [], "sig_roi": []} for _ in range(N_MONTHS)]
    for a in snap:
        if cls[a] in drop:
            continue
        pf = load_json(cache / "portfolio" / f"{a}.json")
        if not pf or "allTime" not in pf:
            continue
        pnl, val = pf["allTime"]["pnl"], pf["allTime"]["value"]
        for j in range(N_MONTHS):  # formation [edges[j], edges[j+1]], evaluation [edges[j+1], edges[j+2]]
            v_mid, v_start = at(val, edges[j + 1]), at(val, edges[j])
            if v_mid is None or v_mid < MIN_VALUE:
                continue
            s = change(pnl, edges[j], edges[j + 1])
            o = change(pnl, edges[j + 1], edges[j + 2])
            if s is None or o is None:
                continue
            per[j]["sig"].append(s)
            per[j]["out"].append(o / v_mid)
            per[j]["sig_roi"].append(s / v_start if v_start and v_start > 0 else s / v_mid)
    rows = []
    for j in range(N_MONTHS):
        sig, out = per[j]["sig"], per[j]["out"]
        m = period_metrics(sig, out)
        mr = period_metrics(per[j]["sig_roi"], out)
        # without the top 1 % by |formation PnL|
        if len(sig) >= 100:
            keep = sorted(range(len(sig)), key=lambda i: -abs(sig[i]))[max(1, len(sig) // 100) :]
            m1 = period_metrics([sig[i] for i in keep], [out[i] for i in keep])
        else:
            m1 = {"ic": None, "d": None}
        rows.append(
            {
                "eval_end": datetime.fromtimestamp(edges[j + 2] / 1000, UTC).date().isoformat(),
                "n": m["n"],
                "ic": m["ic"],
                "d": m["d"],
                "ic_roi": mr["ic"],
                "d_roi": mr["d"],
                "ic_x1": m1["ic"],
                "d_x1": m1["d"],
                "top_med": m["top_median"],
                "rest_med": m["rest_median"],
            }
        )
    return rows


def retro_weekly(snap: dict[str, dict[str, Any]], cls: dict[str, str], cache: Path, drop: set[str]) -> list[dict[str, Any]]:
    edges = window_edges(E0, 7 * DAY_MS, N_WEEKS)  # len N_WEEKS+1
    per: list[tuple[list[float], list[float]]] = [([], []) for _ in range(N_WEEKS - 1)]
    for a in snap:
        if cls[a] in drop:
            continue
        pf = load_json(cache / "portfolio" / f"{a}.json")
        if not pf or "month" not in pf:
            continue
        pnl, val = pf["month"]["pnl"], pf["month"]["value"]
        for j in range(N_WEEKS - 1):
            v_mid = step_at(val, edges[j + 1])
            if v_mid is None or v_mid < MIN_VALUE:
                continue
            s, o = step_change(pnl, edges[j], edges[j + 1]), step_change(pnl, edges[j + 1], edges[j + 2])
            if s is None or o is None:
                continue
            per[j][0].append(s)
            per[j][1].append(o / v_mid)
    rows = []
    for j in range(N_WEEKS - 1):
        m = period_metrics(*per[j])
        rows.append({"eval_end": datetime.fromtimestamp(edges[j + 2] / 1000, UTC).date().isoformat(), **m})
    return rows


def retro_fills(
    snap: dict[str, dict[str, Any]], cls: dict[str, str], cache: Path, tracked: list[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    edges = window_edges(E0, 30 * DAY_MS, N_MONTHS + 1)
    per: list[tuple[list[float], list[float]]] = [([], []) for _ in range(N_MONTHS)]
    stats = {"tracked": len(tracked), "files": 0, "capped": 0, "no_fills": 0, "end_days_ago": []}
    for a in tracked:
        fz = load_json(cache / "fills" / f"{a}.json")
        if not fz:
            continue
        stats["files"] += 1
        stats["capped"] += bool(fz["capped"])
        if not fz["fills"]:
            stats["no_fills"] += 1
        # The fetch paged forward from ``start`` and our own cap cut the END of the history: a capped file is
        # complete over [start, last loaded fill], not from its first fill (the cached ``coverage_start`` is wrong).
        cov_start = fz["start"]
        cov_end = fz.get("coverage_end")
        if fz["capped"] and fz["fills"] and cov_end is None:
            cov_end = int(fz["fills"][-1]["time"])
        if cov_end is not None:
            stats["end_days_ago"].append((E0 - cov_end) / DAY_MS)
        if cls.get(a) in EXCLUDED_CLASSES:
            continue
        wins = realized_pnl_windows(fz["fills"], edges, cov_start, cov_end)
        pf = load_json(cache / "portfolio" / f"{a}.json")
        val = pf["allTime"]["value"] if pf and "allTime" in pf else []
        for j in range(N_MONTHS):
            v_mid = step_at(val, edges[j + 1])
            if wins[j] is None or wins[j + 1] is None or v_mid is None or v_mid < MIN_VALUE:
                continue
            per[j][0].append(wins[j])
            per[j][1].append(wins[j + 1] / v_mid)
    rows = []
    for j in range(N_MONTHS):
        m = period_metrics(*per[j])
        rows.append({"eval_end": datetime.fromtimestamp(edges[j + 2] / 1000, UTC).date().isoformat(), **m})
    return rows, stats


def print_periods(title: str, rows: list[dict[str, Any]], extra: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    emit(title)
    emit()
    if extra:
        emit("| Оцінка до | n | IC | D (медіани ROI) | IC без топ-1 % | D без топ-1 % | IC (сигнал ROI) | Медіана топ / решта |")
        emit("|---|---|---|---|---|---|---|---|")
    else:
        emit("| Оцінка до | n | IC | D (медіани ROI) |")
        emit("|---|---|---|---|")
    for r in rows:
        if extra:
            emit(
                f"| {r['eval_end']} | {r['n']} | {f4(r['ic'])} | {f4(r['d'], True)} | {f4(r['ic_x1'])} | {f4(r['d_x1'], True)} "
                f"| {f4(r['ic_roi'])} | {f4(r['top_med'], True)} / {f4(r['rest_med'], True)} |"
            )
        else:
            emit(f"| {r['eval_end']} | {r['n']} | {f4(r['ic'])} | {f4(r.get('d'), True)} |")
    ic = mean_t_ci([r["ic"] for r in rows if r["ic"] is not None])
    d = mean_t_ci([r["d"] for r in rows if r["d"] is not None])
    emit()
    stats = [("IC", ic, False), ("D", d, True)]
    if extra:
        stats += [
            ("IC без топ-1 %", mean_t_ci([r["ic_x1"] for r in rows if r.get("ic_x1") is not None]), False),
            ("D без топ-1 %", mean_t_ci([r["d_x1"] for r in rows if r.get("d_x1") is not None]), True),
        ]
    for name, s, pct in stats:
        emit(
            f"- {name}: T = {s['n']}, середнє {f4(s['mean'], pct)}, sd між періодами {f4(s['sd'], pct)}, 95 % CI [{f4(s['lo'], pct)}; {f4(s['hi'], pct)}]"
        )
    emit()
    return ic, d


def power_line(name: str, sd: float | None, target: float, unit_days: int, pct: bool = False, n_sd: int | None = None) -> None:
    if sd is None:
        emit(f"- {name}: sd невідоме")
        return
    n = periods_needed(sd, target)
    rng = ""
    if n_sd and n_sd >= 2:
        lo, hi = sd_ci95(sd, n_sd)
        n_lo, n_hi = periods_needed(lo, target), periods_needed(hi, target)
        rng = (
            f"; 95 % CI для sd з {n_sd} періодів [{f4(lo, pct)}; {f4(hi, pct)}] → {n_lo} … {n_hi} періодів "
            f"(≈ {n_lo * unit_days / 30.4:.0f} … {n_hi * unit_days / 30.4:.0f} міс.)"
        )
    emit(
        f"- {name}: sd {f4(sd, pct)} → для MDE ≤ {f4(target, pct)} треба {n} незалежних періодів ≈ {n * unit_days} днів "
        f"(≈ {n * unit_days / 30.4:.1f} міс.){rng}; MDE після 3 міс. = {f4(mde(sd, max(1, 90 // unit_days)), pct)}, "
        f"після 6 міс. = {f4(mde(sd, max(1, 182 // unit_days)), pct)}"
    )


def position_runs(conn: sqlite3.Connection, venue: str) -> tuple[list[dict[str, Any]], list[int], int | None]:
    cur = conn.execute(
        "SELECT ts, account, market, side, size, value_usd, entry_px, title FROM trader_positions WHERE venue = ? ORDER BY ts", (venue,)
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    runs = sorted({r["ts"] for r in rows})
    # the cutover is detected over both venues (the same migration)
    closed = [r[0] for r in conn.execute("SELECT ts FROM trader_positions WHERE side = 'closed'")]
    return rows, runs, detect_delta_start(sorted({r[0] for r in conn.execute("SELECT DISTINCT ts FROM trader_positions")}), closed)


def section_h2_h3(conn: sqlite3.Connection, snap: dict[str, dict[str, Any]], cls: dict[str, str]) -> None:
    emit("## 5. H2 і H3: скільки спостережень дають дані колектора")
    emit()
    rows, runs, delta = position_runs(conn, "hyperliquid")
    emit(
        f"HL: {len(rows)} рядків, {len(runs)} прогонів, з {datetime.fromtimestamp(runs[0], UTC)} до {datetime.fromtimestamp(runs[-1], UTC)}; перехід на журнал змін (v12) з {delta and datetime.fromtimestamp(delta, UTC)}."
    )
    first, last = runs[0], runs[-1]
    s0, s1 = replay_positions(rows, delta, first), replay_positions(rows, delta, last)
    tracked = sorted(set(s0) | set(s1))
    univ = [a for a in snap if cls[a] not in EXCLUDED_CLASSES and (snap[a]["account_value"] or 0) >= MIN_VALUE]
    q = sorted(univ, key=lambda a: -(snap[a]["pnl_month"] or 0))[: len(univ) // 5]
    strong = [a for a in tracked if a in set(q)]
    with_pos = [a for a in tracked if s1.get(a)]
    emit(
        f"Відстежуваних HL: {len(tracked)}, з позиціями на останньому прогоні: {len(with_pos)}; «сильних» (топ-квінтиль H1-універсуму): {len(strong)}, з позиціями: {sum(1 for a in strong if s1.get(a))}."
    )
    flow_all, flow_strong = net_flow(s0, s1, tracked), net_flow(s0, s1, strong)
    n_all = sum(1 for v in flow_all.values() if abs(v) > 0)
    n_str = sum(1 for v in flow_strong.values() if abs(v) > 0)
    emit(
        f"Монет зі зміною позиції за ~24 год ({datetime.fromtimestamp(first, UTC):%m-%d %H:%M} → {datetime.fromtimestamp(last, UTC):%m-%d %H:%M}): усі відстежувані {n_all}, сильні {n_str}."
    )
    for label, n in (("усі відстежувані", n_all), ("сильні", n_str)):
        if n < 3:
            emit(f"- H2 ({label}): N = {n} монет — IC не визначений; день не пройшов би фільтр ≥ 10 монет.")
            continue
        sd = ic_null_sd(n)
        for infl, tag in ((1.0, "незалежні сигнали"), (math.sqrt(7), "стійкий сигнал, ×√7")):
            days = periods_needed(sd * infl, H2_IC_TARGET)
            emit(
                f"- H2 ({label}, N = {n}, {tag}): sd(IC_d) ≈ {sd * infl:.3f} → для MDE ≤ 0.03 треба ≈ {days} днів (≈ {days / 30.4:.1f} міс.)"
            )
    emit()
    pm, pm_runs, _ = position_runs(conn, "polymarket")
    pre = [t for t in pm_runs if delta is None or t < delta]
    new_pos, markets = 0, set()
    prev = replay_positions(pm, delta, pre[0]) if pre else {}
    for t in pre[1:]:
        cur = replay_positions(pm, delta, t)
        for a, ps in cur.items():
            for m, p in ps.items():
                if a in prev and m not in prev[a]:
                    px = (p.get("value_usd") or 0) / p["size"] if p.get("size") else None
                    if px is not None and 0.03 <= px <= 0.97:
                        new_pos += 1
                        markets.add(p.get("title") or m)
        prev = cur
    hours = (pre[-1] - pre[0]) / 3600 if len(pre) > 1 else 0
    emit(
        f"Polymarket: нових позицій (ціна спостереження в [0.03; 0.97]) за {hours:.0f} год до переходу на журнал змін: {new_pos}, різних ринків: {len(markets)}."
    )
    if hours and markets:
        # Power of H3 is NOT estimated here: the same markets recur day after day, "new" positions before v12 include
        # threshold crossings, the cluster is the event (not the market) and only part of the markets resolve in time.
        # Needs distinct Gamma events with new positions and their endDate from the v12 log (before registering H3).
        emit(
            f"- H3: потужність не оцінена. Є лише верхня межа кількості: {len(markets)} різних ринків за {hours:.0f} год "
            "(ринки з дня в день повторюються; кластер — подія; вирішується лише частина)."
        )
    emit()


def report(conn: sqlite3.Connection, cache: Path) -> None:
    snap = snapshot(conn, SNAP_DAY)
    cls = classes(snap, cache)
    have = sum(1 for a in snap if (cache / "portfolio" / f"{a}.json").exists())
    emit(f"# Smart money, стадія 0 — сирий вивід ({datetime.now(UTC):%Y-%m-%d %H:%M} UTC)")
    emit()
    emit(f"Знімок HL {SNAP_DAY}: {len(snap)} акаунтів, portfolio у кеші: {have}. rss {rss_mb():.0f} MB")
    emit()
    sanity(conn, snap, cache)
    section_classes(snap, cls, cache)
    rows = retro_monthly(snap, cls, cache, set(EXCLUDED_CLASSES), method="step")
    ic, d = print_periods(
        "## 2. Ретроспектива H1, джерело A (portfolio, 30 днів → 30 днів, без system/mm; survivorship!). "
        "Межі вікон — остання точка ≤ межі (без lookahead; виправлення після рев'ю критика)",
        rows,
        extra=True,
    )
    rows_i = retro_monthly(snap, cls, cache, set(EXCLUDED_CLASSES), method="interp")
    ic_i, d_i = print_periods(
        "### 2i. Те саме із зареєстрованою лінійною інтерполяцією на межах (має lookahead: сигнал бачить сегмент після межі)",
        rows_i,
        extra=True,
    )
    rows_hft = retro_monthly(snap, cls, cache, set(EXCLUDED_CLASSES) | {"hft_directional"})
    print_periods("### 2a. Те саме без hft_directional (зареєстрований варіант)", rows_hft)
    rows_all = retro_monthly(snap, cls, cache, set())
    print_periods("### 2b. Те саме з усіма класами (без фільтра MM; не зареєстровано)", rows_all)
    rows_idle = retro_monthly(snap, cls, cache, set(EXCLUDED_CLASSES) | {"idle", "unknown"})
    print_periods("### 2c. Без idle / unknown (додано після часткового прогону на 160 акаунтах; постфактум, розвідувально)", rows_idle)
    wk = retro_weekly(snap, cls, cache, set(EXCLUDED_CLASSES))
    icw, dw = print_periods("## 3. Ретроспектива H1w (portfolio.month, 7 днів → 7 днів, межі — остання точка ≤ межі)", wk)
    fr, fs = retro_fills(snap, cls, cache, tracked_accounts(conn))
    ed = sorted(fs["end_days_ago"])
    emit("## 4. Ретроспектива H1, джерело B (userFillsByTime, реалізований PnL, відстежувані акаунти) — ◯, не оцінювалось")
    emit()
    emit(
        f"Відстежуваних {fs['tracked']}, файлів {fs['files']} (fetch зупинено), обрізаних НАШИМ скриптом на 10 000 угод {fs['capped']}, "
        f"без угод {fs['no_fills']}. Завантаження йшло вперед від початку, тож обрізано кінець історії: "
        f"остання завантажена угода обрізаних файлів, днів до {datetime.fromtimestamp(E0 / 1000, UTC):%Y-%m-%d}: "
        f"медіана {statistics.median(ed) if ed else float('nan'):.0f}, мін {ed[0] if ed else float('nan'):.0f}, макс {ed[-1] if ed else float('nan'):.0f}. "
        "Вікна після кінця покриття відкинуто (coverage_end). Акаунти відібрано за PnL того самого місяця."
    )
    print_periods("", fr)
    emit("## 6. Потужність H1 / H1w (sd між періодами з ретроспективи; survivorship може його занижувати)")
    emit()
    power_line("H1 IC (без lookahead)", ic["sd"], IC_TARGET, 30, n_sd=ic["n"])
    power_line("H1 IC (інтерполяція)", ic_i["sd"], IC_TARGET, 30, n_sd=ic_i["n"])
    power_line("H1 D (без lookahead)", d["sd"], D_TARGET, 30, pct=True, n_sd=d["n"])
    power_line("H1 D (інтерполяція)", d_i["sd"], D_TARGET, 30, pct=True, n_sd=d_i["n"])
    power_line("H1w IC", icw["sd"], ICW_TARGET, 7, n_sd=icw["n"])
    power_line("H1w D", dw["sd"], DW_TARGET, 7, pct=True, n_sd=dw["n"])
    emit()
    section_h2_h3(conn, snap, cls)
    emit(f"peak rss {rss_mb():.0f} MB")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", choices=("fetch", "report", "align"), required=True)
    ap.add_argument("--db", default=None, help="backup .sqlite or .sqlite.gz (default: newest in /var/lib/tradedesk-mcp/backups)")
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/smart_money")
    ap.add_argument("--limit", type=int, default=None, help="fetch only the first N accounts (timing sample)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.out:
        OUT["fh"] = open(Path(a.out).expanduser(), "w", encoding="utf-8")  # noqa: SIM115
    if a.stage == "align":
        asyncio.run(align_live())
        return
    conn = open_db(a.db, cache)
    if a.stage == "fetch":
        asyncio.run(fetch(conn, cache, a.limit))
    else:
        report(conn, cache)


if __name__ == "__main__":
    main()
