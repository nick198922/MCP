"""Paper copy of two Hyperliquid vaults at Bybit prices (the owner's choice «А», 2026-10-24).

    python3 scripts/copy_vaults.py run              # every 2 minutes (user timer): follow, trade, mark, funding
    python3 scripts/copy_vaults.py report [--tg]    # the copy against the vault since the start; --tg sends it
    python3 scripts/copy_vaults.py status           # the books now (no network, no writes)

Question: can a follower earn what Delta_01 and Jade Lotus Capital earn (docs/verification/2026-10-23-top-vaults-
profile.md; card IDEA-20261015-smart-money-persistence, «Волти»)? Each vault gets a $10 000 book that mirrors its
positions scaled to the book: target size of a coin = the vault's size × (book equity / vault account value), read
from the public ``clearinghouseState``. A difference above the band (``BAND_USD`` and ``BAND_FRAC`` of the equity; a
closed position always) is traded at once as a taker at Bybit's best bid / ask with Bybit's taker fee (the same
TAKER_FEE as the demo trader's guard); Bybit's settled funding is charged on the held size. So the gap between the copy
and the vault's own return (``vaultDetails``) is what the follower loses to the delay (up to the timer's 2 minutes plus
the API), the venue's prices, fees and funding. The vault's withdrawals and deposits change its account value and its
sizes alike, so they cancel out in the target.

Paper only, no orders and no keys (nothing here can trade): the demo account cannot hold it, because Bybit nets one
position per symbol and the copy's BTC / ETH would mix with the course setups' trades there. Margin and liquidation
are not modelled (the vaults hold 1.7-2.2x gross). Vault data is untrusted: sizes are parsed as numbers, names never
reach Telegram. A failed API call skips the run (logged), never half-trades a book. Kept in DB (SQLite, own file).
Scheduled by deploy/user-units/tradedesk-copy-vaults-*; silent without FLAG.
"""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "research"))

import vault_profile as vp  # noqa: E402
import vault_watch_tg as vw  # noqa: E402

VAULTS = {
    "Delta_01": "0x3005fade4c0df5e1cd187d7062da359416f0eb8e",
    "Jade Lotus Capital": "0xbc5bf88fd012612ba92c5bd96e183955801b7fdc",
}
BOOK_USD = 10_000.0
BAND_USD = 25.0
BAND_FRAC = 0.02
TAKER_FEE = 0.00055  # Bybit USDT perpetual taker, per side (= execution.guard.TAKER_FEE; tested)
BYBIT = "https://api.bybit.com/v5/market"
STATE_DIR = Path("/var/lib/tradedesk-mcp/assistant")
DB = STATE_DIR / "copy_vaults.sqlite"
FLAG = STATE_DIR / "copy-vaults.on"

SCHEMA = """
CREATE TABLE IF NOT EXISTS book (vault TEXT PRIMARY KEY, cash REAL NOT NULL, start_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pos (vault TEXT, sym TEXT, qty REAL NOT NULL, funding_ms INTEGER NOT NULL,
  PRIMARY KEY (vault, sym));
CREATE TABLE IF NOT EXISTS trade (ts INTEGER, vault TEXT, sym TEXT, dqty REAL, px REAL, fee REAL, target REAL,
  vault_szi REAL, scale REAL);
CREATE TABLE IF NOT EXISTS funding (ts INTEGER, vault TEXT, sym TEXT, rate REAL, amount REAL);
CREATE TABLE IF NOT EXISTS mark (ts INTEGER, vault TEXT, equity REAL, vault_av REAL, gross REAL);
CREATE TABLE IF NOT EXISTS note (ts INTEGER, vault TEXT, text TEXT);
"""


def bybit_symbol(coin: str) -> str:
    """Hyperliquid coin -> Bybit linear symbol: kPEPE (price of 1 000 PEPE) is Bybit's 1000PEPEUSDT."""
    return f"1000{coin[1:]}USDT" if coin.startswith("k") and coin[1:].isupper() else f"{coin}USDT"


def vault_book(state: dict) -> tuple[float, dict[str, float]]:
    """(account value, {Bybit symbol: signed size}) of one ``clearinghouseState`` answer."""
    av = float(state["marginSummary"]["accountValue"])
    sizes: dict[str, float] = {}
    for p in state.get("assetPositions") or []:
        q = p.get("position") or {}
        szi = float(q.get("szi") or 0)
        if szi:
            sym = bybit_symbol(str(q["coin"]))
            sizes[sym] = sizes.get(sym, 0.0) + szi
    return av, sizes


def equity(cash: float, held: dict[str, float], mid: dict[str, float]) -> float:
    """Cash plus the held sizes at mid; a held coin without a quote raises (the run is skipped, not mismarked)."""
    missing = [s for s, q in held.items() if q and s not in mid]
    if missing:
        raise RuntimeError(f"no Bybit quote for held {', '.join(missing)}")
    return cash + sum(q * mid[s] for s, q in held.items() if q)


def plan(held: dict[str, float], target: dict[str, float], mid: dict[str, float], eq: float) -> list[tuple[str, float, str]]:
    """(symbol, size to trade, why) for every coin out of the band; a coin the vault closed is always closed."""
    out = []
    for sym in sorted(set(held) | set(target)):
        h, t = held.get(sym, 0.0), target.get(sym, 0.0)
        if sym not in mid:
            continue
        if t == 0 and h != 0:
            out.append((sym, -h, "close"))
        elif abs(t - h) * mid[sym] >= max(BAND_USD, BAND_FRAC * eq):
            out.append((sym, t - h, "open" if h == 0 else "resize"))
    return out


def fill_price(dqty: float, bid: float, ask: float) -> float:
    return ask if dqty > 0 else bid


def funding_due(rows: list[dict], since_ms: int) -> list[tuple[int, float]]:
    """(time, rate) of Bybit's settled funding after ``since_ms``, oldest first."""
    due = [(int(r["fundingRateTimestamp"]), float(r["fundingRate"])) for r in rows]
    return sorted((t, r) for t, r in due if t > since_ms)


# --- I/O ---------------------------------------------------------------------------------------------------------


def _get(path: str, **params: Any) -> dict:
    url = f"{BYBIT}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.load(resp)
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit {path}: {data.get('retMsg')}")
    return data["result"]


def bybit_quotes(get: Callable[..., dict] = _get) -> dict[str, tuple[float, float]]:
    """{symbol: (bid, ask)} of every linear perpetual with a two-sided book."""
    out = {}
    for t in get("tickers", category="linear")["list"]:
        bid, ask = float(t.get("bid1Price") or 0), float(t.get("ask1Price") or 0)
        if bid > 0 and ask > 0:
            out[t["symbol"]] = (bid, ask)
    return out


def connect(path: Path = DB) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def load(con: sqlite3.Connection, vault: str, now: int) -> tuple[float, dict[str, float], dict[str, int]]:
    row = con.execute("SELECT cash FROM book WHERE vault = ?", (vault,)).fetchone()
    if row is None:
        con.execute("INSERT INTO book VALUES (?, ?, ?)", (vault, BOOK_USD, now))
        return BOOK_USD, {}, {}
    pos = con.execute("SELECT sym, qty, funding_ms FROM pos WHERE vault = ?", (vault,)).fetchall()
    return row[0], {s: q for s, q, _ in pos}, {s: f for s, _, f in pos}


def step(
    con: sqlite3.Connection,
    vault: str,
    state: dict,
    quotes: dict[str, tuple[float, float]],
    funding: Callable[[str], list[dict]],
    now: int,
) -> list[str]:
    """One run for one vault: funding, then the trades to the target, then the mark. Returns trade lines."""
    cash, held, fund_ms = load(con, vault, now)
    av, sizes = vault_book(state)
    mid = {s: (b + a) / 2 for s, (b, a) in quotes.items()}
    for sym, q in held.items():
        for t, rate in funding_due(funding(sym), fund_ms.get(sym, now)):
            amount = -rate * q * mid.get(sym, 0.0)  # a long pays a positive rate
            cash += amount
            con.execute("INSERT INTO funding VALUES (?, ?, ?, ?, ?)", (t, vault, sym, rate, amount))
            fund_ms[sym] = t
    eq = equity(cash, held, mid)
    scale = eq / av if av > 0 else 0.0
    target = {s: q * scale for s, q in sizes.items()}
    for sym in target.keys() - mid.keys():
        con.execute("INSERT INTO note VALUES (?, ?, ?)", (now, vault, f"{sym}: no Bybit quote, not copied"))
    lines = []
    for sym, dq, why in plan(held, target, mid, eq):
        bid, ask = quotes[sym]
        px = fill_price(dq, bid, ask)
        fee = abs(dq) * px * TAKER_FEE
        cash -= dq * px + fee
        new = held.get(sym, 0.0) + dq
        held[sym] = 0.0 if abs(new) * px < 1e-6 else new
        con.execute(
            "INSERT INTO trade VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, vault, sym, dq, px, fee, target.get(sym, 0.0), sizes.get(sym, 0.0), scale),
        )
        fund_ms.setdefault(sym, now)
        lines.append(f"{sym} {'+' if dq > 0 else ''}{dq:.6g} @ {px:g} ({why})")
    con.execute("DELETE FROM pos WHERE vault = ?", (vault,))
    con.executemany("INSERT INTO pos VALUES (?, ?, ?, ?)", [(vault, s, q, fund_ms.get(s, now)) for s, q in held.items() if q])
    con.execute("UPDATE book SET cash = ? WHERE vault = ?", (cash, vault))
    gross = sum(abs(q) * mid.get(s, 0.0) for s, q in held.items())
    con.execute("INSERT INTO mark VALUES (?, ?, ?, ?, ?)", (now, vault, equity(cash, held, mid), av, gross))
    return lines


def run(con: sqlite3.Connection, post: Callable = vp._post, get: Callable[..., dict] = _get) -> list[str]:
    now = int(time.time() * 1000)
    quotes = bybit_quotes(get)
    states = {name: post({"type": "clearinghouseState", "user": addr}) for name, addr in VAULTS.items()}

    def funding(sym: str) -> list[dict]:
        return get("funding/history", category="linear", symbol=sym, limit=20)["list"]

    out = []
    for name, state in states.items():
        with con:  # one transaction per book: a failure leaves it as it was
            lines = step(con, name, state, quotes, funding, now)
        out += [f"{name}: {line}" for line in lines]
    return out


def summary(con: sqlite3.Connection, post: Callable = vp._post) -> list[dict]:
    """The copy against the vault since the copy's start, per vault."""
    out = []
    for name, addr in VAULTS.items():
        book = con.execute("SELECT start_ms FROM book WHERE vault = ?", (name,)).fetchone()
        last = con.execute("SELECT equity, gross FROM mark WHERE vault = ? ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
        if not book or not last:
            out.append({"vault": name, "error": "ще немає даних"})
            continue
        n, fees = con.execute("SELECT COUNT(*), COALESCE(SUM(fee), 0) FROM trade WHERE vault = ?", (name,)).fetchone()
        fund = con.execute("SELECT COALESCE(SUM(amount), 0) FROM funding WHERE vault = ?", (name,)).fetchone()[0]
        try:
            details = post({"type": "vaultDetails", "vaultAddress": addr})
            month = vp.series(details, "pnlHistory", "month")
            window = "month" if month and month[0][0] <= book[0] else "allTime"  # finer points while they cover it
            v = vw.since(details, book[0], window)
        except Exception as e:  # noqa: BLE001
            v = {"error": f"API: {type(e).__name__}"}
        out.append(
            {
                "vault": name,
                "start_ms": book[0],
                "copy_ret": last[0] / BOOK_USD - 1,
                "gross": last[1] / last[0] if last[0] else 0.0,
                "vault_ret": v.get("ret"),
                "trades": n,
                "fees": fees,
                "funding": fund,
            }
        )
    return out


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.2f} %"


def report_text(rows: list[dict], now: datetime) -> str:
    lines = ["🪞 <b>Копія волтів (папір, ціни Bybit)</b>", ""]
    for r in rows:
        name = html.escape(r["vault"])
        if "error" in r:
            lines.append(f"• <b>{name}</b>: {html.escape(r['error'])}")
            continue
        days = (now.timestamp() * 1000 - r["start_ms"]) / 86_400_000
        gap = None if r["vault_ret"] is None else r["copy_ret"] - r["vault_ret"]
        lines.append(
            f"• <b>{name}</b>, {days:.0f} дн.: копія {pct(r['copy_ret'])}, волт {pct(r['vault_ret'])}, "
            f"різниця {pct(gap)}; угод {r['trades']}, комісії ${r['fees']:.0f}, funding ${r['funding']:+.0f}, "
            f"плече {r['gross']:.1f}x"
        )
    lines += ["", "Книга $10 000 на волт. Різниця = що втрачає послідовник на запізненні, цінах Bybit, комісіях і funding."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=("run", "report", "status"))
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--tg", action="store_true", help="report: send to the demo topic")
    a = ap.parse_args(argv)
    if a.cmd == "run" and not FLAG.exists() and a.db == DB:
        print(f"off: no {FLAG}")
        return 0
    con = connect(a.db)
    if a.cmd == "run":
        try:
            lines = run(con)
        except Exception as e:  # noqa: BLE001 - one bad run is skipped; the next one catches up
            print(f"run skipped: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        print("\n".join(lines) or "no trades")
        if lines:
            import tg_send

            text = "🪞 <b>Копія волтів</b> (папір)\n" + "\n".join(html.escape(x) for x in lines[:15])
            tg_send.send(text, topic="demo", silent=True)
        return 0
    if a.cmd == "status":
        for vault, cash in con.execute("SELECT vault, cash FROM book"):
            pos = con.execute("SELECT sym, qty FROM pos WHERE vault = ?", (vault,)).fetchall()
            last = con.execute("SELECT equity FROM mark WHERE vault = ? ORDER BY ts DESC LIMIT 1", (vault,)).fetchone()
            print(vault, f"equity {last[0] if last else cash:.2f}", ", ".join(f"{s} {q:.6g}" for s, q in pos))
        return 0
    text = report_text(summary(con), datetime.now(UTC))
    if a.tg:
        import tg_send

        return 0 if tg_send.send(text, topic="demo") else 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
