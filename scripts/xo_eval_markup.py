"""Build docs/calls/x/Trader_XO-eval.csv: the group / kind / flags markup of @Trader_XO's calls for the trader evaluation
(card IDEA-20261113-x-traders, item 3.2). Reads only the calls CSV and (optionally) the private tweet archive; no
market prices. Rules are regexes on each row's ``note`` (the type words the batch reports wrote there) plus the
explicit lists below, which come from the card's batch log and the quarter blocks of the calls CSV.

    uv run python scripts/xo_eval_markup.py [--archive docs/library/courses/x-Trader_XO/tweets.json] [--write]
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path

from tradedesk.analytics.calls import Call, parse_calls

ROOT = Path(__file__).resolve().parents[1]
CALLS = ROOT / "docs/calls/x/Trader_XO.csv"
OUT = ROOT / "docs/calls/x/Trader_XO-eval.csv"
ARCHIVE = ROOT / "docs/library/courses/x-Trader_XO/tweets.json"
PRIMARY_FROM = date(2025, 1, 1)

TRADFI = {"EURUSD", "GBPUSD", "USDJPY", "USDCAD", "ES", "NQ", "XAU", "XAG", "DXY", "OIL", "SPX"}
# rows whose own type is a hedge (the author names it, batch log and CSV notes); "possible hedge" stays in main
HEDGE = {
    "XO-2025-08-23-ETH-SHORT", "XO-2025-09-17-BTC-SHORT", "XO-2026-06-03-BTC-LONG", "XO-2026-09-30-SOL-SHORT",
    "XO-2024-11-12-ETH-SHORT", "XO-2024-11-19-BTC-SHORT", "XO-2024-11-25-BTC-SHORT", "XO-2024-12-05-BTC-SHORT",
    "XO-2024-07-30-SOL-SHORT", "XO-2024-04-15-BTC-SHORT", "XO-2024-04-15-SOL-SHORT", "XO-2024-04-20-BTC-SHORT",
    "XO-2023-03-22-BTC-LONG", "XO-2023-05-25-BTC-LONG", "XO-2022-03-13-LUNA-LONG", "XO-2022-03-20-LUNA-LONG",
    "XO-2022-07-31-BTC-LONG", "XO-2022-08-24-ES-LONG", "XO-2021-09-23-BTC-SHORT",
}  # fmt: skip
HEDGE_POSSIBLE = {"XO-2020-11-30-ETH-SHORT", "XO-2021-08-24-BTC-SHORT"}
# spot positions (the row's own type says spot / Uniswap / meme coin bag); evaluated on perp bars as a separate group
SPOT = {
    "XO-2025-05-19-SOL-LONG", "XO-2025-07-21-ENA-LONG", "XO-2025-07-22-ETH-LONG", "XO-2026-04-08-ETH-LONG",
    "XO-2024-10-07-SEI-LONG", "XO-2024-10-14-JUP-LONG", "XO-2024-10-14-JTO-LONG", "XO-2024-10-20-ETH-LONG",
    "XO-2024-10-29-ZIG-LONG", "XO-2024-11-20-BTC-LONG", "XO-2024-11-22-BERT-LONG", "XO-2024-11-22-KHAI-LONG",
    "XO-2024-08-05-ETH-LONG", "XO-2024-09-25-KHAI-LONG", "XO-2021-01-16-PHA-LONG", "XO-2021-04-15-KTN-LONG",
    "XO-2021-04-17-ETH-LONG", "XO-2021-04-17-ZCX-LONG", "XO-2021-04-28-EQZ-LONG", "XO-2021-05-23-ETH-LONG",
    "XO-2021-06-23-BTC-LONG", "XO-2022-03-03-SYS-LONG", "XO-2022-03-25-ACA-LONG", "XO-2022-05-27-SOL-LONG",
    "XO-2023-05-31-ARB-LONG", "XO-2023-08-18-PAAL-LONG", "XO-2023-11-09-TVK-LONG", "XO-2023-11-09-PYR-LONG",
    "XO-2024-02-16-ETH-LONG", "XO-2024-02-16-JUP-LONG", "XO-2024-02-29-SNX-LONG", "XO-2024-03-20-DYM-LONG",
    "XO-2024-04-20-ETH-LONG", "XO-2024-05-15-W-LONG", "XO-2024-05-20-INJ-LONG",
}  # fmt: skip
CANCELLED = {"XO-2021-04-15-BTC-LONG": "2021-04-18T04:23"}
# one idea in several rows (batch log and notes): the head is the row counted in the "one idea" variant
CHAIN = {
    "XO-2025-04-08-BTC-SHORT": "XO-2025-03-26-BTC-SHORT",  # Q1 26.03 88.4 short = Q2 04-08 (do not count twice)
    "XO-2025-05-30-SOL-SHORT": "XO-2025-05-19-SOL-SHORT",  # possibly the same position (2a amendment)
    "XO-2026-04-29-BTC-SHORT": "XO-2026-03-05-BTC-SHORT",  # swing short in four rows, item 3 counts it once
    "XO-2026-05-05-BTC-SHORT": "XO-2026-03-05-BTC-SHORT",
    "XO-2026-05-16-BTC-SHORT": "XO-2026-03-05-BTC-SHORT",
    "XO-2026-01-08-ZEC-SHORT": "XO-2025-12-31-ZEC-SHORT",  # pair Q4 2025 12-31 <-> 2026 01-08
    "XO-2026-07-06-BTC-SHORT": "XO-2026-06-21-BTC-SHORT",  # Q2 <-> Q3, same side
}

# recovered rows the two automatic rules miss (batch log: picture "Before" rows of 2021-Q1 known only from later tweets)
RECOVERED_EXTRA: set[str] = set()
DIAG: list[str] = []

KIND_PATTERNS = (
    ("disclosure", r"розкриття"),
    ("conditional", r"умовн|пробійн|бажання з числом"),
    ("setup", r"план із зоною|план\b|ліміт"),
)


def first_id(note: str) -> str | None:
    m = re.search(r"\d{18,20}", note)
    return m.group(0) if m else None


def kind_of(c: Call) -> str:
    """The first type word in the note decides (the batch reports write the type after the quote)."""
    best: tuple[int, str] | None = None
    for k, p in KIND_PATTERNS:
        m = re.search(p, c.note)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), k)
    return best[1] if best else "setup"


def markup(calls: list[Call], archive_ids: set[str] | None) -> list[dict]:
    rows = []
    for c in calls:
        n = c.note
        flags = []
        fid = first_id(n)
        # the row's own type word ("розкриття, відновлений", "відновлений (виключати за id)"), not a mention of another tweet
        rec_word = bool(re.search(r"(розкриття|умовний|спот|межовий)[^;«»]{0,40}відновлений|відновлений \(виключати", n))
        rec_arch = archive_ids is not None and fid is not None and fid not in archive_ids
        if rec_word or rec_arch or c.id in RECOVERED_EXTRA:
            flags.append("recovered")
        if rec_word != rec_arch:
            DIAG.append(f"{c.id[3:]} word={int(rec_word)} archive={int(rec_arch)}")
        if re.search(r"межов", n):
            flags.append("boundary")
        if re.search(r"в основному результаті[^;]{0,30}виключ|виключати за id", n):
            flags.append("excl_main")
        if re.search(r"не оцінюється автоматично|автоматично не оцінюється", n):
            flags.append("not_auto")
        if re.search(r"варіант[^;]{0,30}виключити|або виключити", n):
            flags.append("excl_variant")
        if re.search(r"показано (вже )?в плюсі|в плюсі на момент|на плюсі|ROI \+|P&L \+|прибутков", n):
            flags.append("in_profit")
        if re.search(r"пробійн", n):
            flags.append("breakout")
        if re.search(r"з графіка", n):
            flags.append("from_chart")
        if c.symbol.split(":")[-1].endswith("/BTC"):
            flags.append("btc_quoted")
        if c.id in HEDGE_POSSIBLE:
            flags.append("hedge_possible")
        if c.asset in TRADFI:
            group = "tradfi"
        elif c.id in SPOT:
            group = "spot"
        elif c.id in HEDGE:
            group = "hedge"
        elif c.id in CANCELLED:
            group = "cancelled"
        else:
            group = "main"
        rows.append(
            {
                "id": c.id,
                "sample": "primary" if c.day >= PRIMARY_FROM else "oos",
                "group": group,
                "kind": kind_of(c),
                "t0_utc": "",
                "cancel_utc": CANCELLED.get(c.id, ""),
                "author_exit": "",
                "author_exit_utc": "",
                "chain_head": CHAIN.get(c.id, ""),
                "flags": ";".join(flags),
            }
        )
    return rows


HEADER = """# Розмітка закликів @Trader_XO для оцінки (картка IDEA-20261113-x-traders, п. 3.2; реєстрація 18.11, до будь-якого P&L).
# Згенеровано scripts/xo_eval_markup.py з note рядків Trader_XO.csv (слова типу, які писали звіти партій) і явних списків
# у скрипті (хеджі, спот, скасовані, ланцюги «одна ідея» — з журналу партій картки). recovered = перший id у note відсутній
# в архіві tweets.json або note каже «відновлений». sample: primary (2025-Q1 … 2026-Q4) | oos (2020-Q3 … 2024-Q4).
# group: main | hedge | spot | tradfi | cancelled (пріоритет tradfi > spot > hedge > cancelled > main). kind: disclosure | setup | conditional.
# t0_utc порожній = час твіту за snowflake першого id у note.
"""
FIELDS = ("id", "sample", "group", "kind", "t0_utc", "cancel_utc", "author_exit", "author_exit_utc", "chain_head", "flags")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--archive", type=Path, default=ARCHIVE)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--list", choices=("primary", "oos"), help="print one line per row of this sample")
    a = ap.parse_args()
    calls, errors = parse_calls(CALLS.read_text(encoding="utf-8"))
    ids = {str(t["id"]) for t in json.loads(a.archive.read_text(encoding="utf-8"))} if a.archive.exists() else None
    rows = markup(calls, ids)
    known = {c.id for c in calls}
    unknown = sorted((HEDGE | HEDGE_POSSIBLE | SPOT | set(CANCELLED) | set(CHAIN) | set(CHAIN.values())) - known)
    if unknown:
        raise SystemExit(f"ids in the lists but not in {CALLS.name}: {unknown}")
    print(f"rows {len(rows)}, parser errors {len(errors)}, archive {'yes' if ids is not None else 'NO'}")
    for s in ("primary", "oos"):
        sel = [r for r in rows if r["sample"] == s]
        print(f"{s}: {len(sel)} groups {dict(Counter(r['group'] for r in sel))} kinds(main) "
              f"{dict(Counter(r['kind'] for r in sel if r['group'] == 'main'))}")  # fmt: skip
        print("   flags(main):", dict(Counter(f for r in sel if r["group"] == "main" for f in r["flags"].split(";") if f)))
    print("recovered: word rule vs archive rule differ:", "; ".join(DIAG) or "none")
    if a.list:
        for r in rows:
            if r["sample"] == a.list:
                print(f"{r['id'][3:]:24} {r['group']:6} {r['kind']:11} {r['chain_head'][3:]:22} {r['flags']}")
    if a.write:
        body = ",".join(FIELDS) + "\n" + "".join(",".join(r[f] for f in FIELDS) + "\n" for r in rows)
        OUT.write_text(HEADER + body, encoding="utf-8")
        print("written", OUT)


if __name__ == "__main__":
    main()
