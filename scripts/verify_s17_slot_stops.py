"""S17 signals stopped only by daily setup slots or the open-positions cap (IDEA-20261030-demo-setups-depth, audit 24.11).

Registration: the card's section «Попередня реєстрація — S17, сигнали, зупинені слотом чи лімітом позицій». Accounting
of how the demo's S17 sample differs from the passport's, not an edge test. Reads the demo DB read-only (``mode=ro``);
Bybit perp bars (1m for the first hour after a decision, 1h after it) only through the runtime providers
(``Runtime.exchanges``), cached per key under ``--cache-dir`` (complete fetches only).

    uv run python scripts/verify_s17_slot_stops.py --stage smoke
    uv run python scripts/verify_s17_slot_stops.py --stage run --out ~/.cache/tradedesk-agents/s17-slot-stops/run.txt

``smoke`` prints only data presence, schema, pairing / journal integrity, the 18.11 sent-limits check, one
price-level check, one 1m-to-1h splice check and the runtime; no class counts, touches, R or dates. ``run`` computes the registered counts (a)-(c).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import common_helpers as cmn  # noqa: E402
from demo_vs_backtest import to_bars, venue_fetch  # noqa: E402

from tradedesk.analytics import s17_slot_stops as ss  # noqa: E402
from tradedesk.analytics import sample_pace as pace  # noqa: E402
from tradedesk.analytics.course_s14_ema import Bars  # noqa: E402
from tradedesk.analytics.pending_slots import Interval, pending_interval  # noqa: E402
from tradedesk.analytics.setup_passport import mean_ci  # noqa: E402
from tradedesk.analytics.stop_management import Fees  # noqa: E402
from tradedesk.config import Settings  # noqa: E402
from tradedesk.core.envelope import Builder  # noqa: E402
from tradedesk.runtime import Runtime  # noqa: E402

DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
DEFAULT_CACHE = Path.home() / ".cache/tradedesk-agents/s17-slot-stops"
DAY = 86_400_000
HOUR = 3_600_000
MINUTE = 60_000
TF = {"1h": HOUR, "1m": MINUTE}


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp() * 1000)


W0 = ms("2026-10-31T00:00")  # registered window start
CUTOFF = ms("2026-11-25T00:00")  # registered decision cut-off (exclusive), fixed 25.11 before any outcome number
DATA_CUTOFF = ms("2026-11-26T00:00")  # registered cut-off of the 1h bars and the journal (fill / cancel rows) of the run
CAP25_MS = ms("2026-11-11T08:02")  # first «25 open positions = max» row (report 18.11, defect 1): split only
HORIZONS = (3, 7, 90)  # days, (b)
KM_AT = (1, 3, 7, 14, 21)
HOLD_MS = 20 * DAY  # demo_trader.SETUP_HOLD_MS["S17"] = S17_HOLD_BARS 1d bars
TARGET = 30
CHECK_DATE = "2027-03-31"
N_BOOT, SEED = 10_000, 20261030
STOPPED = ss.STOPPED
MIN_N = 10  # registered: fewer eligible keys (or resolved keys of a row) -> ◯
SENT_18_11 = frozenset({"FLUID", "LTC", "DOGE", "PARTI", "ZEC", "RENDER"})  # report 18.11: the 6 S17 limits sent
ZEC_PENDING_UNTIL = ms("2026-11-19T00:00")  # report 18.11: ZEC still pending; a fill before this would contradict it
CLOSE_RE = re.compile(r"закрилась на ([0-9.eE+-]+)")
LINES: list[str] = []


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


def load(db: str, until: int) -> dict[str, Any]:
    """S17 audit rows by link_id (ts < ``until``) and the S17 demo_trade rows; never R or P&L columns."""
    con = cmn.connect_ro(db)
    try:
        by: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        sym: dict[str, str] = {}
        for rid, ts, kind, lid, symbol, payload in con.execute(
            "SELECT id, ts, kind, link_id, symbol, payload FROM exec_audit WHERE setup = 'S17' AND link_id IS NOT NULL AND ts < ? "
            "AND kind IN ('signal','refused','skipped','sent','filled','missed','cancelled','expired') ORDER BY id",
            (until,),
        ):
            p = json.loads(payload or "{}") if kind in ("signal", "refused", "skipped") else {}
            by[lid][kind].append({"id": rid, "ts": int(ts), "link_id": lid, "payload": p})
            if symbol:
                sym.setdefault(lid, str(symbol))
        cols = (
            "link_id",
            "symbol",
            "side",
            "timeframe",
            "status",
            "signal_ts",
            "entry",
            "stop",
            "target",
            "opened_ts",
            "closed_ts",
            "rest_until",
        )
        trades = [dict(zip(cols, r, strict=True)) for r in con.execute(f"SELECT {', '.join(cols)} FROM demo_trade WHERE setup = 'S17'")]
        return {"audit": by, "trades": trades, "symbol": sym}
    finally:
        con.close()


def keys_in_window(audit: dict[str, Any]) -> list[str]:
    """link_ids whose first signal row is in [W0, CUTOFF)."""
    out = []
    for lid, k in audit.items():
        sig = k.get("signal") or []
        if sig and W0 <= min(r["ts"] for r in sig) < CUTOFF:
            out.append(lid)
    return sorted(out)


def intervals(d: dict[str, Any], until: int) -> dict[str, Interval]:
    """Unfilled interval of every S17 demo row: from the first sent row to the first ``filled`` audit row (defect 2 of
    18.11: ``opened_ts`` of a pending row is the send time), else the first missed / cancelled / expired row, else
    ``closed_ts`` of a never-filled row, else ``until``."""
    out: dict[str, Interval] = {}
    for t in d["trades"]:
        a = d["audit"].get(t["link_id"], {})
        sent = min((r["ts"] for r in a.get("sent", [])), default=None)
        if sent is None:
            continue
        fill = min((r["ts"] for r in a.get("filled", [])), default=None)
        gone = min((r["ts"] for k in ("missed", "cancelled", "expired") for r in a.get(k, [])), default=None)
        if fill is None and gone is None and t["status"] not in ("pending", "open", "unknown"):
            gone = ms_of(t["closed_ts"])
        iv = pending_interval(sent, None, fill, gone if fill is None else None, until)
        if iv is not None:
            out[t["link_id"]] = iv
    return out


def classify_all(d: dict[str, Any], lids: list[str]) -> dict[str, ss.KeyClass]:
    out = {}
    for lid in lids:
        a = d["audit"][lid]
        dec = [*a.get("refused", []), *a.get("skipped", [])]
        cands = ss.pair_first_cycle(a.get("signal", []), dec)
        out[lid] = ss.classify_key(lid, cands, bool(a.get("sent")))
    return out


def symbol_of(lid: str, d: dict[str, Any]) -> str:
    """The audit row's ccxt symbol (Bybit linear, e.g. 'ETH/USDT:USDT')."""
    return d["symbol"][lid]


async def fetch_bars(rt: Runtime, b: Builder, cache: Path, symbol: str, since: int, until: int, tf: str = "1h") -> Bars:
    """Bybit perp bars of ``tf`` (1h, or 1m for the first hour after a decision) opening in [since, until), only
    closed ones; one JSON file per (symbol, tf, since, until). The file is written only when the fetch added nothing to
    ``b.missing`` / ``b.warnings``, so a series cut by a failed later page is fetched again on the next run."""
    bar = TF[tf]
    sym = symbol.replace("/", "_").replace(":", "_")
    f = cache / "bars" / (f"{sym}_{since}_{until}.json" if tf == "1h" else f"{sym}_{tf}_{since}_{until}.json")
    if since >= until:
        return to_bars([], bar)
    if f.exists():
        rows = cmn.read_json(f)
    else:
        n_miss, n_warn = len(b.missing), len(b.warnings)
        if tf == "1h":
            got: Any = await venue_fetch(rt, b, "bybit")(symbol, "1h", since, until - 1)
        else:  # at most 60 bars: one request (Bybit returns up to 1000)
            got = await b.guard(f"ohlcv:bybit:{symbol}:{tf}", "ccxt:bybit", rt.exchanges.ohlcv("bybit", symbol, tf, 1000, since, until - 1))
        rows = [list(r[:5]) for r in (got or []) if isinstance(r, (list, tuple)) and len(r) >= 5 and r[0] is not None]
        if rows and len(b.missing) == n_miss and len(b.warnings) == n_warn:
            cmn.write_json(f, rows)
    return to_bars([r for r in rows if since <= int(r[0]) and int(r[0]) + bar <= until], bar)


def gaps(b: Bars) -> int:
    return sum(1 for i in range(1, len(b)) if int(b.t[i]) - int(b.t[i - 1]) != HOUR)


def signal_close(a: dict[str, Any]) -> float | None:
    for r in a.get("signal", []):
        m = CLOSE_RE.search(str(((r["payload"] or {}).get("signal") or {}).get("comment") or ""))
        if m:
            try:
                return float(m.group(1).rstrip("."))
            except ValueError:
                return None
    return None


def base_of(symbol: str) -> str:
    return symbol.split("/", 1)[0]


def sent_check(d: dict[str, Any]) -> str:
    """Registered integrity check against report 18.11: the S17 limits sent before the decision cut-off are FLUID, LTC,
    DOGE, PARTI, ZEC, RENDER, and ZEC was still pending then (no ``filled`` row before 19.11). No outcome number."""
    sent = {}
    for t in d["trades"]:
        a = d["audit"].get(t["link_id"], {})
        s = min((r["ts"] for r in a.get("sent", [])), default=None)
        if s is not None and s < CUTOFF:
            sent[base_of(str(t["symbol"]))] = (t, min((r["ts"] for r in a.get("filled", [])), default=None))
    zec = sent.get("ZEC")
    zec_ok = zec is not None and (zec[1] is None or zec[1] >= ZEC_PENDING_UNTIL)
    ok = set(sent) == SENT_18_11 and zec_ok
    zec_txt = "немає" if zec is None else f"статус зараз {zec[0]['status']}, перший filled {iso(zec[1])}"
    return (
        f"цілісність (18.11): відправлені S17 до cut-off {len(sent)}: {', '.join(sorted(sent))}; очікувано "
        f"{', '.join(sorted(SENT_18_11))}; ZEC: {zec_txt} (до 19.11 не заповнена: {'так' if zec_ok else 'ні'}); збіг: {'так' if ok else 'НІ'}"
    )


# ------------------------------------------------------------------------------------------------------------ smoke


async def smoke(a: argparse.Namespace, until: int) -> None:
    t0 = time.time()
    d = load(a.db, until)
    lids = keys_in_window(d["audit"])
    tfs = Counter(lid.split("-")[3] for lid in lids)
    n_sig = sum(len(d["audit"][lid].get("signal", [])) for lid in lids)
    emit(
        f"вікно {iso(W0)} … {iso(CUTOFF)} UTC (рішення), дані до {iso(until)}; link_id S17 з сигналом у вікні {len(lids)}; рядків signal {n_sig}; таймфрейми {dict(tfs)}"
    )
    cls = classify_all(d, lids)
    unmatched = sum(1 for k in cls.values() if k.cls == "unmatched")
    emit(f"зіставлення signal ↔ рішення: незіставлених {unmatched} з {len(lids)} (ключі з відправленим ордером не потребують зіставлення)")
    st = Counter(t["status"] for t in d["trades"])
    ivs = intervals(d, until)
    bad = sum(1 for iv in ivs.values() if iv.end < iv.start)
    mism = 0
    for t in d["trades"]:
        f = min((r["ts"] for r in d["audit"].get(t["link_id"], {}).get("filled", [])), default=None)
        if f is not None and ms_of(t["opened_ts"]) is not None and abs((ms_of(t["opened_ts"]) or 0) - f) > 60_000:
            mism += 1
    emit(
        f"demo_trade S17: {dict(sorted(st.items()))}; інтервалів очікування {len(ivs)}; кінець < початку {bad}; filled ≠ opened_ts (> 1 хв) {mism}"
    )
    with_slots_text = sum(
        1 for k in cls.values() if k.best is not None and "setup_slots" in k.cats and ss.slots_used(k.best.violations) is None
    )
    emit(f"рядків «слоти зайняті» без числа N у тексті: {with_slots_text}")
    emit(sent_check(d))
    # one price-level check against a known number: the breakout candle's close in the signal text vs Bybit 1h
    lid = next((x for x in lids if signal_close(d["audit"][x]) is not None), None)
    if lid is None:
        emit("перевірка рівня цін: немає сигналу з «закрилась на X»")
    else:
        sig_ts = int(lid.rsplit("-", 1)[1]) * 1000
        rt = Runtime(dataclasses.replace(Settings.from_env(), db_path=""))
        b = Builder()
        try:
            t1 = time.time()
            bars = await fetch_bars(rt, b, a.cache_dir, symbol_of(lid, d), sig_ts, sig_ts + DAY)
            dt = time.time() - t1
            t1 = time.time()
            fine = await fetch_bars(rt, b, a.cache_dir, symbol_of(lid, d), sig_ts + DAY - HOUR, sig_ts + DAY, "1m")
            dt1 = time.time() - t1
        finally:
            await rt.aclose()
        c_txt = signal_close(d["audit"][lid])
        c_bar = float(bars.c[-1]) if len(bars) else math.nan
        emit(
            f"перевірка рівня цін {symbol_of(lid, d)} свічка {iso(sig_ts)}: барів 1h {len(bars)} (дірок {gaps(bars)}), закриття Bybit "
            f"{c_bar:g} проти тексту сигналу {c_txt:g}, відношення {c_bar / c_txt:.4f}; запит {dt:.1f} с; missing {len(b.missing)}"
        )
        if len(fine) and len(bars):  # the 1m bars of the candle's last hour must rebuild its last 1h bar
            emit(
                f"склейка 1m ↔ 1h, остання година свічки: барів 1m {len(fine)}; max high 1m {float(fine.h.max()):g} проти 1h {float(bars.h[-1]):g}; "
                f"min low 1m {float(fine.low.min()):g} проти 1h {float(bars.low[-1]):g}; запит {dt1:.1f} с; missing {len(b.missing)}"
            )
        else:
            emit(f"склейка 1m ↔ 1h: барів 1m {len(fine)}, 1h {len(bars)}; missing {len(b.missing)}")
    emit(f"час {time.time() - t0:.1f} с; peak RSS {cmn.rss_mb():.0f} MB")


# -------------------------------------------------------------------------------------------------------------- run


def ci_line(name: str, v: list[float], days: list[int]) -> str:
    if not v:
        return f"| {name} | 0 | — | — |"
    r = mean_ci(v, days, n_boot=N_BOOT, seed=SEED)
    return f"| {name} | {len(v)} ({len(set(days))} дн.) | {r['m']:+.3f} | [{r['lo']:+.3f}; {r['hi']:+.3f}] |"


async def run(a: argparse.Namespace, until: int) -> None:
    t0 = time.time()
    d = load(a.db, until)
    lids = keys_in_window(d["audit"])
    cls = classify_all(d, lids)
    ivs = intervals(d, until)
    iv_list = list(ivs.values())
    emit(f"# S17: сигнали, зупинені слотами чи лімітом позицій — облік; рішення {iso(W0)} … {iso(CUTOFF)}, дані до {iso(until)} UTC")
    emit(f"ключів {len(lids)}; peak RSS {cmn.rss_mb():.0f} MB")

    # (a) classes, overall and by position-cap period
    emit("\n## (а) Класи ключів (монета, бік, свічка пробою)")
    emit("| клас | усе вікно | до 11.11 08:02 (ліміт 15) | після (ліміт 25) |")
    emit("|---|---|---|---|")

    def t_of(lid: str) -> int:
        k = cls[lid]
        if k.best is not None:
            return k.best.ts
        aa = d["audit"][lid]
        return min(r["ts"] for r in (aa.get("sent") or aa.get("signal")))

    order = ("placed", *STOPPED, "slots_limit_plus_other", "slots_unread", "other", "at_level", "ineligible", "unmatched")
    for c in order:
        ks = [x for x in lids if cls[x].cls == c]
        emit(f"| {c} | {len(ks)} | {sum(t_of(x) < CAP25_MS for x in ks)} | {sum(t_of(x) >= CAP25_MS for x in ks)} |")
    emit(
        "(придатні = placed + slots_only + limit_only + slots_and_limit + slots_limit_plus_other + slots_unread + other; "
        "at_level — «ціна вже на рівні, post-only взяв би»: паспорт поставив би, демо ні; у придатні не входить, як і зареєстровано)"
    )
    stopped = [x for x in lids if cls[x].cls in STOPPED]
    placed = [x for x in lids if cls[x].cls == "placed"]
    n_unm = sum(cls[x].cls == "unmatched" for x in lids)
    emit("\nцілісність прогону:")
    emit(f"- незіставлених {n_unm} з {len(lids)} ключів ({n_unm / max(len(lids), 1):.1%}; поріг ◯ > 1 %)")
    emit(f"- «слоти зайняті» без числа N у тексті (клас slots_unread, у (а) не входить): {sum(cls[x].cls == 'slots_unread' for x in lids)}")
    emit(f"- {sent_check(d)}")
    for name, group in (("(а)", stopped), ("slots_limit_plus_other", [x for x in lids if cls[x].cls == "slots_limit_plus_other"])):
        with_slots = [x for x in group if "setup_slots" in cls[x].cats]
        prev = [x for x in with_slots if ss.prev_day_pending(iv_list, cls[x].best.ts) >= 1]  # type: ignore[union-attr]
        emit(
            f"{name}: ключів {len(group)}, з категорією «слоти» {len(with_slots)}, з них слот тримала лімітка попереднього дня: {len(prev)}"
        )
    rel = ss.released_keys([cls[x] for x in lids], iv_list)
    emit(f"допущені б без `pending` попередніх днів (один крок без каскаду; не межа ні зверху, ні знизу): {len(rel)}")

    emit("\n## (а) Частки серед придатних ключів [95 % CI, бутстреп кластерами за днем рішення]")
    sv = ss.share_vectors([(cls[x].cls, t_of(x) // DAY) for x in lids])
    n_el = len(sv["days"])
    emit(f"придатних ключів n {n_el} (поріг ◯ < {MIN_N}) за {len(set(sv['days']))} дн.")
    for key, label in (
        ("s", "s = (а) / придатні (головна)"),
        ("s_prime", "s′ = ((а) + slots_limit_plus_other) / придатні (чутливість)"),
        ("placed", "частка розміщених = placed / придатні (описово)"),
    ):
        if n_el:
            r = mean_ci(sv[key], sv["days"], n_boot=N_BOOT, seed=SEED)  # type: ignore[arg-type]
            emit(f"- {label}: {r['m']:.3f} [{r['lo']:.3f}; {r['hi']:.3f}]")
    for name, pick in (("до 11.11 08:02 (ліміт 15)", lambda x: t_of(x) < CAP25_MS), ("після (ліміт 25)", lambda x: t_of(x) >= CAP25_MS)):
        sp = ss.share_vectors([(cls[x].cls, t_of(x) // DAY) for x in lids if pick(x)])
        if sp["days"]:
            r = mean_ci(sp["s"], sp["days"], n_boot=N_BOOT, seed=SEED)  # type: ignore[arg-type]
            emit(f"- s {name}, описово: {r['m']:.3f} [{r['lo']:.3f}; {r['hi']:.3f}], n {len(sp['days'])}")
        else:
            emit(f"- s {name}: придатних 0")
    emit(
        f"◯-умови (пріоритет над ✓ / ≈ / ✗): придатних < {MIN_N}: {'так' if n_el < MIN_N else 'ні'}; незіставлених > 1 %: {'так' if n_unm > 0.01 * len(lids) else 'ні'}"
    )

    # (b) touches and shadow R
    rt = Runtime(dataclasses.replace(Settings.from_env(), db_path=""))
    b = Builder()
    rows: list[dict[str, Any]] = []
    try:
        for i, lid in enumerate([*stopped, *placed]):
            k = cls[lid]
            aa = d["audit"][lid]
            if k.best is not None:
                c = k.best
                t_dec, side, e, st, tg, rest = c.ts, c.side, c.entry, c.stop, c.target, c.rest_until
            else:  # placed: the levels of the demo_trade row (the sibling the demo sent, rounded to the tick)
                tr = next((t for t in d["trades"] if t["link_id"] == lid), None)
                if tr is None or tr["entry"] is None:
                    emit(f"  розміщений {lid} без рядка demo_trade: пропущено")
                    continue
                t_dec = min(r["ts"] for r in aa["sent"])
                side, e, st, tg = str(tr["side"]), float(tr["entry"]), float(tr["stop"]), float(tr["target"])
                rest = ms_of(tr["rest_until"]) or (t_dec + 90 * DAY)
            t_start = -(-t_dec // HOUR) * HOUR  # first full 1h bar after the decision
            t_min = -(-t_dec // MINUTE) * MINUTE  # first full 1m bar after the decision
            bars = await fetch_bars(rt, b, a.cache_dir, symbol_of(lid, d), t_start, until)
            fine = await fetch_bars(rt, b, a.cache_dir, symbol_of(lid, d), t_min, t_start, "1m")
            # main (registered after review B): 1m bars up to the first full hour, 1h after it; sensitivity: 1h only
            touch = ss.first_touch_glued(fine, bars, side, e, t_min, t_start, until)
            through = ss.first_touch_glued(fine, bars, side, e, t_min, t_start, until, through=True)
            touch_h = ss.first_touch(bars, side, e, t_start, until)
            through_h = ss.first_touch(bars, side, e, t_start, until, through=True)
            row = {
                "lid": lid,
                "grp": "stopped" if lid in stopped else "placed",
                "cls": k.cls,
                "t": t_dec,
                "day": t_dec // DAY,
                "rel": lid in rel,
                "touch_ms": touch,
                "fine_bars": len(fine),
                "fine_expected": (t_start - t_min) // MINUTE,
            }
            for h in HORIZONS:  # horizons run from the decision itself
                row[f"touch{h}"] = ss.touch_status(touch, t_dec, h * DAY, until)
                row[f"touch{h}_1h"] = ss.touch_status(touch_h, t_dec, h * DAY, until)
            row["age_touch"] = None if touch is None else (touch - t_dec) / DAY
            row["follow"] = (until - t_dec) / DAY
            row["fill_before_cutoff"] = through is not None and through < CUTOFF
            row["fill_before_cutoff_1h"] = through_h is not None and through_h < CUTOFF
            for name, rule, fees, thr in (
                ("B", ss.RULE_B, Fees(), True),
                ("A", ss.RULE_A, Fees(), True),
                ("B15", ss.RULE_B, Fees().scaled(1.5), True),
                ("Btouch", ss.RULE_B, Fees(), False),
            ):
                row[name] = ss.shadow_trade(bars, side, e, st, tg, t_start, rest, until, HOLD_MS, rule, fees, thr)
            row["bars"], row["gaps"] = len(bars), gaps(bars)
            if lid in placed:
                f = min((r["ts"] for r in aa.get("filled", [])), default=None)
                row["demo_fill"] = f
            rows.append(row)
            if (i + 1) % 10 == 0:
                emit(f"  … {i + 1} ключів, peak RSS {cmn.rss_mb():.0f} MB, {time.time() - t0:.0f} с")
    finally:
        await rt.aclose()
    cmn.write_json(a.cache_dir / "rows.json", rows)
    emit(
        f"missing провайдера: {len(b.missing)}; попереджень {len(b.warnings)}; ключів без 1h-барів {sum(r['bars'] == 0 for r in rows)}; "
        f"з дірками 1h {sum(r['gaps'] > 0 for r in rows)}; з неповною першою годиною 1m {sum(r['fine_bars'] < r['fine_expected'] for r in rows)}"
    )

    emit("\n## (б) Дотик рівня (low ≤ рівень для лонга / high ≥ для шорта), від рішення: 1m до першої повної години, далі 1h")
    emit(
        "| група | горизонт | торкнулись (горизонт скінчився) | не торкнулись | частка [CI кластери за днем] | "
        "вже торкнулись, горизонт не скінчився (описово) | ще ні, горизонт не скінчився |"
    )
    emit("|---|---|---|---|---|---|---|")

    def share_cell(gr: list[dict[str, Any]], col: str, h: int) -> str:
        if h == 90:
            return "◯ (частку не оцінюю: горизонт не скінчився в жодного ключа)"
        done = [r for r in gr if r[col] in ("touched", "not")]
        k, n = ss.touch_share([r[col] for r in gr])
        if n < MIN_N:
            return f"◯ ({k} з {n}, вирішених < {MIN_N})"
        m = mean_ci([1.0 if r[col] == "touched" else 0.0 for r in done], [r["day"] for r in done], n_boot=N_BOOT, seed=SEED)
        return f"{m['m']:.2f} [{m['lo']:.2f}; {m['hi']:.2f}] (n {n})"

    for g in ("stopped", "placed"):
        gr = [r for r in rows if r["grp"] == g]
        for h in HORIZONS:
            st_ = Counter(r[f"touch{h}"] for r in gr)
            emit(
                f"| {g} | {h} д | {st_['touched']} | {st_['not']} | {share_cell(gr, f'touch{h}', h)} | {st_['early_touch']} | {st_['censored']} |"
            )
        st3 = Counter(r["touch3_1h"] for r in gr)
        emit(
            f"| {g} | 3 д, чутливість: лише 1h від першої повної години | {st3['touched']} | {st3['not']} | {share_cell(gr, 'touch3_1h', 3)} | "
            f"{st3['early_touch']} | {st3['censored']} |"
        )
        km = ss.km_touch([r["age_touch"] for r in gr], [r["follow"] for r in gr], KM_AT)
        emit(
            f"| {g} | KM: "
            + ", ".join(f"{t} д {v:.2f}" for t, v in zip(KM_AT, km, strict=True))
            + f" | макс. спостереження {max((r['follow'] for r in gr), default=0):.1f} д | | | |"
        )

    emit(
        "\n## (б) Тіньовий R (паспорт: лімітка через рівень, B = беззбиток на півдорозі, комісії F1, утримання 20 діб; "
        "лише 1h від першої повної години після рішення)"
    )
    emit("| група / варіант | n | E[R] net | 95 % CI (кластери за днем) |")
    emit("|---|---|---|---|")
    for g in ("stopped", "placed"):
        gr = [r for r in rows if r["grp"] == g]
        st_ = Counter(r["B"]["status"] for r in gr)
        emit(f"| {g}: статуси B | {dict(st_)} | | |")
        for v in ("B", "A", "B15", "Btouch"):
            res = [r for r in gr if r[v]["status"] == "resolved"]
            emit(ci_line(f"{g} {v}, закриті", [r[v]["r"] for r in res], [r["day"] for r in res]))
        mtm = [r for r in gr if r["B"]["status"] in ("resolved", "open")]
        emit(ci_line(f"{g} B, закриті + відкриті за ринком (чутливість)", [r["B"]["r_mtm"] for r in mtm], [r["day"] for r in mtm]))
        n_res = sum(r["B"]["status"] == "resolved" for r in gr)
        if n_res < MIN_N:
            emit(f"| {g}: закритих B {n_res} < {MIN_N} → ◯ | | | |")
    # the row «per placement (unfilled = 0)» is not printed: rest_until is 90 days, so no key is not_filled by the
    # cut-off and the row would repeat «closed» (review B, finding 11)
    pl = [r for r in rows if r["grp"] == "placed" and r.get("demo_fill") is not None]
    agree = sum(1 for r in pl if r["touch_ms"] is not None and abs(r["touch_ms"] - r["demo_fill"]) <= HOUR)
    emit(
        f"\nцілісність: перший дотик моделі (1m у першу годину, далі 1h) у межах ±1 год від `filled` демо: {agree} з {len(pl)} розміщених і заповнених"
    )

    # (c) 30th filled trade
    emit("\n## (в) Дата 30-ї заповненої угоди S17 (`sample_pace`, точковий темп; від cut-off)")
    days = (CUTOFF - W0) / DAY
    have = sum(1 for iv in iv_list if iv.filled and W0 <= iv.start < CUTOFF and iv.end < CUTOFF)
    extra = sum(1 for r in rows if r["rel"] and r["fill_before_cutoff"])
    extra_h = sum(1 for r in rows if r["rel"] and r["fill_before_cutoff_1h"])
    today = datetime.fromtimestamp(CUTOFF / 1000, UTC)
    emit(f"f (допущені, рівень пробито до cut-off): {extra} (головний, 1m у першу годину); чутливість лише 1h: {extra_h}")
    emit("| правило | заповнено до cut-off | темп / тиждень [точний 95 % CI] | днів до 30 | дата 30 | P(30) до 31.03.2027 |")
    emit("|---|---|---|---|---|---|")
    dates = {}
    for name, k in (("нинішнє", have), ("без `pending` попередніх днів у слотах (k + f)", have + extra)):
        lam = k / days
        lo, hi = pace.poisson_ci(k)
        dd = pace.days_to_reach(TARGET, k, lam)
        date = None if math.isinf(dd) else today + timedelta(days=dd)
        dates[name] = date
        p = pace.prob_reach(TARGET - k, lam, (datetime.fromisoformat(CHECK_DATE).replace(tzinfo=UTC) - today).total_seconds() / 86400)
        emit(
            f"| {name} | {k} | {lam * 7:.2f} [{lo / days * 7:.2f}; {hi / days * 7:.2f}] | {dd:.0f} | {'ніколи' if date is None else f'{date:%Y-%m-%d}'} | {p:.3f} |"
        )
    a_, b_ = dates.values()
    if a_ is not None and b_ is not None:
        emit(f"Δ дати = {(a_ - b_).days} діб (тригер питання ≥ 14)")
    elif a_ is None and b_ is not None:
        emit("Δ дати: нинішнє правило «ніколи», контрфакт — дата (тригер питання спрацьовує)")
    emit(f"\nчас {time.time() - t0:.0f} с; peak RSS {cmn.rss_mb():.0f} MB")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("smoke", "run"), required=True)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--out", type=Path, help="write the printed lines to this file")
    ap.add_argument("--now-ms", type=int, help="data cut-off for bars and journal, unix ms (default: now, floored to the hour)")
    a = ap.parse_args()
    a.cache_dir = a.cache_dir.expanduser()
    (a.cache_dir / "bars").mkdir(parents=True, exist_ok=True)
    until = a.now_ms if a.now_ms is not None else int(time.time() * 1000) // HOUR * HOUR
    if until < CUTOFF:
        print(f"data cut-off {iso(until)} is before the decision cut-off {iso(CUTOFF)}", file=sys.stderr)
        return 2
    if a.stage == "run" and (a.now_ms != DATA_CUTOFF or until > int(time.time() * 1000)):
        print(f"run: pass --now-ms {DATA_CUTOFF} (registered data cut-off {iso(DATA_CUTOFF)}), not before that time", file=sys.stderr)
        return 2
    asyncio.run(smoke(a, until) if a.stage == "smoke" else run(a, until))
    if a.out:
        out = a.out.expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
