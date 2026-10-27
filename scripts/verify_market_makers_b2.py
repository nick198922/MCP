"""Part B2 of IDEA-20261017-market-makers: two-hop detection of MM exchange deposit addresses (H3).

Stages (compact summaries on stdout; raw responses go to the per-file cache under --cache-dir):
  prereg     SHA-256 of the card section «Попередня реєстрація (частина B2» with the UTC time; writes the registration
             file once (never overwrites it).
  crawl      recrawl Etherscan ``tokentx`` of the 16 MM wallets into tx4/: outgoing rows of non-excluded tokens to any
             receiver, incoming rows from labelled exchange addresses; counts checked against tx3/.
  receivers  one ``tokentx`` call per candidate receiver (phase 1, then phase 2), cached in recv/<address>.json.
  stage0     deposit-address classification, registered event count, clusters and the power gate (no event means).

The Etherscan key is read from ~/.config/tradedesk/onchain.env by the part B script helpers; never printed or cached.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import statistics
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import verify_market_makers_onchain as base  # noqa: E402

from tradedesk.analytics.mm_onchain import (  # noqa: E402
    EXCLUDED_SYMBOLS,
    Transfer,
    daily_token_flows,
    find_events,
    mde,
    next_page,
    pick_contracts_from_counts,
    shift,
    short_return,
    utc_day,
)
from tradedesk.analytics.mm_twohop import classify_receiver, exchange_bound, known_since, net_daily  # noqa: E402
from tradedesk.core.errors import ProviderError  # noqa: E402
from tradedesk.core.http import Http  # noqa: E402

CARD = base.CARD
PREREG_HEAD = "## Попередня реєстрація (частина B2"
RESULTS_HEAD = "\n## Частина B2: результати"
REG_FILE = ROOT / "docs" / "verification" / "2026-10-27-market-makers-b2.registration.txt"
DEFAULT_CACHE = base.DEFAULT_CACHE
TX = "tx4"
RECV = "recv"
B1 = 26_065_069
BUDGET = 20_000
SEED = 20261027
WINDOW, MIN_SHARE, MIN_SWEEPS = 86_400, 0.8, 1
log = print


def prereg_hash() -> tuple[str, int]:
    text = CARD.read_text(encoding="utf-8")
    start = text.index(PREREG_HEAD)
    end = text.find(RESULTS_HEAD, start)
    raw = (text[start : end if end >= 0 else len(text)].rstrip() + "\n").encode("utf-8")
    return hashlib.sha256(raw).hexdigest(), len(raw)


def prereg() -> None:
    h, n = prereg_hash()
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"section «Попередня реєстрація (частина B2…)» sha256={h} bytes={n}"
    if REG_FILE.exists():
        first = REG_FILE.read_text(encoding="utf-8")
        log(f"registration file exists (kept):\n{first.strip()}\nnow {now}: {line}; same={h in first}")
        return
    REG_FILE.write_text(
        f"IDEA-20261017-market-makers, part B2 pre-registration\nwritten_utc={now}\n{line}\n"
        "written by scripts/verify_market_makers_b2.py prereg before the first B2 Etherscan call or price load\n",
        encoding="utf-8",
    )
    log(f"wrote {REG_FILE.relative_to(ROOT)}: {now} {line}")


class Es:
    def __init__(self, key: str) -> None:
        self.key = key
        self.http = Http("etherscan", "https://api.etherscan.io", rate=2.0, burst=1, timeout=60.0, retries=4)

    async def tokentx(self, address: str, start: int, end: int, secrets: dict[str, str]) -> list[dict[str, Any]] | None:
        """Rows of one page, [] for "No transactions", None on a persistent error (not cached; retried next run)."""
        for attempt in range(8):
            try:
                got = await self.http.get_json(
                    "/v2/api",
                    params={"chainid": 1, "module": "account", "action": "tokentx", "address": address,
                            "startblock": start, "endblock": end, "page": 1, "offset": base.PAGE, "sort": "asc",
                            "apikey": self.key},
                )  # fmt: skip
            except ProviderError as e:
                log(f"  {address[:10]} ProviderError {e.reason}: {base.scrub(str(e.detail), secrets)[:100]}")
                return None
            d = got.data
            res = d.get("result")
            if isinstance(res, list):
                return res
            if "No transactions" in str(d.get("message")) or "No transactions" in str(res):
                return []
            if "rate limit" in str(res).lower():
                await asyncio.sleep(1.0 + attempt)
                continue
            log(f"  {address[:10]}: {d.get('message')} {base.scrub(str(res), secrets)[:100]}")
            return None
        return None


def wallets_all(cache: Path) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]], set[str]]:
    main_w, sens, ex = base.wallet_sets(cache)
    return {**sens, **main_w}, main_w, ex  # B2 main = all 16 (2026 dump); sensitivity = 2023 snapshot (main_w of B)


async def crawl(cache: Path, secrets: dict[str, str]) -> None:
    w, _, ex = wallets_all(cache)
    b0, b1 = json.loads((cache / "blocks.json").read_text())
    log(f"wallets {len(w)}, exchange addresses {len(ex)}, blocks {b0}..{b1}")
    es = Es(secrets["ETHERSCAN_API_KEY"])
    t0, calls = time.time(), 0
    for i, (addr, (firm, _tag)) in enumerate(sorted(w.items(), key=lambda kv: kv[1])):
        wd = cache / TX / addr
        wd.mkdir(parents=True, exist_ok=True)
        if (wd / "done.json").exists():
            continue
        prev = sorted(wd.glob("p_*.json"))
        start, pages = (json.loads(prev[-1].read_text())["next_start"], len(prev)) if prev else (b0, 0)
        while True:
            res = await es.tokentx(addr, start, b1, secrets)
            calls += 1
            if res is None:
                break
            st = next_page(res, start, b1, base.PAGE)
            out = [r for r in st.kept if str(r.get("from", "")).lower() == addr]
            keep_out = [
                [int(r["timeStamp"]), int(r["blockNumber"]), r["hash"], r["to"].lower(), r["contractAddress"].lower(),
                 (r.get("tokenSymbol") or "").upper(), r["value"], int(r.get("tokenDecimal") or 0)]
                for r in out if (r.get("tokenSymbol") or "").upper() not in EXCLUDED_SYMBOLS
            ]  # fmt: skip
            keep_in = [
                [int(r["timeStamp"]), int(r["blockNumber"]), r["hash"], r["from"].lower(), r["contractAddress"].lower(),
                 (r.get("tokenSymbol") or "").upper(), r["value"], int(r.get("tokenDecimal") or 0)]
                for r in st.kept
                if str(r.get("to", "")).lower() == addr and str(r.get("from", "")).lower() in ex
                and (r.get("tokenSymbol") or "").upper() not in EXCLUDED_SYMBOLS
            ]  # fmt: skip
            n_out_ex = sum(1 for r in out if r["to"].lower() in ex)
            (wd / f"p_{pages:05d}.json").write_text(
                json.dumps({"start": start, "next_start": st.next_start, "n_unique": len(st.kept), "n_out": len(out),
                            "n_out_ex": n_out_ex, "truncated": st.truncated, "out": keep_out, "in_ex": keep_in})
            )  # fmt: skip
            pages += 1
            if st.done:
                (wd / "done.json").write_text(json.dumps({"pages": pages}))
                break
            start = st.next_start
        log(f"[{i + 1}/{len(w)}] {firm:12s} {addr[:10]} pages={pages} done={(wd / 'done.json').exists()} "
            f"calls={calls} t={time.time() - t0:.0f}s rss={base.rss_mb():.0f}MB", flush=True)  # fmt: skip
    await es.http.aclose()
    # consistency with tx3: outgoing counts per wallet and the rows to exchanges
    for addr, (firm, _) in sorted(w.items(), key=lambda kv: kv[1]):
        a = sum(json.loads(p.read_text())["n_out"] for p in (cache / "tx3" / addr).glob("p_*.json"))
        b = sum(json.loads(p.read_text())["n_out"] for p in (cache / TX / addr).glob("p_*.json"))
        c3 = sum(len(json.loads(p.read_text())["rows"]) for p in (cache / "tx3" / addr).glob("p_*.json"))
        c4 = sum(json.loads(p.read_text())["n_out_ex"] for p in (cache / TX / addr).glob("p_*.json"))
        log(f"  check {firm:12s} {addr[:10]} n_out tx3={a} tx4={b} to_ex tx3={c3} tx4={c4} {'OK' if (a, c3) == (b, c4) else 'DIFF'}")
    log("crawl finished", flush=True)


def load_out(cache: Path, wallets: dict[str, tuple[str, str]]) -> tuple[list[tuple[str, Transfer]], list[Transfer]]:
    """([(firm, outgoing MM transfer, non-excluded token)], [withdrawals from labelled exchanges to MM wallets])."""
    out: list[tuple[str, Transfer]] = []
    wd: list[Transfer] = []
    for addr, (firm, _) in wallets.items():
        for p in sorted((cache / TX / addr).glob("p_*.json")):
            d = json.loads(p.read_text())
            for ts, _b, _h, to, c, sym, v, dec in d["out"]:
                out.append((firm, Transfer(ts, addr, to, c, sym, int(v) / 10**dec if v.isdigit() else float("nan"))))
            for ts, _b, _h, fr, c, sym, v, dec in d["in_ex"]:
                wd.append(Transfer(ts, fr, addr, c, sym, int(v) / 10**dec if v.isdigit() else float("nan")))
    return out, wd


def candidates(cache: Path) -> tuple[list[str], list[str], dict[str, int]]:
    w, _, ex = wallets_all(cache)
    out, _ = load_out(cache, w)
    first: dict[str, int] = {}
    for _, t in out:
        if t.dst not in ex and t.dst not in w:
            first[t.dst] = min(first.get(t.dst, 1 << 62), t.ts)
    agg: Counter[str] = Counter()
    for a in w:
        for p in (cache / "tx3" / a).glob("p_*.json"):
            agg.update(json.loads(p.read_text())["receivers"])
    ph1 = sorted((r for r in first if agg[r] >= 2), key=lambda r: (-agg[r], r))
    rest = sorted(r for r in first if agg[r] < 2)
    random.Random(SEED).shuffle(rest)
    return ph1, rest[: max(0, BUDGET - len(ph1))], first


async def receivers(cache: Path, secrets: dict[str, str], phase: int, limit: int) -> None:
    w, _, ex = wallets_all(cache)
    ph1, ph2, first_ts = candidates(cache)
    # start block of the first MM -> R transfer (non-excluded token)
    first_blk: dict[str, int] = {}
    for addr in w:
        for p in sorted((cache / TX / addr).glob("p_*.json")):
            for _ts, blk, _h, to, *_ in json.loads(p.read_text())["out"]:
                if to in first_ts and to not in first_blk:
                    first_blk[to] = blk
                elif to in first_blk:
                    first_blk[to] = min(first_blk[to], blk)
    todo = ph1 if phase == 1 else ph2
    rd = cache / RECV
    rd.mkdir(exist_ok=True)
    have = {p.stem for p in rd.glob("0x*.json")}
    left = [r for r in todo if r not in have][:limit]
    log(f"candidates: phase1 {len(ph1)}, phase2 {len(ph2)} (budget {BUDGET}); phase {phase}: cached "
        f"{len(todo) - len([r for r in todo if r not in have])}, to fetch now {len(left)}", flush=True)  # fmt: skip
    es = Es(secrets["ETHERSCAN_API_KEY"])
    t0, n_full = time.time(), 0
    for i, r in enumerate(left):
        res = await es.tokentx(r, first_blk[r], B1, secrets)
        if res is None:
            continue
        rows = []
        for x in res:
            fr, to = str(x.get("from", "")).lower(), str(x.get("to", "")).lower()
            d = "o" if fr == r else "i"
            cp = to if d == "o" else fr
            tag = cp if (cp in ex or cp in w) else "x"
            rows.append([int(x["timeStamp"]), d, tag, x["contractAddress"].lower() if tag != "x" else ""])
        n_full += len(res) >= base.PAGE
        (rd / f"{r}.json").write_text(json.dumps({"start": first_blk[r], "n_raw": len(res), "rows": rows}))
        if (i + 1) % 200 == 0 or i + 1 == len(left):
            el = time.time() - t0
            log(f"  [{i + 1}/{len(left)}] full_pages={n_full} t={el:.0f}s eta={el / (i + 1) * (len(left) - i - 1):.0f}s "
                f"rss={base.rss_mb():.0f}MB", flush=True)  # fmt: skip
    await es.http.aclose()
    log(f"receivers phase {phase} finished", flush=True)


def recv_rows(cache: Path, r: str) -> list[Transfer] | None:
    f = cache / RECV / f"{r}.json"
    if not f.exists():
        return None
    d = json.loads(f.read_text())
    return [Transfer(ts, r if dr == "o" else cp, cp if dr == "o" else r, c, "", 1.0) for ts, dr, cp, c in d["rows"]]


def classify_all(cache: Path, recv: list[str], w: dict[str, Any], ex: set[str], **kw: Any) -> tuple[dict[str, int | None], Counter[str]]:
    """{deposit address: known_since}, plus counts of verdict reasons."""
    dep: dict[str, int | None] = {}
    why: Counter[str] = Counter()
    for r in recv:
        rows = recv_rows(cache, r)
        if rows is None:
            why["not fetched"] += 1
            continue
        v = classify_receiver(r, rows, w, ex, **kw)
        why["deposit" if v.is_deposit else ("no sweep" if v.n_sweeps == 0 else "share < min")] += 1
        if v.is_deposit:
            dep[r] = known_since(r, rows, w, ex, **kw)
    return dep, why


COST, STRESS = 0.0041, 0.0101


def load_funding(sym: str) -> list[tuple[int, float]]:
    d = base.DATA_DIR / "funding" / sym
    out: list[tuple[int, float]] = []
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        out += [(int(r[0]), float(r[2])) for r in json.loads(p.read_text()) or []]
    return out


def fmt(t: tuple[float, float, float, float]) -> str:
    return f"{t[0] * 100:+.2f} % [{t[1] * 100:+.2f}; {t[2] * 100:+.2f}] p1={t[3]:.4f}"


_FUND: dict[str, list[tuple[int, float]]] = {}


def brief(evs: list[Any], bars: dict[str, dict[str, Any]]) -> None:
    """Robustness rows (reported, do not affect the verdict): net short h=3 and the difference with placebo."""
    from tradedesk.analytics.mm_twohop import cluster_boot, short_net

    rng = random.Random(SEED + 1)
    ev_days: dict[str, list[str]] = {}
    for e in evs:
        ev_days.setdefault(e.symbol, []).append(e.day)
    net, diff, cl, cld = [], [], [], []
    for e in evs:
        f = _FUND.setdefault(e.symbol, load_funding(e.symbol))
        r3 = short_net(bars[e.symbol], f, e.day, 3, COST)
        if r3 is None:
            continue
        net.append(r3[0])
        cl.append(e.day)
        cand = [c for c in (shift(e.day, -k) for k in range(7, 121)) if all(abs(base._d(c) - base._d(x)) > 7 for x in ev_days[e.symbol])]
        pv = [x[0] for x in (short_net(bars[e.symbol], f, c, 3, COST) for c in rng.sample(cand, min(20, len(cand)))) if x is not None]
        if pv:
            diff.append(r3[0] - statistics.fmean(pv))
            cld.append(e.day)
    log(f"      net h=3: {fmt(cluster_boot(net, cl))}; diff vs placebo: {fmt(cluster_boot(diff, cld))}")


def outcomes(evs: list[Any], bars: dict[str, dict[str, Any]], by_ev: dict[tuple[str, str], Counter[str]]) -> None:
    """Registered outcomes of the main configuration (called only after the gate has passed)."""
    from tradedesk.analytics.mm_twohop import cluster_boot, short_net
    from tradedesk.analytics.news_events import cochran_q, holm

    fund: dict[str, list[tuple[int, float]]] = {}
    btc = base.load_bars("BTCUSDT")
    rng = random.Random(SEED + 1)
    ev_days: dict[str, list[str]] = {}
    for e in evs:
        ev_days.setdefault(e.symbol, []).append(e.day)
    rows = []  # (event, net1, net3, net3_stress, gross3, fund3, placebo mean net3 or None, btc-hedged net3)
    for e in evs:
        f = fund.setdefault(e.symbol, load_funding(e.symbol))
        r1, r3 = short_net(bars[e.symbol], f, e.day, 1, COST), short_net(bars[e.symbol], f, e.day, 3, COST)
        if r1 is None or r3 is None:
            log(f"   no outcome bars: {e.day} {e.symbol}")
            continue
        cand = [c for c in (shift(e.day, -k) for k in range(7, 121)) if all(abs(base._d(c) - base._d(x)) > 7 for x in ev_days[e.symbol])]
        pl = [short_net(bars[e.symbol], f, c, 3, COST) for c in rng.sample(cand, min(20, len(cand)))]
        pv = [x[0] for x in pl if x is not None]
        b = short_return(btc, e.day, 3)
        hedged = r3[1] - b - 2 * COST + r3[2] if b is not None else None
        rows.append((e, r1[0], r3[0], r3[0] - (STRESS - COST), r3[1], r3[2], statistics.fmean(pv) if pv else None, hedged))
    n = len(rows)
    dates = sorted({r[0].day for r in rows})
    cl = [r[0].day for r in rows]
    log(f"\n== OUTCOMES (main) n={n} k={len(dates)} ==")
    m3, m1 = cluster_boot([r[2] for r in rows], cl), cluster_boot([r[1] for r in rows], cl, seed=SEED + 2)
    hp = holm({"h1": m1[3], "h3": m3[3]})
    log(f"net h=3 (main): {fmt(m3)}; median {statistics.median(r[2] for r in rows) * 100:+.2f} %; Holm-p {hp['h3']:.4f}")
    log(f"net h=1: {fmt(m1)}; Holm-p {hp['h1']:.4f}")
    wk = [datetime.fromisoformat(r[0].day).isocalendar()[:2] for r in rows]
    log(f"net h=3, ISO-week clusters: {fmt(cluster_boot([r[2] for r in rows], wk))}")
    pr = [r for r in rows if r[6] is not None]
    log(f"diff vs placebo (n={len(pr)}): {fmt(cluster_boot([r[2] - r[6] for r in pr], [r[0].day for r in pr]))}; "
        f"placebo mean {statistics.fmean(r[6] for r in pr) * 100:+.2f} %")  # fmt: skip
    cut = dates[int(len(dates) * 0.75)] if dates else ""
    for lab, sub in (("IS first 75 % dates", [r for r in rows if r[0].day < cut]), ("OOS last 25 %", [r for r in rows if r[0].day >= cut])):
        log(f"{lab}: n={len(sub)} {fmt(cluster_boot([r[2] for r in sub], [r[0].day for r in sub]))}")
    log(
        f"components h=3: gross {statistics.fmean(r[4] for r in rows) * 100:+.2f} %, funding {statistics.fmean(r[5] for r in rows) * 100:+.3f} %, cost -0.41 %"
    )
    log(f"stress cost 1.01 %: {fmt(cluster_boot([r[3] for r in rows], cl))}")
    srt = sorted(rows, key=lambda r: -r[2])
    for kx in (1, 3):
        sub = srt[kx:]
        log(f"without top-{kx}: {fmt(cluster_boot([r[2] for r in sub], [r[0].day for r in sub]))}")
    for lab, lo, hi in (("2023-10..2025-03", "2023-10-01", "2025-03-31"), ("2025-04..2026-09", "2025-04-01", "2026-09-30")):
        sub = [r for r in rows if lo <= r[0].day <= hi]
        log(f"{lab}: n={len(sub)} {fmt(cluster_boot([r[2] for r in sub], [r[0].day for r in sub]))}")
    hb = [r for r in rows if r[7] is not None]
    log(f"BTC-hedged (short X, long BTC, 2 legs of costs): {fmt(cluster_boot([r[7] for r in hb], [r[0].day for r in hb]))}")
    # 5a: per token
    by_tok: dict[str, list[Any]] = {}
    for r in rows:
        by_tok.setdefault(r[0].symbol, []).append(r)
    log("per token (n >= 3): symbol n mean CI")
    ms, ses = [], []
    for s, rs in sorted(by_tok.items(), key=lambda kv: -len(kv[1])):
        if len(rs) < 3:
            continue
        v = [r[2] for r in rs]
        t = cluster_boot(v, [r[0].day for r in rs])
        log(f"   {s:16s} {len(rs):3d} {fmt(t)}")
        ms.append(statistics.fmean(v))
        ses.append(statistics.stdev(v) / len(v) ** 0.5)
    q = cochran_q(ms, ses)
    log(f"Cochran Q={q[0]:.2f} df={q[1]} I2={q[2]:.2f} p={q[3]:.4f}")
    is_r = [r for r in rows if r[0].day < cut]
    oos_r = [r for r in rows if r[0].day >= cut]
    is_tok: dict[str, list[float]] = {}
    for r in is_r:
        is_tok.setdefault(r[0].symbol, []).append(r[2])
    ranked = sorted((s for s, v in is_tok.items() if len(v) >= 2), key=lambda s: -statistics.fmean(is_tok[s]))
    top = set(ranked[: max(1, len(ranked) // 4)]) if ranked else set()
    a = [r[2] for r in oos_r if r[0].symbol in top]
    b_ = [r[2] for r in oos_r if r[0].symbol not in top]
    log(f"persistence: IS tokens with >= 2 events {len(ranked)}, top quarter {sorted(top)}; OOS top n={len(a)} "
        f"mean {statistics.fmean(a) * 100 if a else float('nan'):+.2f} % vs rest n={len(b_)} "
        f"mean {statistics.fmean(b_) * 100 if b_ else float('nan'):+.2f} %")  # fmt: skip
    firms: dict[str, list[Any]] = {}
    for r in rows:
        for fm in by_ev.get((r[0].symbol, r[0].day), {}):
            firms.setdefault(fm, []).append(r)
    for fm, rs in sorted(firms.items()):
        log(f"firm {fm:14s} n={len(rs)} {fmt(cluster_boot([r[2] for r in rs], [r[0].day for r in rs]))}")


def stage0(cache: Path, do_test: bool = False) -> None:
    w, snap, ex = wallets_all(cache)
    pend = [a[:10] for a in w if not (cache / TX / a / "done.json").exists()]
    log(f"tx4 crawl incomplete: {pend}" if pend else "tx4 crawl complete")
    ph1, ph2, _ = candidates(cache)
    fetched1 = sum((cache / RECV / f"{r}.json").exists() for r in ph1)
    fetched2 = sum((cache / RECV / f"{r}.json").exists() for r in ph2)
    log(f"phase 1: {fetched1}/{len(ph1)} fetched; phase 2: {fetched2}/{len(ph2)}")
    if fetched1 < len(ph1):
        log("phase 1 incomplete: stage0 is not run (registration)")
        return
    out_all, wd_all = load_out(cache, w)
    agg: dict[tuple[str, str], int] = {}
    for a in w:
        for p in (cache / "tx3" / a).glob("p_*.json"):
            for k, n in json.loads(p.read_text())["out_by_contract"].items():
                s, _, c = k.partition("|")
                agg[(s, c)] = agg.get((s, c), 0) + n
    keep, _ = pick_contracts_from_counts(agg)
    um = set(json.loads((base.DATA_DIR / "symbols.json").read_text()))
    bars: dict[str, dict[str, Any]] = {}

    def perp_of(sym: str) -> tuple[str | None, float]:
        for name, scale in ((f"{sym}USDT", 1.0), (f"1000{sym}USDT", 1e-3), (f"1000000{sym}USDT", 1e-6)):
            if name in um:
                return name, scale
        return None, 1.0

    def to_perp(flows: dict[tuple[str, str], float]) -> dict[tuple[str, str], float]:
        pf: dict[tuple[str, str], float] = {}
        for (sym, day), amt in flows.items():
            perp, scale = perp_of(sym)
            if perp is None:
                continue
            pf[(perp, day)] = pf.get((perp, day), 0.0) + amt * scale
            if perp not in bars:
                bars[perp] = base.load_bars(perp)
        return pf

    deps: dict[tuple[str, Any], dict[str, int | None]] = {}

    def dep_for(recv: tuple[str, ...], **kw: Any) -> dict[str, int | None]:
        key = (str(len(recv)), tuple(sorted(kw.items())))
        if key not in deps:
            deps[key], why = classify_all(cache, list(recv), w, ex, **kw)
            log(f"  classify {len(recv)} receivers {kw or 'registered'}: {dict(why)}")
        return deps[key]

    def run(label: str, wallets: set[str], dep: dict[str, int | None], *, pit: bool = False, net: bool = False,
            direct_only: bool = False, s: float = 0.02, u: float = 250_000, gate: bool = False) -> None:  # fmt: skip
        tr = [(f, t) for f, t in out_all if t.src in wallets]
        hit = exchange_bound((t for _, t in tr), ex, {} if direct_only else dep, point_in_time=pit)
        gross = daily_token_flows(hit, keep)
        flows = net_daily(gross, daily_token_flows((t for t in wd_all if t.dst in wallets), keep)) if net else gross
        pf = to_perp(flows)
        evs = find_events(pf, bars, min_share=s, min_usd=u, first_day=base.PERIOD[0], last_day=base.PERIOD[1])
        days = sorted({e.day for e in evs})
        n_two = sum(1 for t in hit if t.dst not in ex)
        log(f"[{label}] share>={s:.0%} usd>={u / 1e3:.0f}k: exchange-bound {len(hit)} (two-hop {n_two}), "
            f"token-days {len(flows)} ({len(pf)} with perp), events n={len(evs)}, k={len(days)}")  # fmt: skip
        if not gate:
            if do_test and gate_ok[0] and evs:
                brief(evs, bars)
            return
        firm_of = {a: v[0] for a, v in w.items()}
        by_ev: dict[tuple[str, str], Counter[str]] = {}
        for t in hit:
            perp, _ = perp_of(t.symbol)
            if perp and keep.get(t.symbol) == t.contract:
                by_ev.setdefault((perp, utc_day(t.ts)), Counter())[firm_of[t.src]] += 1
        log(f"  events by token: {dict(Counter(e.symbol for e in evs).most_common(25))}")
        log(f"  events by year: {dict(sorted(Counter(e.day[:4] for e in evs).items()))}")
        fc: Counter[str] = Counter()
        for e in evs:
            fc.update(by_ev.get((e.symbol, e.day), Counter()).keys())
        log(f"  events by firm (event has >=1 transfer of the firm): {dict(fc)}")
        for e in evs[:30]:
            log(
                f"   {e.day} {e.symbol:16s} usd={e.usd / 1e6:7.2f}M share={e.share:6.1%} transfers={dict(by_ev.get((e.symbol, e.day), {}))}"
            )
        rng = random.Random(SEED)
        ev_days: dict[str, list[str]] = {}
        for e in evs:
            ev_days.setdefault(e.symbol, []).append(e.day)
        plac: list[float] = []
        for e in evs:
            cand = [shift(e.day, -k) for k in range(7, 121)]
            cand = [c for c in cand if all(abs(base._d(c) - base._d(x)) > 7 for x in ev_days[e.symbol])]
            for c in rng.sample(cand, min(20, len(cand))):
                r = short_return(bars[e.symbol], c, 3)
                if r is not None:
                    plac.append(r)
        sd = statistics.pstdev(plac) if len(plac) > 1 else float("nan")
        m = mde(sd, len(days))
        ok = len(days) >= 30 and m <= 0.02
        log(f"  placebo 3d short returns n={len(plac)} SD={sd:.4f}; MDE=2.8*SD/sqrt(k)={m:.4f} (gate: k>=30, MDE<=0.020)")
        log(f"  GATE {'PASSED' if ok else 'FAILED'}")
        gate_ok[0] = ok
        if ok and do_test:
            outcomes(evs, bars, by_ev)

    gate_ok = [False]
    allw, snapw = set(w), set(snap)
    d1 = dep_for(tuple(ph1))
    log(f"deposit addresses (phase 1, registered rule): {len(d1)}; known point-in-time: {sum(v is not None for v in d1.values())}")
    run("MAIN: 16 wallets, phase 1, address rule, gross", allw, d1, gate=True)
    if do_test and not gate_ok[0]:
        log("gate failed: no event outcomes are computed (registration)")
    for s in (0.01, 0.02, 0.05):
        for u in (100_000, 250_000, 1_000_000):
            if (s, u) != (0.02, 250_000):
                run("grid", allw, d1, s=s, u=u)
    run("point-in-time deposit rule", allw, d1, pit=True)
    run("net of withdrawals", allw, d1, net=True)
    run("2023 snapshot wallets", snapw, d1)
    run("without Wintermute", {a for a in allw if w[a][0] != "Wintermute"}, d1)
    run("direct only (part B rule)", allw, d1, direct_only=True)
    for kw in ({"window": 6 * 3600}, {"window": 72 * 3600}, {"min_ex_share": 0.5}, {"min_ex_share": 0.95}):
        run(f"rule {kw}", allw, dep_for(tuple(ph1), **kw))
    if fetched2:
        run(f"phase 1 + phase 2 ({fetched2} of {len(ph2)} fetched)", allw, dep_for(tuple(ph1 + ph2)))
    log(f"rss={base.rss_mb():.0f} MB")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["prereg", "crawl", "receivers", "stage0", "test"])
    ap.add_argument("--phase", type=int, default=1)
    ap.add_argument("--limit", type=int, default=BUDGET)
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    a = ap.parse_args()
    cache = a.cache_dir
    if a.stage == "prereg":
        prereg()
        return
    if a.stage in ("stage0", "test"):
        stage0(cache, do_test=a.stage == "test")
        return
    if not REG_FILE.exists():
        sys.exit("registration file missing: run the prereg stage first")
    secrets = base.keys()
    if not secrets.get("ETHERSCAN_API_KEY"):
        sys.exit("ETHERSCAN_API_KEY not found in ~/.config/tradedesk/onchain.env")
    if a.stage == "crawl":
        asyncio.run(crawl(cache, secrets))
    else:
        asyncio.run(receivers(cache, secrets, a.phase, a.limit))


if __name__ == "__main__":
    main()
