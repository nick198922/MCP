"""Wikipedia attention shock, cross-sectional weekly test on Binance USDT-M perps: data and the stage-0 power gate.

Idea card: docs/ideas/IDEA-20261111-wikipedia-attention-xs-spread.md ("Тріаж критика" conditions are binding).

Data: pageviews and page metadata through ``rt.wikimedia`` (``core.http.Http``); daily perp klines from the
funding_crowding cache (data.binance.vision archive, delisted symbols included). No keys, no orders.

Stages (per-file JSON cache under --cache-dir; a rerun resumes):
    probe    one real answer of each endpoint; ``--save-fixtures`` writes them to tests/fixtures
    map      resolve the candidate titles below to en.wikipedia page ids (short description, redirects); prints a table
    freeze   apply the inclusion rule (crypto short description, EXCLUDE list, one page per asset) and write the frozen
             map to docs/verification/2026-11-11-wikipedia-coin-map.csv (built before any return is looked at)
    fetch    daily pageviews 2015-07-01 .. 2026-10-31 of every frozen page: its current title and every namespace-0
             redirect to it (pre-move views live under old titles), agent=user and agent=all-agents
    stage0   power gate from saved prices only: weeks, universe width, SD of the weekly FM slope / rank IC / quintile
             spread under a placebo (permuted) signal and under a price-only proxy (volume shock); MDE with Holm x2.
             No signal-vs-return mean is computed or printed.

    uv run python scripts/verify_wiki_attention.py <stage> --cache-dir ~/.cache/tradedesk-agents/wiki_attention
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import random
import resource
import statistics
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.funding_crowding import Bars, newey_west_se
from tradedesk.analytics.wiki_attention import (
    QUINTILE_Z_GAP,
    beta_ols,
    mde,
    ols_slope,
    placebo_sd,
    quintile_spread,
    rank_ic,
    rank_z,
    z_two_sided,
)
from tradedesk.core.errors import ProviderError
from tradedesk.server import build_server

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
MAP_CSV = ROOT / "docs" / "verification" / "2026-11-11-wikipedia-coin-map.csv"
FC_CACHE = Path("~/.cache/tradedesk-agents/funding_crowding").expanduser()
PV_START, PV_END = "20150701", "20261031"
AGENTS = ("user", "all-agents")
DAY = 86400
FIRST_RANK = int(datetime(2022, 1, 2, tzinfo=UTC).timestamp()) // DAY  # Sunday
LAST_RANK = int(datetime(2026, 9, 20, tzinfo=UTC).timestamp()) // DAY  # exit 2026-09-27 inside the cached data
BTC = "BTCUSDT"
MIN_QV = 5e6  # median daily quote volume over 30 days, USDT
MIN_AGE = 90  # days of perp history before the signal
BETA_DAYS = 30
HOLD = 7
NW_LAG = 4
N_PERM = 200
GATE_PP = 0.008  # MDE of the quintile-spread-equivalent effect, gross, per week (card + critic)
HOLM_M = 2  # two-sided sign, two hypotheses (reversal / momentum)
SEED = 11

# Candidate titles per Binance USDT-M symbol (first one that resolves to a non-disambiguation page wins). Written from
# general knowledge of en.wikipedia before any data were fetched; the inclusion rule is applied in ``freeze``.
CANDIDATES: dict[str, list[str]] = {
    "BTCUSDT": ["Bitcoin"],
    "ETHUSDT": ["Ethereum"],
    "BNBUSDT": ["BNB (cryptocurrency)", "Binance Coin", "BNB Chain"],
    "XRPUSDT": ["XRP Ledger", "Ripple (payment protocol)", "XRP"],
    "SOLUSDT": ["Solana (blockchain platform)", "Solana (blockchain)"],
    "ADAUSDT": ["Cardano (blockchain platform)", "Cardano (cryptocurrency)"],
    "DOGEUSDT": ["Dogecoin"],
    "TRXUSDT": ["Tron (cryptocurrency)", "TRON (blockchain)"],
    "DOTUSDT": ["Polkadot (blockchain platform)", "Polkadot (cryptocurrency)"],
    "LTCUSDT": ["Litecoin"],
    "BCHUSDT": ["Bitcoin Cash"],
    "LINKUSDT": ["Chainlink (blockchain)", "Chainlink"],
    "XLMUSDT": ["Stellar (payment network)", "Stellar (cryptocurrency)"],
    "XMRUSDT": ["Monero"],
    "ETCUSDT": ["Ethereum Classic"],
    "AVAXUSDT": ["Avalanche (blockchain platform)", "Avalanche (cryptocurrency)"],
    "1000SHIBUSDT": ["Shiba Inu (cryptocurrency)", "Shiba Inu coin"],
    "DASHUSDT": ["Dash (cryptocurrency)"],
    "ZECUSDT": ["Zcash"],
    "EOSUSDT": ["EOS.IO", "EOS (cryptocurrency)"],
    "IOTAUSDT": ["IOTA (technology)", "IOTA (distributed ledger)"],
    "NEOUSDT": ["Neo (blockchain platform)", "NEO (cryptocurrency)"],
    "XTZUSDT": ["Tezos"],
    "ALGOUSDT": ["Algorand"],
    "FILUSDT": ["Filecoin"],
    "ATOMUSDT": ["Cosmos (blockchain)", "Cosmos Network"],
    "UNIUSDT": ["Uniswap"],
    "AAVEUSDT": ["Aave"],
    "MKRUSDT": ["MakerDAO", "Maker (cryptocurrency)"],
    "HBARUSDT": ["Hedera (distributed ledger)", "Hedera Hashgraph"],
    "VETUSDT": ["VeChain"],
    "BSVUSDT": ["Bitcoin SV"],
    "NEARUSDT": ["Near Protocol", "NEAR Protocol"],
    "ICPUSDT": ["Internet Computer"],
    "TONUSDT": ["The Open Network", "Toncoin", "TON (blockchain)"],
    "SUIUSDT": ["Sui (blockchain)"],
    "APTUSDT": ["Aptos (blockchain)", "Aptos Labs"],
    "ARBUSDT": ["Arbitrum"],
    "OPUSDT": ["Optimism (blockchain)", "OP Mainnet"],
    "MATICUSDT": ["Polygon (blockchain)", "Polygon (cryptocurrency)"],
    "POLUSDT": ["Polygon (blockchain)", "Polygon (cryptocurrency)"],
    "FTMUSDT": ["Fantom (cryptocurrency)", "Fantom (blockchain)"],
    "SANDUSDT": ["The Sandbox (video game)", "The Sandbox"],
    "MANAUSDT": ["Decentraland"],
    "AXSUSDT": ["Axie Infinity"],
    "CHZUSDT": ["Chiliz", "Socios.com"],
    "ENJUSDT": ["Enjin"],
    "GALAUSDT": ["Gala Games"],
    "LUNAUSDT": ["Terra (blockchain)"],
    "LUNA2USDT": ["Terra (blockchain)"],
    "1000LUNCUSDT": ["Terra (blockchain)"],
    "USTCUSDT": ["Terra (blockchain)"],
    "WLDUSDT": ["World (blockchain)", "Worldcoin"],
    "HYPEUSDT": ["Hyperliquid"],
    "ENAUSDT": ["Ethena", "Ethena (finance)"],
    "KASUSDT": ["Kaspa"],
    "STXUSDT": ["Stacks (blockchain)"],
    "XEMUSDT": ["NEM (cryptocurrency)"],
    "ZILUSDT": ["Zilliqa"],
    "QTUMUSDT": ["Qtum"],
    "ONTUSDT": ["Ontology (blockchain)"],
    "WAVESUSDT": ["Waves (blockchain)", "Waves Platform"],
    "KSMUSDT": ["Kusama"],
    "GRTUSDT": ["The Graph (protocol)", "The Graph"],
    "RNDRUSDT": ["Render Network", "Render (cryptocurrency)"],
    "RENDERUSDT": ["Render Network", "Render (cryptocurrency)"],
    "THETAUSDT": ["Theta Network"],
    "HNTUSDT": ["Helium Network", "Helium (blockchain)"],
    "CRVUSDT": ["Curve Finance", "Curve (cryptocurrency)"],
    "COMPUSDT": ["Compound (cryptocurrency)", "Compound Finance"],
    "SNXUSDT": ["Synthetix"],
    "YFIUSDT": ["Yearn.finance", "Yearn Finance"],
    "SUSHIUSDT": ["SushiSwap"],
    "DYDXUSDT": ["DYdX"],
    "LDOUSDT": ["Lido Finance", "Lido DAO"],
    "IMXUSDT": ["Immutable X"],
    "FLOWUSDT": ["Flow (blockchain)"],
    "MINAUSDT": ["Mina Protocol", "Mina (cryptocurrency)"],
    "CELOUSDT": ["Celo (blockchain)", "Celo"],
    "ONEUSDT": ["Harmony (blockchain)"],
    "EGLDUSDT": ["MultiversX", "Elrond"],
    "RUNEUSDT": ["THORChain"],
    "CFXUSDT": ["Conflux Network"],
    "KLAYUSDT": ["Klaytn", "Kaia (blockchain)"],
    "KAIAUSDT": ["Kaia (blockchain)", "Klaytn"],
    "TAOUSDT": ["Bittensor"],
    "OCEANUSDT": ["Ocean Protocol"],
    "FETUSDT": ["Fetch.ai"],
    "AGIXUSDT": ["SingularityNET"],
    "TIAUSDT": ["Celestia (blockchain)"],
    "RVNUSDT": ["Ravencoin"],
    "DGBUSDT": ["DigiByte"],
    "XVGUSDT": ["Verge (cryptocurrency)", "Verge Currency"],
    "SCUSDT": ["Siacoin", "Sia (network)"],
    "ARUSDT": ["Arweave"],
    "STORJUSDT": ["Storj"],
    "LSKUSDT": ["Lisk"],
    "OMGUSDT": ["OMG Network"],
    "HOTUSDT": ["Holochain"],
    "ICXUSDT": ["ICON (blockchain)"],
    "1000PEPEUSDT": ["Pepe (cryptocurrency)", "Pepe coin"],
    "WIFUSDT": ["Dogwifhat"],
    "1000BONKUSDT": ["Bonk (cryptocurrency)"],
    "1000FLOKIUSDT": ["Floki (cryptocurrency)", "Floki Inu"],
    "TRUMPUSDT": ["$Trump"],
    "MELANIAUSDT": ["$Melania"],
    "WLFIUSDT": ["World Liberty Financial"],
    "NOTUSDT": ["Notcoin"],
    "HMSTRUSDT": ["Hamster Kombat"],
    "FARTCOINUSDT": ["Fartcoin"],
    "PENGUUSDT": ["Pudgy Penguins"],
    "APEUSDT": ["ApeCoin"],
    "GMTUSDT": ["StepN", "STEPN"],
    "CAKEUSDT": ["PancakeSwap"],
    "PYTHUSDT": ["Pyth Network"],
    "ONDOUSDT": ["Ondo Finance"],
    "STRKUSDT": ["Starknet", "StarkNet"],
    "ZKUSDT": ["ZKsync"],
    "EIGENUSDT": ["EigenLayer"],
    "SEIUSDT": ["Sei (blockchain)"],
    "INJUSDT": ["Injective (blockchain)", "Injective Protocol"],
    "BERAUSDT": ["Berachain"],
    "VIRTUALUSDT": ["Virtuals Protocol"],
    "AI16ZUSDT": ["Ai16z"],
    "PUMPUSDT": ["Pump.fun"],
    "STEEMUSDT": ["Steem", "Steemit"],
    "ROSEUSDT": ["Oasis Network"],
    "BANDUSDT": ["Band Protocol"],
    "KAVAUSDT": ["Kava (blockchain)"],
    "ZRXUSDT": ["0x (protocol)"],
    "BALUSDT": ["Balancer (cryptocurrency)"],
    "BNTUSDT": ["Bancor"],
    "LRCUSDT": ["Loopring"],
    "IOTXUSDT": ["IoTeX"],
    "LPTUSDT": ["Livepeer"],
    "AUDIOUSDT": ["Audius"],
    "ENSUSDT": ["Ethereum Name Service"],
    "CELRUSDT": ["Celer Network"],
    "POWRUSDT": ["Power Ledger"],
    "RLCUSDT": ["IExec"],
    "TWTUSDT": ["Trust Wallet"],
    "GLMUSDT": ["Golem (cryptocurrency)", "Golem Network"],
    "GLMRUSDT": ["Moonbeam (blockchain)"],
    "JASMYUSDT": ["JasmyCoin", "Jasmy"],
    "ARKMUSDT": ["Arkham Intelligence"],
    "ILVUSDT": ["Illuvium"],
    "YGGUSDT": ["Yield Guild Games"],
    "RPLUSDT": ["Rocket Pool"],
    "CVXUSDT": ["Convex Finance"],
    "PENDLEUSDT": ["Pendle Finance"],
    "ETHFIUSDT": ["Ether.fi"],
    "DYMUSDT": ["Dymension"],
    "MANTAUSDT": ["Manta Network"],
    "ZROUSDT": ["LayerZero"],
    "IPUSDT": ["Story Protocol"],
    "MOVEUSDT": ["Movement Labs"],
    "MEUSDT": ["Magic Eden"],
    "OMUSDT": ["Mantra (blockchain)", "MANTRA (blockchain)"],
    "AXLUSDT": ["Axelar"],
    "QNTUSDT": ["Quant Network"],
    "XAIUSDT": ["Xai (blockchain)"],
    "SAFEUSDT": ["Safe (wallet)"],
    "GMXUSDT": ["GMX (exchange)"],
}

# Excluded after resolution, decided on title/short description only (no returns): the page is about a company, an
# exchange, a person, an animal or an unrelated topic rather than the token / its own protocol.
# Filled 2026-11-12 from the `map` table (title | short description), before any pageviews or returns were fetched.
EXCLUDE: dict[str, str] = {
    "BNBUSDT": "page is the centralized exchange company (Binance), not the BNB token or BNB Chain",
    "OPUSDT": "page is a different asset (Ethereum), not Optimism",
    "ARUSDT": "page is a different platform (Odysee), not Arweave",
    "WIFUSDT": "page is the generic topic 'Meme coin', not this token",
    "1000FLOKIUSDT": "page is the generic topic 'Meme coin', not this token",
    "FARTCOINUSDT": "page is the generic topic 'Meme coin', not this token",
    "MELANIAUSDT": "page is a different token ($Trump)",
    "ARKMUSDT": "page is the company (blockchain analytics company), not the token",
}

CRYPTO_WORDS = (
    "crypto",
    "blockchain",
    "token",
    "coin",
    "decentrali",
    "defi",
    "ledger",
    "web3",
    "smart contract",
    "stablecoin",
    "proof-of",
    "layer",
    "exchange",
    "protocol",
    "nft",
    "metaverse",
)


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def wr(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
    tmp.replace(p)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def day_of(yyyymmdd: str) -> int:
    return int(datetime.strptime(yyyymmdd, "%Y%m%d").replace(tzinfo=UTC).timestamp()) // DAY


def ymd(day: int) -> str:
    return datetime.fromtimestamp(day * DAY, UTC).strftime("%Y-%m-%d")


async def retry(go: Any, tries: int = 4) -> Any:
    for k in range(tries):
        try:
            return await go()
        except ProviderError as e:
            if e.reason == "data_unavailable":
                return None
            if k == tries - 1:
                raise
            await asyncio.sleep(3 * (k + 1))
    return None


# ---------------------------------------------------------------- probe


async def stage_probe(save: bool) -> None:
    _, rt = build_server()
    w = rt.wikimedia
    pv = pg = None
    try:
        t0 = time.time()
        try:
            pv = await w.http.get_json(
                "/metrics/pageviews/per-article/en.wikipedia/all-access/user/Solana_(blockchain_platform)/daily/2026100100/2026100700",
                tool="probe",
            )
            print(f"pageviews: {len(pv.data.get('items', []))} items in {time.time() - t0:.1f}s; first {pv.data['items'][0]}")
        except ProviderError as e:
            print(f"pageviews: FAILED {e.reason}: {str(e)[:160]}")
        t0 = time.time()
        try:
            pg = await w.enwiki.get_json(
                "/w/api.php",
                params={
                    "action": "query",
                    "format": "json",
                    "formatversion": 2,
                    "redirects": 1,
                    "prop": "info|pageprops|redirects",
                    "ppprop": "wikibase-shortdesc|disambiguation",
                    "rdlimit": "max",
                    "rdnamespace": 0,
                    "titles": "Solana (blockchain)",
                },
                tool="probe",
            )
            page = pg.data["query"]["pages"][0]
            print(
                f"page: id {page.get('pageid')} title {page.get('title')!r} shortdesc {page.get('pageprops', {}).get('wikibase-shortdesc')!r}"
                f" redirects {len(page.get('redirects') or [])} in {time.time() - t0:.1f}s; redirected {pg.data['query'].get('redirects')}"
            )
        except ProviderError as e:
            print(f"enwiki action API: FAILED {e.reason}: {str(e)[:160]}")
        if save and pv is not None:
            (FIX / "wikimedia_pageviews_solana_2026-11-12.json").write_text(json.dumps(pv.data, indent=1) + "\n")
            print("pageviews fixture written")
        if save and pg is not None:
            (FIX / "wikipedia_page_solana_2026-11-12.json").write_text(json.dumps(pg.data, indent=1, ensure_ascii=False) + "\n")
            print("page fixture written")
    finally:
        await rt.aclose()


# ---------------------------------------------------------------- map


def symbols_cached() -> set[str]:
    return {p.name for p in (FC_CACHE / "k1d").iterdir() if p.is_dir()}


async def stage_map(cache: Path) -> None:
    have = symbols_cached()
    _, rt = build_server()
    try:
        for sym, titles in CANDIDATES.items():
            p = cache / "map" / f"{sym}.json"
            if p.exists():
                continue
            if sym not in have:
                wr(p, {"symbol": sym, "status": "no_klines"})
                continue
            tried = []
            hit = None
            for t in titles:
                f = await retry(lambda t=t: rt.wikimedia.page(t))
                s = f.data if f is not None else None
                tried.append({"title": t, "pageid": s and s["pageid"], "disambiguation": s and s["disambiguation"]})
                if s and s["pageid"] and not s["disambiguation"] and s["ns"] == 0:
                    hit = {**s, "query": t}
                    break
            wr(
                p,
                {
                    "symbol": sym,
                    "status": "ok" if hit else "unresolved",
                    "tried": tried,
                    "page": hit,
                    "resolved_at": datetime.now(UTC).strftime("%Y-%m-%d"),
                },
            )
            print(f"  {sym}: {'ok' if hit else 'unresolved'}", flush=True)
    finally:
        await rt.aclose()
    print_map(cache)


def load_map(cache: Path) -> list[dict[str, Any]]:
    return [rd(cache / "map" / f"{s}.json") for s in CANDIDATES if (cache / "map" / f"{s}.json").exists()]


def is_crypto(desc: str | None) -> bool:
    d = (desc or "").lower()
    return any(w in d for w in CRYPTO_WORDS)


def print_map(cache: Path) -> None:
    rows = load_map(cache)
    print(f"{'symbol':<14}{'pageid':>10}  {'crypto':<6} {'redir':>5}  title | shortdesc")
    for r in rows:
        pg = r.get("page")
        if not pg:
            print(f"{r['symbol']:<14}{'-':>10}  {r['status']}")
            continue
        print(
            f"{r['symbol']:<14}{pg['pageid']:>10}  {str(is_crypto(pg['shortdesc'] or pg['title'])):<6} {len(pg['redirects']):>5}  {pg['title']} | {pg['shortdesc']}"
        )
    print(Counter(r["status"] for r in rows))


def stage_freeze(cache: Path) -> None:
    rows = load_map(cache)
    out = []
    for r in rows:
        pg = r.get("page")
        if not pg:
            out.append({"symbol": r["symbol"], "status": r["status"]})
            continue
        if r["symbol"] in EXCLUDE:
            status = "excluded: " + EXCLUDE[r["symbol"]]
        elif not is_crypto(pg["shortdesc"] or pg["title"]):
            # Empty local short description (real answer for Solana, 2026-11-12): the title decides instead.
            status = "excluded: short description (or title, if empty) is not about a crypto asset"
        else:
            status = "included"
        out.append(
            {
                "symbol": r["symbol"],
                "status": status,
                "pageid": pg["pageid"],
                "title": pg["title"],
                "shortdesc": pg["shortdesc"] or "",
                "n_redirects": len(pg["redirects"]),
                "lastrevid": pg["lastrevid"],
                "resolved_at": r["resolved_at"],
            }
        )
    cols = ["symbol", "status", "pageid", "title", "shortdesc", "n_redirects", "lastrevid", "resolved_at"]
    with MAP_CSV.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for o in out:
            w.writerow({c: o.get(c, "") for c in cols})
    inc = [o for o in out if o["status"] == "included"]
    print(f"frozen map: {len(out)} symbols, {len(inc)} included, {len({o['pageid'] for o in inc})} distinct pages -> {MAP_CSV}")
    print(Counter(o["status"].split(":")[0] for o in out))


def frozen() -> list[dict[str, str]]:
    with MAP_CSV.open() as fh:
        return [r for r in csv.DictReader(fh) if r["status"] == "included"]


# ---------------------------------------------------------------- fetch


def pv_path(cache: Path, agent: str, pageid: str, title: str) -> Path:
    return cache / "pv" / agent / pageid / f"{hashlib.sha1(title.encode()).hexdigest()[:16]}.json"


async def stage_fetch(cache: Path, sample: int) -> None:
    pages: dict[str, str] = {r["pageid"]: r["symbol"] for r in frozen()}
    _, rt = build_server()
    sem = asyncio.Semaphore(6)
    done = Counter()
    t0 = time.time()
    try:
        jobs = []
        for pid, sym in sorted(pages.items()):
            info = rd(cache / "map" / f"{sym}.json")["page"]
            titles = [info["title"], *info["redirects"]]
            for agent in AGENTS:
                for t in titles:
                    p = pv_path(cache, agent, pid, t)
                    if not p.exists():
                        jobs.append((p, agent, t))
        if sample:
            jobs = jobs[:sample]
        print(f"[fetch] {len(pages)} pages, {len(jobs)} title-agent series to fetch", flush=True)

        async def one(p: Path, agent: str, t: str) -> None:
            async with sem:
                try:
                    f = await retry(lambda: rt.wikimedia.pageviews_daily(t, PV_START, PV_END, agent))
                except ProviderError as e:
                    done["error"] += 1
                    if done["error"] <= 5:
                        print(f"  error {t!r} {agent}: {e.reason} {str(e)[:160]}", flush=True)
                    return
                wr(p, {"title": t, "agent": agent, "rows": f.data if f is not None else None})
                done["ok" if f is not None else "absent"] += 1
                n = done["ok"] + done["absent"]
                if n % 100 == 0:
                    el = time.time() - t0
                    print(f"  {n}/{len(jobs)} in {el:.0f}s, eta {el / n * (len(jobs) - n):.0f}s, rss {rss_mb():.0f} MB", flush=True)

        await asyncio.gather(*[one(*j) for j in jobs])
    finally:
        await rt.aclose()
    print(f"[fetch] done in {time.time() - t0:.0f}s: {dict(done)}; peak rss {rss_mb():.0f} MB", flush=True)


def page_views(cache: Path, agent: str, pid: str, canonical_only: bool = False, title: str | None = None) -> dict[int, int]:
    """Daily views of one page summed over its current title and every redirect title (or the current title only)."""
    out: dict[int, int] = {}
    d = cache / "pv" / agent / pid
    if not d.exists():
        return out
    for f in d.iterdir():
        obj = rd(f)
        if canonical_only and obj["title"] != title:
            continue
        for ymd_s, v in obj["rows"] or []:
            k = day_of(ymd_s)
            out[k] = out.get(k, 0) + int(v)
    return out


# ---------------------------------------------------------------- stage 0


def load_bars(sym: str) -> Bars | None:
    d = FC_CACHE / "k1d" / sym
    if not d.exists():
        return None
    rows: list[list[float]] = []
    for f in sorted(d.iterdir()):
        k = rd(f)
        if k:
            rows.extend(k)
    return Bars.from_rows(rows) if rows else None


def close_at(b: Bars, day: int, floor: int) -> float | None:
    for d in range(day, floor - 1, -1):
        if d in b.close:
            return b.close[d]
    return None


def daily_rets(b: Bars, first: int, last: int) -> dict[int, float]:
    return {d: b.close[d] / b.close[d - 1] - 1.0 for d in range(first, last + 1) if d in b.close and d - 1 in b.close}


def vol_shock(b: Bars, s: int) -> float | None:
    """Price-only proxy of attention, same timing as A (lag 1): ln((mean qv 7 d + 1) / (median qv 90 d before + 1))."""
    w = [b.qv.get(d, 0.0) for d in range(s - 7, s)]
    base = [b.qv.get(d, 0.0) for d in range(s - 97, s - 7)]
    return math.log((statistics.fmean(w) + 1.0) / (statistics.median(base) + 1.0))


def stage0(cache: Path, universe: str) -> None:
    """``universe='frozen'``: pages of the frozen map with an article-age filter from the pageviews.
    ``universe='candidates'`` (used while Wikimedia is unreachable): every candidate symbol with klines, grouped by its
    first candidate title, no article-age filter -- an upper bound on the universe width, so the MDE is optimistic."""
    by_pid: dict[str, list[str]] = {}
    first_view: dict[str, int] = {}
    if universe == "frozen":
        for r in frozen():
            by_pid.setdefault(r["pageid"], []).append(r["symbol"])
        for pid in by_pid:
            v = page_views(cache, "user", pid)
            if v:
                first_view[pid] = min(v)
    else:
        for sym, titles in CANDIDATES.items():
            by_pid.setdefault(titles[0], []).append(sym)
        first_view = dict.fromkeys(by_pid, 0)
    bars = {s: b for s in {x for v in by_pid.values() for x in v} | {BTC} if (b := load_bars(s)) is not None}
    print(
        f"universe={universe}: assets {len(by_pid)} (usable {len(first_view)}), symbols with klines {len(bars) - 1}; rss {rss_mb():.0f} MB"
    )
    btc = bars[BTC]
    rng = random.Random(SEED)
    weeks = []
    widths = []
    for s in range(FIRST_RANK, LAST_RANK + 1, 7):
        bret = daily_rets(btc, s - BETA_DAYS + 1, s)
        b0, b1 = close_at(btc, s, s - 3), close_at(btc, s + HOLD, s)
        if b0 is None or b1 is None:
            continue
        rb = b1 / b0 - 1.0
        cand: dict[str, tuple[float, str]] = {}
        for pid, syms in by_pid.items():
            if pid not in first_view or first_view[pid] > s - 1 - 7 - 90 + 1:
                continue
            for sym in syms:
                b = bars.get(sym)
                if b is None or sym == BTC or s not in b.close or min(b.close) > s - MIN_AGE:
                    continue
                qv = statistics.median(b.qv.get(d, 0.0) for d in range(s - 29, s + 1))
                if qv < MIN_QV:
                    continue
                if pid not in cand or qv > cand[pid][0]:
                    cand[pid] = (qv, sym)
        ys, proxy = [], []
        for _pid, (_qv, sym) in cand.items():
            b = bars[sym]
            r = daily_rets(b, s - BETA_DAYS + 1, s)
            common = [d for d in r if d in bret]
            if len(common) < 20:
                continue
            beta = beta_ols([r[d] for d in common], [bret[d] for d in common])
            c1 = close_at(b, s + HOLD, s)
            assert c1 is not None
            ys.append(c1 / b.close[s] - 1.0 - beta * rb)
            proxy.append(vol_shock(b, s))
        if len(ys) < 10:
            continue
        widths.append(len(ys))
        sd_sl, sd_ic, sd_sp = placebo_sd(ys, N_PERM, rng)
        z = rank_z(proxy)
        weeks.append(
            {
                "day": s,
                "n": len(ys),
                "xs_sd": statistics.pstdev(ys),
                "pl_slope_sd": sd_sl,
                "pl_ic_sd": sd_ic,
                "pl_spread_sd": sd_sp,
                "px_slope": ols_slope(ys, z),
                "px_ic": rank_ic(proxy, ys),
                "px_spread": quintile_spread(proxy, ys),
            }
        )
    wr(cache / f"stage0_weeks_{universe}.json", weeks)
    T = len(weeks)
    zc = z_two_sided(0.05, HOLM_M)

    def rms(key: str) -> float:
        return math.sqrt(statistics.fmean(w[key] ** 2 for w in weeks))

    print()
    print("STAGE 0 (no signal-vs-return means). Perps 2021-10..2026-09 only; spot 2018-21 not in the cache (not mixed).")
    print(f"weeks T={T} ({ymd(weeks[0]['day'])} .. {ymd(weeks[-1]['day'])}); universe N per week: median {statistics.median(widths)}")
    print(
        f"  min {min(widths)} max {max(widths)}; cross-sectional SD of 7-d beta-neutral return: median {statistics.median(w['xs_sd'] for w in weeks) * 100:.2f} pp"
    )
    print(f"Holm x{HOLM_M} two-sided alpha 0.05 -> z {zc:.3f}; power 0.80 -> multiplier {zc + 0.8416:.3f}")
    se_pl_slope = rms("pl_slope_sd") / math.sqrt(T)
    se_pl_ic = rms("pl_ic_sd") / math.sqrt(T)
    se_pl_sp = rms("pl_spread_sd") / math.sqrt(T)
    px_slope = [w["px_slope"] for w in weeks]
    px_ic = [w["px_ic"] for w in weeks]
    px_sp = [w["px_spread"] for w in weeks]
    se_px_slope, se_px_ic, se_px_sp = (newey_west_se(x, NW_LAG) for x in (px_slope, px_ic, px_sp))
    res = {}
    print(f"{'statistic':<46}{'SD/week':>10}{'SE':>9}{'MDE':>9}  {'MDE spread-eq pp/wk':>20}")
    for lab, sd, se, scale in (
        ("FM slope on rank z, placebo", rms("pl_slope_sd"), se_pl_slope, QUINTILE_Z_GAP),
        ("FM slope on rank z, proxy (NW4)", statistics.stdev(px_slope), se_px_slope, QUINTILE_Z_GAP),
        ("rank IC, placebo", rms("pl_ic_sd"), se_pl_ic, None),
        ("rank IC, proxy (NW4)", statistics.stdev(px_ic), se_px_ic, None),
        ("quintile spread, placebo", rms("pl_spread_sd"), se_pl_sp, 1.0),
        ("quintile spread, proxy (NW4)", statistics.stdev(px_sp), se_px_sp, 1.0),
    ):
        m = mde(se, m=HOLM_M)
        eq = f"{m * scale * 100:20.3f}" if scale else f"{'':>20}"
        unit = 100.0 if scale else 1.0
        print(f"{lab:<46}{sd * unit:10.3f}{se * unit:9.3f}{m * unit:9.3f}  {eq}")
        res[lab] = {"sd": sd, "se": se, "mde": m, "mde_spread_eq": m * scale if scale else None}
    xs_med = statistics.median(w["xs_sd"] for w in weeks)
    ic_conv = max(res["rank IC, placebo"]["mde"], res["rank IC, proxy (NW4)"]["mde"]) * xs_med * QUINTILE_Z_GAP
    print(f"critic's conversion MDE_IC x sigma_xs x {QUINTILE_Z_GAP:.2f} (larger IC MDE): {ic_conv * 100:.3f} pp/week")
    gate_mde = max(res["FM slope on rank z, placebo"]["mde_spread_eq"], res["FM slope on rank z, proxy (NW4)"]["mde_spread_eq"])
    passed = gate_mde <= GATE_PP
    print(
        f"GATE: main test FM slope, spread-equivalent MDE (larger of placebo/proxy) {gate_mde * 100:.3f} pp/week gross <= {GATE_PP * 100:.1f}: {passed}"
    )
    print(f"peak rss {rss_mb():.0f} MB")
    wr(
        cache / f"stage0_{universe}.json",
        {"universe": universe, "T": T, "median_N": statistics.median(widths), "res": res, "gate_mde": gate_mde, "passed": passed},
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["probe", "map", "freeze", "fetch", "stage0"])
    ap.add_argument("--cache-dir", type=Path, default=Path("~/.cache/tradedesk-agents/wiki_attention"))
    ap.add_argument("--save-fixtures", action="store_true")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--universe", choices=["frozen", "candidates"], default="frozen")
    a = ap.parse_args()
    cache = a.cache_dir.expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "probe":
        asyncio.run(stage_probe(a.save_fixtures))
    elif a.stage == "map":
        asyncio.run(stage_map(cache))
    elif a.stage == "freeze":
        stage_freeze(cache)
    elif a.stage == "fetch":
        asyncio.run(stage_fetch(cache, a.sample))
    else:
        stage0(cache, a.universe)


if __name__ == "__main__":
    main()
