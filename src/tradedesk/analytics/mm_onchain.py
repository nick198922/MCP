"""On-chain flows of labelled market-maker wallets to exchanges (IDEA-20261017-market-makers, part B).

Pure functions, no I/O: wallet / exchange label rules, daily MM-to-exchange flows in USD, the registered event
rule (share of the perp's trailing 30-day quote volume, priced at the previous day's close) and the power gate.
Dates are ``YYYY-MM-DD`` UTC strings; shares are decimals (0.02 = 2 %).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

MM_FIRMS: tuple[str, ...] = (
    "Wintermute",
    "Jump Trading",
    "GSR",
    "DWF Labs",
    "Amber Group",
    "Cumberland",
    "B2C2",
    "Auros",
    "QCP Capital",
    "Flow Traders",
    "Kronos Research",
    "Gotbit",
    "Flowdesk",
    "Keyrock",
)

EXCHANGES: tuple[str, ...] = (
    "Binance",
    "OKX",
    "OKEx",
    "Bybit",
    "Coinbase",
    "Kraken",
    "KuCoin",
    "Gate",
    "HTX",
    "Huobi",
    "Bitget",
    "MEXC",
    "Bitfinex",
    "Crypto.com",
    "Upbit",
    "Bithumb",
    "Bitstamp",
    "Gemini",
    "Poloniex",
    "BingX",
)

# Not the subject of H3: stablecoins, BTC / ETH and their wrappers / liquid-staking tokens.
EXCLUDED_SYMBOLS: frozenset[str] = frozenset(
    {
        "USDT",
        "USDC",
        "DAI",
        "FDUSD",
        "TUSD",
        "USDE",
        "PYUSD",
        "BUSD",
        "USDP",
        "GUSD",
        "FRAX",
        "LUSD",
        "USDS",
        "SUSDE",
        "USD0",
        "RLUSD",
        "EURC",
        "EURT",
        "WETH",
        "ETH",
        "WBTC",
        "BTC",
        "CBBTC",
        "TBTC",
        "STETH",
        "WSTETH",
        "CBETH",
        "RETH",
        "WEETH",
        "EETH",
        "METH",
        "XAUT",
        "PAXG",
    }
)


def _firm_regex(names: Iterable[str]) -> re.Pattern[str]:
    alts = "|".join(re.escape(n).replace(r"\ ", r"\s*") for n in names)
    return re.compile(rf"^\s*({alts})\b", re.IGNORECASE)


_MM_RE = _firm_regex(MM_FIRMS)


def mm_firm(name_tag: str, firms: tuple[str, ...] = MM_FIRMS) -> str | None:
    """Firm of a market-maker wallet label, or None. The tag must START with the firm name (so «Fringe Finance: Jump
    Rate Model» or a hex address containing «b2c2» do not match) and must not be a deposit address (those are
    exchange-side addresses and count as destinations)."""
    if not name_tag or "deposit" in name_tag.lower():
        return None
    m = (_MM_RE if firms is MM_FIRMS else _firm_regex(firms)).match(name_tag)
    if not m:
        return None
    hit = re.sub(r"\s+", "", m.group(1)).lower()
    for f in firms:
        if f.replace(" ", "").lower() == hit:
            return f
    return None


_EXN = r"(?:" + "|".join(re.escape(x) for x in EXCHANGES) + r")(?:\.io|\.com)?"
# Accepted tag shapes (case-insensitive, whole tag):
#   «Binance», «Binance 14», «Coinbase Prime 3», «Coinbase Cold 14», «Kraken: Hot Wallet 4», «Coinbase: LINK Hot 12»
#   deposits: «Bitget Dep: 0x…», «Binance: Deposit 1», «Galaxy Digital: HTX Deposit», «Jump Trading: Binance Deposit»
_EX_TAG_RE = re.compile(
    rf"^\s*(?:"
    rf"{_EXN}(?:\s+Prime)?(?:\s+\d+)?"
    rf"|{_EXN}(?:\s+Prime)?:?\s+(?:[A-Z0-9]+\s+)?(?:Hot|Cold)(?:\s+Wallet)?(?:\s+\d+)?"
    rf"|{_EXN}\s+Dep:\s*0x[0-9a-f.]+"
    rf"|{_EXN}:\s*Deposit(?:\s+\d+)?"
    rf"|[^:]+:\s*{_EXN}\s+Deposit(?:\s+\d+)?"
    rf")\s*$",
    re.IGNORECASE,
)
_EXN_LABEL = _EXN.replace(r"\.", "[-.]")  # label categories write «gate-io», «crypto-com»
_EX_LABEL_DEPOSIT_RE = re.compile(rf"(?:^|\s){_EXN_LABEL}-deposit(?:\s|$)", re.IGNORECASE)
# Label categories that are never an exchange's receiving address, whatever the tag says.
_NOT_EXCHANGE_LABELS = ("exploit", "charity", "blocked", "bitdao", "avs-operator", "ofac")


def is_exchange_address(name_tag: str, label: str = "") -> bool:
    """Exchange hot / cold wallet or deposit address, by the shape of the whole tag («Binance 14», «Kraken: Hot Wallet
    4», «Bitget Dep: 0x…», «Galaxy Digital: HTX Deposit»). An exchange name elsewhere in the tag is not enough: token
    contracts («Tether: USDT Stablecoin», «Binance: BNB Token»), exploiters («Bybit Exploiter 33»), charities, locked
    treasuries («BitDAO: Bybit Locked 1»), AVS operators, deployers and «Deposit Funder» gas wallets are rejected, and so
    is any label category in ``_NOT_EXCHANGE_LABELS``. With an empty tag only an explicit «<exchange>-deposit» label
    category counts."""
    tag, lab = (name_tag or "").strip(), (label or "").lower()
    if any(x in lab for x in _NOT_EXCHANGE_LABELS):
        return False
    if not tag:
        return bool(_EX_LABEL_DEPOSIT_RE.search(lab))
    return bool(_EX_TAG_RE.match(tag))


@dataclass(frozen=True)
class PageStep:
    kept: list[Mapping[str, object]]  # rows that belong to this page (block < next_start, or the truncated block)
    next_start: int  # start block of the next request
    done: bool  # the wallet is finished
    truncated: bool  # one block held >= page rows: the rest of that block cannot be fetched


def next_page(rows: Sequence[Mapping[str, object]], start: int, b1: int, page: int) -> PageStep:
    """Paging decision for an ascending ``startblock``-based crawl (Etherscan ``tokentx``, ``page=1``, ``offset=page``).

    A short page closes the wallet. A full page may cut its last block in half, so the rows of that block are dropped
    and the next request starts at it again. A full page that lies inside one block cannot be paged further: its rows
    are kept, the page is flagged ``truncated`` and the crawl moves to the next block."""
    if len(rows) < page:
        return PageStep(list(rows), b1 + 1, True, False)
    last = int(str(rows[-1]["blockNumber"]))
    if last <= start:
        return PageStep(list(rows), start + 1, start + 1 > b1, True)
    kept = [r for r in rows if int(str(r["blockNumber"])) < last]
    return PageStep(kept, last, False, False)


def utc_day(ts: int) -> str:
    return (date(1970, 1, 1) + timedelta(days=ts // 86_400)).isoformat()


def shift(day: str, k: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=k)).isoformat()


@dataclass(frozen=True)
class Transfer:
    ts: int  # unix seconds (block time)
    src: str  # lower-case address
    dst: str
    contract: str
    symbol: str  # upper-case token symbol
    amount: float  # token units (value / 10**decimals)


def pick_contracts(transfers: Iterable[Transfer]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Collision rule: per symbol keep only the contract with the most outgoing MM transfers.
    Returns ({symbol: contract}, {symbol: [dropped contracts]})."""
    counts: dict[tuple[str, str], int] = {}
    for t in transfers:
        counts[(t.symbol, t.contract)] = counts.get((t.symbol, t.contract), 0) + 1
    return pick_contracts_from_counts(counts)


def pick_contracts_from_counts(counts: Mapping[tuple[str, str], int]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """The same collision rule from {(symbol, contract): number of outgoing MM transfers} (all destinations)."""
    by_sym: dict[str, dict[str, int]] = {}
    for (sym, c), n in counts.items():
        by_sym.setdefault(sym, {})[c] = by_sym.get(sym, {}).get(c, 0) + n
    return _pick(by_sym)


def _pick(counts: Mapping[str, Mapping[str, int]]) -> tuple[dict[str, str], dict[str, list[str]]]:
    keep: dict[str, str] = {}
    dropped: dict[str, list[str]] = {}
    for sym, by_c in counts.items():
        order = sorted(by_c.items(), key=lambda kv: (-kv[1], kv[0]))
        keep[sym] = order[0][0]
        if len(order) > 1:
            dropped[sym] = [c for c, _ in order[1:]]
    return keep, dropped


def daily_token_flows(
    transfers: Iterable[Transfer], keep: Mapping[str, str], excluded: frozenset[str] = EXCLUDED_SYMBOLS
) -> dict[tuple[str, str], float]:
    """{(symbol, day): token units sent to exchanges that day}, kept contracts only, excluded symbols removed."""
    out: dict[tuple[str, str], float] = {}
    for t in transfers:
        if t.symbol in excluded or keep.get(t.symbol) != t.contract or not math.isfinite(t.amount) or t.amount <= 0:
            continue
        k = (t.symbol, utc_day(t.ts))
        out[k] = out.get(k, 0.0) + t.amount
    return out


@dataclass(frozen=True)
class Bar:
    open: float
    close: float
    quote_volume: float


@dataclass(frozen=True)
class Event:
    symbol: str
    day: str
    usd: float
    share: float  # usd / trailing 30-day mean daily quote volume


def flow_event(amount: float, day: str, bars: Mapping[str, Bar], *, min_share: float, min_usd: float, adv_days: int = 30) -> Event | None:
    """Registered rule for one (token, day): price at the close of d − 1, ADV over d − 30 … d − 1 (all 30 days must
    exist and be positive). Uses nothing from day d or later, so later data cannot change the decision."""
    prev = bars.get(shift(day, -1))
    if prev is None or prev.close <= 0:
        return None
    vols = []
    for k in range(1, adv_days + 1):
        b = bars.get(shift(day, -k))
        if b is None or b.quote_volume <= 0:
            return None
        vols.append(b.quote_volume)
    adv = sum(vols) / len(vols)
    usd = amount * prev.close
    share = usd / adv
    if usd >= min_usd and share >= min_share:
        return Event("", day, usd, share)
    return None


def find_events(
    flows: Mapping[tuple[str, str], float],
    bars_by_symbol: Mapping[str, Mapping[str, Bar]],
    *,
    min_share: float = 0.02,
    min_usd: float = 250_000.0,
    dedupe_days: int = 7,
    first_day: str = "0000-01-01",
    last_day: str = "9999-12-31",
) -> list[Event]:
    """All events in chronological order per token; an event is dropped if the same token had a kept event in the
    previous ``dedupe_days`` days. ``flows`` keys use token symbols; ``bars_by_symbol`` is keyed the same way."""
    out: list[Event] = []
    by_sym: dict[str, list[tuple[str, float]]] = {}
    for (sym, day), amt in flows.items():
        if first_day <= day <= last_day:
            by_sym.setdefault(sym, []).append((day, amt))
    for sym in sorted(by_sym):
        bars = bars_by_symbol.get(sym)
        if not bars:
            continue
        last_kept: str | None = None
        for day, amt in sorted(by_sym[sym]):
            ev = flow_event(amt, day, bars, min_share=min_share, min_usd=min_usd)
            if ev is None:
                continue
            if last_kept is not None and day <= shift(last_kept, dedupe_days):
                continue
            out.append(Event(sym, day, ev.usd, ev.share))
            last_kept = day
    out.sort(key=lambda e: (e.day, e.symbol))
    return out


def short_return(bars: Mapping[str, Bar], day: str, h: int) -> float | None:
    """Gross short return: entry at the open of d + 1, exit at the close of d + h; None if a bar is missing."""
    a, b = bars.get(shift(day, 1)), bars.get(shift(day, h))
    if a is None or b is None or a.open <= 0 or b.close <= 0:
        return None
    return -(b.close / a.open - 1.0)


def mde(sd: float, k: int, z: float = 2.8) -> float:
    """Minimum detectable effect, 80 % power at two-sided 5 %: 2.8 × SD / √k (k independent clusters)."""
    if k <= 0 or not math.isfinite(sd):
        return math.inf
    return z * sd / math.sqrt(k)
