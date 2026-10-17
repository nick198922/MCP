"""Weekly smart-money board (IDEA-20261015-smart-money-persistence): Hyperliquid and Polymarket top traders, their
new and closed positions and the net positioning of the strong accounts. Writes a Markdown report.

    uv run python scripts/traders_report.py [--db PATH] [--out docs/traders/REPORT.md] [--days 7]

Reads the collector tables (`trader_snapshot`, `trader_positions`) from a backup copy of the database: ``--db`` is a
``.sqlite`` or ``.sqlite.gz`` (default: the newest backup in /var/lib/tradedesk-mcp/backups/, decompressed into
~/.cache/tradedesk-agents/traders/). No network, no keys. This is a board, not a signal: persistence (H1) and the
lead of new positions (H2, H3) are still untested. Rules: src/tradedesk/analytics/smart_money.py.
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tradedesk.analytics.smart_money import (  # noqa: E402
    EXCLUDED_CLASSES,
    classify_account,
    detect_delta_start,
    net_book,
    net_flow,
    position_changes,
    replay_positions,
    weekly_points,
)

BACKUPS = Path("/var/lib/tradedesk-mcp/backups")
MIN_VALUE = 500_000.0
KIND = {"opened": "відкрито", "closed": "закрито", "increased": "збільшено", "reduced": "зменшено", "flipped": "розворот"}
CLASS = {
    "system": "системний",
    "mm": "маркет-мейкер",
    "hft_directional": "HFT напрямний",
    "hedger": "хеджер?",
    "directional": "напрямний",
    "idle": "без позицій",
    "unknown": "—",
}


def open_db(db: str | None, cache: Path) -> tuple[sqlite3.Connection, str]:
    src = Path(db).expanduser() if db else max(BACKUPS.glob("*.sqlite.gz"), key=lambda p: p.name)
    if src.suffix == ".gz":
        cache.mkdir(parents=True, exist_ok=True)
        dst = cache / src.name[: -len(".gz")]
        if not dst.exists():
            tmp = dst.with_suffix(".part")
            with gzip.open(src) as f, open(tmp, "wb") as g:
                shutil.copyfileobj(f, g, 1 << 20)
            tmp.rename(dst)
        return sqlite3.connect(f"file:{dst}?mode=ro", uri=True), src.name
    return sqlite3.connect(f"file:{src}?mode=ro", uri=True), src.name


def usd(x: float | None) -> str:
    if x is None:
        return "—"
    a = abs(x)
    s = f"{a / 1e6:,.2f} млн" if a >= 1e6 else f"{a / 1e3:,.0f} тис." if a >= 1e3 else f"{a:,.0f}"
    return ("−" if x < 0 else "") + "$" + s.replace(",", " ")


def qty(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") if abs(x) >= 1000 else f"{x:.4g}"


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.1f} %"


def text(x: Any, n: int = 60) -> str:
    """Untrusted market titles and names: one line, no table breaks, truncated."""
    s = " ".join(str(x or "").split()).replace("|", "/").replace("<", "‹").replace(">", "›").replace("`", "'")
    s = s.replace("\\", "/").replace("[", "\\[").replace("]", "\\]")  # no markdown links / images from titles
    return s if len(s) <= n else s[: n - 1] + "…"


def short(a: str) -> str:
    return f"`{a[:6]}…{a[-4:]}`"


def snapshots(conn: sqlite3.Connection, venue: str) -> tuple[str | None, dict[str, dict[str, Any]], dict[str, dict[str, float | None]]]:
    """The latest day's rows by account and every day's pnl_week by account."""
    day = conn.execute("SELECT MAX(day) FROM trader_snapshot WHERE venue = ?", (venue,)).fetchone()[0]
    if day is None:
        return None, {}, {}
    cur = conn.execute(
        "SELECT account, name, account_value, pnl_day, pnl_week, pnl_month, pnl_all, roi_month, vlm_month FROM trader_snapshot "
        "WHERE venue = ? AND day = ?",
        (venue, day),
    )
    cols = [d[0] for d in cur.description]
    latest = {r[0]: dict(zip(cols, r, strict=True)) for r in cur.fetchall()}
    weeks: dict[str, dict[str, float | None]] = {}
    for acc, d, w in conn.execute("SELECT account, day, pnl_week FROM trader_snapshot WHERE venue = ?", (venue,)):
        weeks.setdefault(acc, {})[d] = w
    return day, latest, weeks


def positions_log(conn: sqlite3.Connection, venue: str) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT ts, account, market, side, size, value_usd, entry_px, upnl, leverage, title FROM trader_positions WHERE venue = ? ORDER BY ts",
        (venue,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def week_share(weeks: dict[str, float | None], day: str) -> str:
    pts = weekly_points(weeks, day)
    return "—" if not pts else f"{sum(1 for p in pts if p > 0)}/{len(pts)}"


def pm_change_kept(c: dict[str, Any], after: dict[str, dict[str, dict[str, Any]]]) -> bool:
    """Polymarket rows worth showing: opens, closes and increases by >= 10 %; a close of a position smaller than the
    wallet's smallest current one, when the wallet is at the collector's 100-position cap, is a drop out of the list."""
    if c["kind"] == "increased":
        return c["size_after"] >= 1.1 * c["size_before"]
    if c["kind"] == "closed":
        cur = after.get(c["account"], {})
        if len(cur) >= 100 and c["value_usd"] <= min(float(p.get("value_usd") or 0.0) for p in cur.values()):
            return False
        return True
    return c["kind"] in ("opened", "flipped")


def render(conn: sqlite3.Connection, source: str, days: int, now: datetime) -> str:
    runs = [r[0] for r in conn.execute("SELECT DISTINCT ts FROM trader_positions ORDER BY ts")]
    closed = [r[0] for r in conn.execute("SELECT DISTINCT ts FROM trader_positions WHERE side = 'closed'")]
    delta = detect_delta_start(runs, closed)
    out = [
        "# Розумні гроші: щотижневий звіт",
        "",
        f"Звіт на {now:%Y-%m-%d %H:%M} UTC (`scripts/traders_report.py`, база `{source}`). Картка: "
        "[IDEA-20261015-smart-money-persistence](../ideas/IDEA-20261015-smart-money-persistence.md).",
        "",
        "Як читати:",
        "- Це **дошка, а не сигнали**: чи лишаються сильні трейдери сильними (H1) і чи їхні нові позиції випереджають ціну (H2, H3), ще не перевірено.",
        "- Класи акаунтів — евристика з обігу (обсяг за 30 днів / вартість) і позицій головного акаунта; суб-акаунтів, вольтів, споту й ринків HIP-3 не видно.",
        "- Позиції для класів тут — з журналу колектора (лише відстежувані акаунти; «—» — позицій не спостерігали), тому розподіл класів відрізняється від звіту стадії 0, де позиції взято з `clearinghouseState` усіх акаунтів.",
        "- «Сильні» на Hyperliquid — відстежувані акаунти з топ-квінтилю за PnL 30 днів серед акаунтів ≥ $500 тис. без маркет-мейкерів і системних; на Polymarket — відстежувані гаманці з топ-50 за PnL 30 днів.",
        "- Назви ринків і акаунтів — недовірений текст із зовнішніх API.",
        "- ⚠ біля ROI > 1000 %: швидше за все, артефакт лідерборду (переказ чи депозит, врахований як PnL), а не угоди.",
        "",
    ]
    if not runs:
        out.append("Даних позицій ще немає.")
        return "\n".join(out) + "\n"
    first_ts, last_ts = runs[0], runs[-1]
    since_ts = max(first_ts, last_ts - days * 86_400)
    base_ts = max([t for t in runs if t <= since_ts] or [first_ts])
    span_h = (last_ts - base_ts) / 3600
    period = f"{datetime.fromtimestamp(base_ts, UTC):%Y-%m-%d %H:%M} → {datetime.fromtimestamp(last_ts, UTC):%Y-%m-%d %H:%M} UTC"
    if span_h < days * 24 - 2:
        period += f" (лише {span_h:.0f} год: колектор пише з 2026-10-16)"

    # ---------------- Hyperliquid
    day, snap, weeks = snapshots(conn, "hyperliquid")
    hl = positions_log(conn, "hyperliquid")
    s_base, s_last = replay_positions(hl, delta, base_ts), replay_positions(hl, delta, last_ts)
    cls = {}
    for a, r in snap.items():
        cls[a] = classify_account(r["account_value"], r["vlm_month"], list(s_last[a].values()) if a in s_last else None)
    univ = [a for a in snap if cls[a] not in EXCLUDED_CLASSES and (snap[a]["account_value"] or 0) >= MIN_VALUE]
    top_q = set(sorted(univ, key=lambda a: -(snap[a]["pnl_month"] or 0))[: len(univ) // 5])
    strong = sorted(a for a in s_last if a in top_q)
    out += [
        "## Hyperliquid",
        "",
        f"Знімок лідерборду {day}: {len(snap)} акаунтів ≥ $500 тис.; позиції відстежуваних ({len(s_last)}) — останній прогін "
        f"{datetime.fromtimestamp(last_ts, UTC):%Y-%m-%d %H:%M} UTC; «сильних» з них: {len(strong)}.",
        "",
        "### Топ-30 за PnL 30 днів (без системних)",
        "",
        "| # | Акаунт | Назва | Клас | Вартість | PnL 1 д | PnL 7 д | PnL 30 д | ROI 30 д | PnL весь час | Обіг 30 д / вартість | Плюсових тижнів |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    ranked = sorted((a for a in snap if cls[a] != "system"), key=lambda a: -(snap[a]["pnl_month"] or float("-inf")))
    for i, a in enumerate(ranked[:30], 1):
        r = snap[a]
        to = (r["vlm_month"] or 0) / r["account_value"] if r["account_value"] else None
        out.append(
            f"| {i} | {short(a)} | {text(r['name'], 24) or '—'} | {CLASS[cls[a]]} | {usd(r['account_value'])} | {usd(r['pnl_day'])} "
            f"| {usd(r['pnl_week'])} | {usd(r['pnl_month'])} | {pct(r['roi_month'])}{' ⚠' if abs(r['roi_month'] or 0) > 10 else ''} | {usd(r['pnl_all'])} "
            f"| {'—' if to is None else f'{to:.1f}'} | {week_share(weeks.get(a, {}), day)} |"
        )
    counts: dict[str, int] = {}
    for a in ranked[:100]:
        counts[CLASS[cls[a]]] = counts.get(CLASS[cls[a]], 0) + 1
    out += [
        "",
        "Класи в топ-100 за PnL 30 днів: " + ", ".join(f"{k} — {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])) + ".",
        "",
    ]

    changes = position_changes(s_base, s_last, sorted(s_last))
    changes.sort(key=lambda c: -c["delta_usd"])
    out += [f"### Нові й закриті позиції ({period})", "", f"Змін: {len(changes)}; нижче 30 найбільших за зміною номіналу.", ""]
    out += [
        "| Акаунт | Клас | Сильний | Монета | Що | Сторона | Розмір до → після | Зміна | Номінал |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in changes[:30]:
        a = c["account"]
        out.append(
            f"| {short(a)} | {CLASS[cls.get(a, 'unknown')]} | {'так' if a in top_q else ''} | {text(c['market'], 16)} | {KIND[c['kind']]} "
            f"| {c['side']} | {qty(c['size_before'])} → {qty(c['size_after'])} | {usd(c['delta_usd'])} | {usd(c['value_usd'])} |"
        )
    book = net_book(s_last, strong)
    flow = net_flow(s_base, s_last, strong)
    out += [
        "",
        "### Чиста позиція сильних акаунтів по монетах",
        "",
        f"Номінал за останнім прогоном; «потік» — зміна знакового розміру за період, оцінена за останньою ціною ({period}). 25 монет з найбільшою |чистою| позицією.",
        "",
        "| Монета | Чиста | Лонги | Шорти | Тримачів | Потік за період |",
        "|---|---|---|---|---|---|",
    ]
    for m, r in sorted(book.items(), key=lambda kv: -abs(kv[1]["net"]))[:25]:
        out.append(f"| {text(m, 16)} | {usd(r['net'])} | {usd(r['long'])} | {usd(r['short'])} | {r['holders']} | {usd(flow.get(m))} |")
    out.append("")

    # ---------------- Polymarket
    pm = positions_log(conn, "polymarket")
    p_base, p_last = replay_positions(pm, delta, base_ts), replay_positions(pm, delta, last_ts)
    for venue, label in (("polymarket", "загальний рейтинг"), ("polymarket_crypto", "крипто-рейтинг")):
        pday, psnap, pweeks = snapshots(conn, venue)
        if not psnap:
            continue
        pr = sorted(psnap, key=lambda a: -(psnap[a]["pnl_month"] or float("-inf")))
        strong_pm = [a for a in pr[:50] if a in p_last]
        out += [
            f"## Polymarket — {label}",
            "",
            f"Знімок {pday}: {len(psnap)} гаманців; з топ-50 за PnL 30 днів позиції відстежуються у {len(strong_pm)}.",
            "",
            "### Топ-20 за PnL 30 днів",
            "",
            "| # | Гаманець | Назва | PnL 1 д | PnL 7 д | PnL 30 д | PnL весь час | Обсяг 30 д | Плюсових тижнів |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for i, a in enumerate(pr[:20], 1):
            r = psnap[a]
            out.append(
                f"| {i} | {short(a)} | {text(r['name'], 24) or '—'} | {usd(r['pnl_day'])} | {usd(r['pnl_week'])} | {usd(r['pnl_month'])} "
                f"| {usd(r['pnl_all'])} | {usd(r['vlm_month'])} | {week_share(pweeks.get(a, {}), pday)} |"
            )
        ch = [c for c in position_changes(p_base, p_last, strong_pm) if pm_change_kept(c, p_last)]
        ch.sort(key=lambda c: -c["delta_usd"])
        out += [
            "",
            f"### Нові, збільшені й закриті позиції топ-50 ({period})",
            "",
            f"Змін: {len(ch)} (відкриття, закриття і збільшення ≥ 10 %); 25 найбільших за зміною вартості. Закриття включає вирішені ринки; "
            "позиції, що лише випали зі 100 найбільших позицій гаманця, відкинуто.",
            "",
            "| Гаманець | Ринок | Ісход | Що | Акцій до → після | Зміна | Вартість |",
            "|---|---|---|---|---|---|---|",
        ]
        for c in ch[:25]:
            out.append(
                f"| {short(c['account'])} | {text(c['title'])} | {text(c['side'], 20)} | {KIND[c['kind']]} "
                f"| {qty(c['size_before'])} → {qty(c['size_after'])} | {usd(c['delta_usd'])} | {usd(c['value_usd'])} |"
            )
        agg: dict[tuple[str, str], dict[str, Any]] = {}
        for a in strong_pm:
            for p in p_last[a].values():
                k = (text(p.get("title")), text(p.get("side"), 20))
                row = agg.setdefault(k, {"wallets": 0, "usd": 0.0})
                row["wallets"] += 1
                row["usd"] += float(p.get("value_usd") or 0.0)
        out += [
            "",
            "### Де стоять гаманці топ-50 (за поточною вартістю позицій)",
            "",
            "| Ринок | Ісход | Гаманців | Вартість |",
            "|---|---|---|---|",
        ]
        for (title, side), r in sorted(agg.items(), key=lambda kv: (-kv[1]["wallets"], -kv[1]["usd"]))[:25]:
            out.append(f"| {title} | {side} | {r['wallets']} | {usd(r['usd'])} |")
        out.append("")
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None, help="database copy: .sqlite or .sqlite.gz (default: newest backup)")
    ap.add_argument("--out", type=Path, default=Path("docs/traders/REPORT.md"))
    ap.add_argument("--days", type=int, default=7, help="window for new / closed positions")
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/traders")
    a = ap.parse_args()
    conn, source = open_db(a.db, Path(a.cache_dir).expanduser())
    report = render(conn, source, a.days, datetime.now(UTC))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(report, encoding="utf-8")
    print(f"{len(report.splitlines())} lines -> {a.out}")


if __name__ == "__main__":
    main()
