"""Minimal periodic-job scheduler.

Each :class:`PeriodicJob` wraps an async callable. :class:`Scheduler.start`
spawns one task per job; :class:`Scheduler.stop` cancels them cleanly. The
runtime holds the scheduler so :func:`system_health` can read the metrics.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from .bad_rows import count_bad_rows

if TYPE_CHECKING:  # avoid import cycle
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

JobCallable = Callable[["Runtime"], Awaitable[int]]


@dataclass
class JobMetrics:
    name: str
    period_seconds: int
    runs: int = 0
    successes: int = 0
    failures: int = 0
    last_run_ts: float | None = None
    last_duration_ms: float | None = None
    last_rows_written: int | None = None
    last_error: str | None = None  # the latest run's error; cleared by the next success
    # The latest failure, kept across later successes: without it a job that fails 7 times and then succeeds once
    # shows failures=7 and no reason (liquidations:bybit / :okx, 2026-11-02).
    last_failure: str | None = None
    last_failure_ts: float | None = None
    # Provider rows / fields the job dropped (or stored as NULL) as malformed (collector/bad_rows.py): the latest run,
    # failed runs included, and the sum since the process started. None before the first run.
    last_rows_dropped: int | None = None
    rows_dropped_total: int = 0
    # WebSocket jobs only (liquidations:*): connection counters that survive reconnects inside a run
    # (providers.liquidations.StreamStats); None for the other jobs.
    stream: Any | None = None


@dataclass
class PeriodicJob:
    name: str
    period_seconds: int
    fn: JobCallable
    metrics: JobMetrics = field(init=False)

    def __post_init__(self) -> None:
        self.metrics = JobMetrics(name=self.name, period_seconds=self.period_seconds)


class Scheduler:
    def __init__(self, rt: "Runtime", jobs: list[PeriodicJob]) -> None:
        self.rt = rt
        self.jobs = jobs
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()

    def metrics(self) -> list[JobMetrics]:
        return [j.metrics for j in self.jobs]

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        for j in self.jobs:
            self._tasks.append(asyncio.create_task(self._run_job(j), name=f"collector:{j.name}"))

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []

    async def _run_job(self, job: PeriodicJob) -> None:
        while not self._stop.is_set():
            start = time.perf_counter()
            with count_bad_rows() as dropped:  # a fresh count of malformed provider rows per run (bad_rows.py)
                try:
                    written = await job.fn(self.rt)
                    job.metrics.successes += 1
                    job.metrics.last_rows_written = written
                    job.metrics.last_error = None
                except asyncio.CancelledError as e:
                    task = asyncio.current_task()
                    if self._stop.is_set() or (task is not None and task.cancelling()):
                        raise
                    # A CancelledError from inside the job, not a cancel of this task: ending the loop here would stop
                    # the job for good with no trace, so record it as a failed run and keep going.
                    job.metrics.failures += 1
                    job.metrics.last_error = f"CancelledError: {e}" if str(e) else "CancelledError"
                    job.metrics.last_failure, job.metrics.last_failure_ts = job.metrics.last_error, time.time()
                    log.warning("collector job %s failed: %s", job.name, job.metrics.last_error, extra={"job": job.name})
                except Exception as e:  # noqa: BLE001
                    job.metrics.failures += 1
                    job.metrics.last_error = f"{type(e).__name__}: {e}"
                    job.metrics.last_failure, job.metrics.last_failure_ts = job.metrics.last_error, time.time()
                    log.warning("collector job %s failed: %s", job.name, e, extra={"job": job.name, "error": job.metrics.last_error})
                finally:
                    job.metrics.runs += 1
                    job.metrics.last_duration_ms = (time.perf_counter() - start) * 1000.0
                    job.metrics.last_run_ts = time.time()
                    # A failed run keeps what it dropped before failing: those rows were still lost.
                    job.metrics.last_rows_dropped = dropped.n
                    job.metrics.rows_dropped_total += dropped.n
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=job.period_seconds)
                return  # stop was set
            except TimeoutError:
                continue


# ---------- default job set ----------


async def _collect_atm_term(rt: "Runtime") -> int:
    """Collect ATM-IV term structure (per currency × expiry) into `atm_term`."""
    from ..analytics.options import atm_iv, build_chain, build_smile, group_by_expiry, rr_fly  # noqa: PLC0415

    assert rt.store is not None  # scheduler is only built when the store exists
    now = rt.now()
    ts = int(now.timestamp())
    total = 0
    for currency in ("BTC", "ETH"):
        raw = await rt.deribit.book_summary(currency, "option")
        chain = build_chain(raw.data, now)
        rows_out: list[dict[str, object]] = []
        for exp, rs in group_by_expiry(chain).items():
            smile = build_smile(rs)
            if smile is None:
                continue
            rf25 = rr_fly(smile, 0.25)
            rf10 = rr_fly(smile, 0.10)
            rows_out.append(
                {
                    "expiry": exp.isoformat(),
                    "days": (exp - now).total_seconds() / 86400.0,
                    "forward": smile.forward,
                    "atm_iv": atm_iv(smile),
                    "rr25": rf25.get("rr"),
                    "fly25": rf25.get("fly"),
                    "rr10": rf10.get("rr"),
                    "fly10": rf10.get("fly"),
                }
            )
        total += await rt.store.insert_atm_term(ts=ts, currency=currency, rows=rows_out)
    return total


async def _collect_funding(rt: "Runtime") -> int:
    """Collect current cross-venue funding snapshots for BTC/ETH into `funding_hist`."""
    from ..analytics.derivatives import annualize_funding, parse_interval_hours  # noqa: PLC0415
    from ..providers.exchanges import PERP_EXCHANGES  # noqa: PLC0415

    assert rt.store is not None
    ts = int(rt.now().timestamp())
    total = 0
    for base in ("BTC", "ETH"):
        symbol = f"{base}/USDT:USDT"
        rows: list[dict[str, object]] = []
        for ex in PERP_EXCHANGES:
            try:
                fr = (await rt.exchanges.funding_rate(ex, symbol)).data
            except Exception:  # noqa: BLE001 - a single venue failing is fine
                continue
            rate = fr.get("fundingRate")
            if rate is None:
                continue
            hours = parse_interval_hours(fr.get("interval"))
            rows.append(
                {
                    "venue": ex,
                    "base": base,
                    "rate_per_interval": float(rate),
                    "interval_hours": hours,
                    "annualized": annualize_funding(float(rate), hours),
                }
            )
        try:
            ctx = (await rt.hyperliquid.asset_contexts()).data or []
            row = next((c for c in ctx if c["coin"] == base), None)
            if row and row.get("funding_1h") is not None:
                rows.append(
                    {
                        "venue": "hyperliquid",
                        "base": base,
                        "rate_per_interval": float(row["funding_1h"]),
                        "interval_hours": 1.0,
                        "annualized": float(row.get("funding_annualized") or annualize_funding(float(row["funding_1h"]), 1.0)),
                    }
                )
        except Exception:  # noqa: BLE001
            pass
        if base in ("BTC", "ETH"):
            try:
                fut = (await rt.deribit.book_summary(base, "future")).data or []
                perp = next((x for x in fut if x.get("instrument_name", "").endswith("PERPETUAL")), None)
                if perp and perp.get("funding_8h") is not None:
                    rate = float(perp["funding_8h"])
                    rows.append(
                        {
                            "venue": "deribit",
                            "base": base,
                            "rate_per_interval": rate,
                            "interval_hours": 8.0,
                            "annualized": annualize_funding(rate, 8.0),
                        }
                    )
            except Exception:  # noqa: BLE001
                pass
        total += await rt.store.insert_funding(ts=ts, rows=rows)
    return total


async def _collect_basis(rt: "Runtime") -> int:
    """Collect Deribit dated-futures basis snapshot (per currency × expiry) into `basis_hist`."""
    from ..analytics.derivatives import basis as basis_calc  # noqa: PLC0415
    from ..analytics.instruments import parse_future_name  # noqa: PLC0415

    assert rt.store is not None
    now = rt.now()
    ts = int(now.timestamp())
    total = 0
    for currency in ("BTC", "ETH"):
        try:
            fut = (await rt.deribit.book_summary(currency, "future")).data or []
        except Exception:  # noqa: BLE001
            continue
        rows: list[dict[str, object]] = []
        for f in fut:
            parsed = parse_future_name(f.get("instrument_name", ""))
            if parsed is None or parsed[1] is None:
                continue  # skip perpetual
            mark, idx = f.get("mark_price"), f.get("estimated_delivery_price")
            if not mark or not idx:
                continue
            bs = basis_calc(float(mark), float(idx), parsed[1], now)
            rows.append(
                {
                    "expiry": parsed[1].isoformat(),
                    "days": bs["days_to_expiry"],
                    "basis_pct": bs["basis_pct"],
                    "annualized_simple": bs.get("annualized_simple"),
                }
            )
        total += await rt.store.insert_basis(ts=ts, currency=currency, rows=rows)
    return total


async def _consume_liquidation_stream(rt: "Runtime", stream) -> int:
    """Drain an async-iterable of liquidation rows into the store, batching by 100."""
    assert rt.store is not None
    written = 0
    buffer: list[dict] = []
    try:
        async for row in stream:
            buffer.append(row)
            if len(buffer) >= 100:
                written += await rt.store.insert_liquidations(buffer)
                buffer.clear()
    finally:
        if buffer:
            written += await rt.store.insert_liquidations(buffer)
    return written


async def _collect_liquidations_binance(rt: "Runtime", stats: Any = None) -> int:
    """Open the Binance !forceOrder@arr stream for a bounded window (reconnecting inside it; ``stats``: StreamStats)."""
    from ..providers.liquidations import binance_liquidation_stream  # noqa: PLC0415

    s = rt.settings
    return await _consume_liquidation_stream(
        rt, binance_liquidation_stream(max_seconds=float(s.collect_liquidations_window_seconds), stats=stats)
    )


async def _collect_liquidations_bybit(rt: "Runtime", stats: Any = None) -> int:
    """Subscribe to Bybit v5 ``allLiquidation.<symbol>`` for the configured symbols."""
    from ..providers.liquidations import bybit_liquidation_stream  # noqa: PLC0415

    s = rt.settings
    symbols = [sym.strip().upper() for sym in s.collect_liquidations_bybit_symbols.split(",") if sym.strip()]
    if not symbols:
        return 0
    return await _consume_liquidation_stream(
        rt, bybit_liquidation_stream(symbols, max_seconds=float(s.collect_liquidations_window_seconds), stats=stats)
    )


async def _collect_liquidations_okx(rt: "Runtime", stats: Any = None) -> int:
    """Subscribe to OKX v5 ``liquidation-orders`` for one instType.

    OKX sizes are contracts, so the instType's contract values are read first (``public/instruments``, cached 1 h);
    without them the run fails instead of storing wrong sizes."""
    from ..core.errors import ProviderError  # noqa: PLC0415
    from ..providers.liquidations import okx_contract_specs, okx_liquidation_stream  # noqa: PLC0415

    s = rt.settings
    inst_type = s.collect_liquidations_okx_inst_type.strip().upper()
    raw = (await rt.exchanges.public_get("okx", "publicGetPublicInstruments", {"instType": inst_type}, ttl=3600)).data
    data = raw.get("data") if isinstance(raw, dict) and str(raw.get("code")) == "0" else None
    specs = okx_contract_specs(data if isinstance(data, list) else [])
    if not specs:
        raise ProviderError("ccxt:okx", "data_unavailable", f"no contract values for instType {inst_type}")
    return await _consume_liquidation_stream(
        rt, okx_liquidation_stream(specs, inst_type, max_seconds=float(s.collect_liquidations_window_seconds), stats=stats)
    )


async def _health_alerts(rt: "Runtime", now_ts: int) -> list:
    """Provider-health transitions since the last tick (alerts/health.py). The announced states live on the runtime;
    after a restart they are loaded from the stored alerts, so a provider still down is not announced again."""
    from ..alerts.health import RULE_PREFIX, evaluate_health, state_from_alerts  # noqa: PLC0415

    assert rt.store is not None
    s = rt.settings
    if rt.health_watch is None:
        rt.health_watch = state_from_alerts(await rt.store.last_alerts_with_prefix(RULE_PREFIX))
    rt.health_watch, fired = evaluate_health(
        rt.health.snapshot()["providers"],
        rt.health_watch,
        now_ts=now_ts,
        min_failures=s.alerts_health_min_failures,
        quiet_seconds=s.alerts_health_cooldown,
        confirm_seconds=s.alerts_health_confirm,
    )
    return fired


async def _evaluate_alerts(rt: "Runtime") -> int:
    """Load rules, evaluate against the store, deliver via channels, record outcome."""
    from ..alerts import build_channels, evaluate_rules, load_rules  # noqa: PLC0415
    from ..alerts.engine import evaluate_dsl_rules  # noqa: PLC0415

    assert rt.store is not None
    s = rt.settings
    now_ts = int(rt.now().timestamp())
    fired: list = []
    # JSON (Phase 2.5) rules — optional file path.
    if s.alerts_rules_path:
        rules = load_rules(s.alerts_rules_path, default_cooldown=s.alerts_default_cooldown)
        fired += await evaluate_rules(rules, rt.store, now_ts=now_ts)
    # DB-backed DSL (Phase 3.1) rules — always consulted when the job runs.
    fired += await evaluate_dsl_rules(rt.store, now_ts=now_ts)
    health: list = []
    if s.alerts_health_enabled:
        try:
            health = await _health_alerts(rt, now_ts)
        except Exception as e:  # noqa: BLE001 — a health-alert failure (e.g. the DB read) must not drop the other alerts
            log.warning("provider-health alerts skipped this tick: %s", e)
    if not fired and not health:
        return 0
    channels = build_channels(s)
    delivered_rows = 0
    # Provider health: one row per provider in every channel's log, but the non-log channels get one digest per tick
    # (alerts/health.py, digest): a network blip used to send one Telegram message per provider, down and up.
    from ..alerts.health import digest  # noqa: PLC0415

    summary = digest(health) if health else None
    sent: dict[str, tuple[bool, str | None]] = {}
    for ch in channels:
        if ch.name != "log" and summary is not None:
            sent[ch.name] = await ch.send(summary)
    for alert in health:
        delivered_any = False
        for ch in channels:
            if ch.name == "log":
                ok, err = await ch.send(alert)
            elif ch.name in sent:
                ok, err = sent[ch.name]
            else:
                continue  # left out of the digest (a short outage's recovery): logged only
            await rt.store.insert_alert(
                rule_id=alert.rule_id,
                ts=alert.ts,
                payload=alert.payload() | {"channel": ch.name},
                channel=ch.name,
                delivered=ok,
                delivery_error=err,
            )
            delivered_any = delivered_any or ok
        if delivered_any:
            delivered_rows += 1
    for alert in fired:
        delivered_any = False
        last_err: str | None = None
        for ch in channels:
            ok, err = await ch.send(alert)
            payload = alert.payload() | {"channel": ch.name}
            await rt.store.insert_alert(
                rule_id=alert.rule_id,
                ts=alert.ts,
                payload=payload,
                channel=ch.name,
                delivered=ok,
                delivery_error=err,
            )
            delivered_any = delivered_any or ok
            if err and not ok:
                last_err = err
        if delivered_any:
            delivered_rows += 1
        else:
            log.warning("alert %s not delivered through any channel: %s", alert.rule_id, last_err)
    # Close telegram client http if present.
    for ch in channels:
        aclose = getattr(getattr(ch, "_client", None), "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                pass
    return delivered_rows


def _parse_ohlcv_markets(raw: str) -> list[tuple[str, str, str]]:
    """``venue:symbol:timeframe`` entries separated by commas. Whitespace tolerated."""
    out: list[tuple[str, str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":")
        if len(bits) < 3:
            continue
        venue = bits[0].strip()
        timeframe = bits[-1].strip()
        symbol = ":".join(bits[1:-1]).strip()  # ccxt symbols may contain ':' (e.g. BTC/USDT:USDT)
        if venue and symbol and timeframe:
            out.append((venue, symbol, timeframe))
    return out


async def _collect_ohlcv_hist(rt: "Runtime") -> int:
    """Backfill + incremental pull for every (venue, symbol, timeframe) in settings.

    Each tick advances every market by up to 10 batches of 500 bars; the job
    runs often enough (default 1 h) that long backfills complete over several
    ticks without blocking the scheduler.
    """
    from ..tools._util import timeframe_seconds  # noqa: PLC0415

    assert rt.store is not None
    s = rt.settings
    markets = _parse_ohlcv_markets(s.collect_ohlcv_hist_markets)
    if not markets:
        return 0
    now_ms = int(rt.now().timestamp() * 1000)
    written = 0
    MAX_BATCHES = 10
    BATCH_SIZE = 500
    for venue, symbol, timeframe in markets:
        try:
            tf_sec = timeframe_seconds(timeframe)
        except ValueError:  # one bad entry in TRADEDESK_COLLECT_OHLCV_HIST_MARKETS must not fail the whole job
            log.warning("ohlcv_hist: skipping %s:%s, unknown timeframe %r", venue, symbol, timeframe)
            continue
        step_ms = tf_sec * 1000
        last = await rt.store.ohlcv_last_ts(venue, symbol, timeframe)
        since_ms = (last + step_ms) if last is not None else (now_ms - s.collect_ohlcv_hist_backfill_days * 86_400_000)
        for _ in range(MAX_BATCHES):
            if since_ms >= now_ms:
                break
            try:
                fetched = await rt.exchanges.ohlcv(venue, symbol, timeframe, BATCH_SIZE, since_ms=since_ms)
                bars = fetched.data or []
            except Exception as e:  # noqa: BLE001
                log.warning("ohlcv_hist %s %s %s: fetch failed: %s", venue, symbol, timeframe, e)
                break
            # Keep only rows at or after `since_ms` in case the venue returns an earlier bar.
            fresh = [b for b in bars if isinstance(b, (list, tuple)) and len(b) >= 6 and int(b[0]) >= since_ms]
            if not fresh:
                break
            inserted = await rt.store.insert_ohlcv_batch(venue, symbol, timeframe, fresh)
            written += inserted
            since_ms = int(fresh[-1][0]) + step_ms
            if inserted < BATCH_SIZE:
                break
    return written


async def _collect_supply(rt: "Runtime") -> int:
    """Daily point-in-time supply snapshot of the top coins by market cap into `supply_snapshot`, plus the extra ids
    (perp coins outside the top pages, `_supply_extra_ids`) and the `/global` totals into `global_snapshot`.

    CoinGecko's history is corrected retroactively and its supply history is Enterprise-only, so research that needs
    supply as it was known on a day (unlocks, net issuance, buybacks) can only rely on snapshots taken at the time.
    A failed later page keeps the pages already fetched; a failed first page fails the job. The extra ids and the
    global totals are best effort: a failure there is logged and the top pages are still written.
    """
    from ..core.errors import ProviderError  # noqa: PLC0415

    assert rt.store is not None
    now = rt.now()
    day, ts = now.strftime("%Y-%m-%d"), int(now.timestamp())
    rows: list[dict[str, object]] = []
    for page in range(1, max(1, rt.settings.collect_supply_pages) + 1):
        try:
            fetched = await rt.coingecko.coins_markets("usd", 250, page)
        except ProviderError as e:
            if not rows:
                raise
            log.warning("supply snapshot: page %d failed, keeping %d rows: %s", page, len(rows), e)
            break
        if not fetched.data:
            break
        rows.extend(fetched.data)
    seen = {r.get("id") for r in rows if isinstance(r, dict)}
    extra = [i for i in _supply_extra_ids(rt.settings.collect_supply_extra_ids_path) if i not in seen]
    for k in range(0, len(extra), 250):
        try:
            got = await rt.coingecko.coins_markets_ids("usd", extra[k : k + 250])
        except ProviderError as e:
            log.warning("supply snapshot: extra ids %d-%d failed: %s", k, k + 250, e)
            continue
        rows.extend(r for r in got.data if isinstance(r, dict) and r.get("id") not in seen)
    written = await rt.store.insert_supply_snapshot(day, ts, rows)
    try:
        g = await rt.coingecko.global_market()
        written += await rt.store.insert_global_snapshot(day, ts, g.data)
    except ProviderError as e:
        log.warning("supply snapshot: /global failed, totals not recorded today: %s", e)
    return written


def _supply_extra_ids(path: str) -> list[str]:
    """CoinGecko ids from a text file (one per line, ``#`` comments); "" = the repo's deploy/coingecko_extra_ids.txt.
    A missing or unreadable file gives no extra ids."""
    from pathlib import Path  # noqa: PLC0415

    p = Path(path) if path else Path(__file__).resolve().parents[3] / "deploy" / "coingecko_extra_ids.txt"
    try:
        text = p.read_text()
    except OSError:
        return []
    out: list[str] = []
    for line in text.splitlines():
        i = line.split("#", 1)[0].strip()
        if i and i not in out:
            out.append(i)
    return out


def _num(v: object) -> float | None:
    try:
        return None if v is None or isinstance(v, bool) else float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


async def _pm_leaderboard(rt: "Runtime", category: str, pages: int) -> list[dict[str, object]]:
    """Polymarket leaderboard rows for one category, merged by wallet over the DAY / WEEK / MONTH / ALL rankings
    (data-api v2: up to ``pages`` cursor pages of 50 per ranking; `user_id` is the proxy wallet v1 called
    `proxyWallet`, so accounts continue the rows written before the v1 shutdown of 2026-11-02)."""
    merged: dict[str, dict[str, object]] = {}
    for period, key in (("MONTH", "pnl_month"), ("WEEK", "pnl_week"), ("DAY", "pnl_day"), ("ALL", "pnl_all")):
        cursor: str | None = None
        for _ in range(max(1, pages)):
            page = (await rt.polymarket.leaderboard(period=period, category=category, limit=50, cursor=cursor)).data
            for r in page["rows"]:
                wallet = str(r.get("user_id") or "").lower() if isinstance(r, dict) else ""
                if not wallet:
                    continue
                m = merged.setdefault(wallet, {"account": wallet, "name": r.get("user_name") or None})
                m[key] = _num(r.get("pnl"))
                if period == "MONTH":
                    m["vlm_month"] = _num(r.get("volume"))
            cursor = page["next_cursor"]
            if not cursor:  # v2: a short page does not end the walk, only a missing cursor does
                break
    return list(merged.values())


# An account that drops below `traders_min_account_usd` stays in the Hyperliquid snapshot this many days after it was
# last stored above it: without them a forward "does past PnL persist" test sees only survivors (smart-money report
# 2026-10-17, "Що далі" item 2).
TRADERS_KEEP_DAYS = 60


async def _collect_traders(rt: "Runtime") -> int:
    """Daily snapshot of strong traders' public results into `trader_snapshot`: every Hyperliquid account worth at
    least `traders_min_account_usd`, plus those worth that much on any stored day of the last `TRADERS_KEEP_DAYS`
    (so the forward test of PnL persistence keeps the accounts that lost money), and the Polymarket leaderboards
    (overall and crypto). One venue failing does not stop the others; the job fails only if nothing was written."""
    from datetime import timedelta  # noqa: PLC0415

    from ..core.errors import ProviderError  # noqa: PLC0415
    from ..providers.hyperliquid import parse_leaderboard  # noqa: PLC0415

    assert rt.store is not None
    s = rt.settings
    now = rt.now()
    day, ts = now.strftime("%Y-%m-%d"), int(now.timestamp())
    written, errors = 0, []
    since = (now - timedelta(days=TRADERS_KEEP_DAYS)).strftime("%Y-%m-%d")
    keep = await rt.store.trader_accounts_since("hyperliquid", since, s.traders_min_account_usd)
    try:
        rows = parse_leaderboard((await rt.hyperliquid.leaderboard()).data, s.traders_min_account_usd, keep)
        written += await rt.store.insert_trader_snapshot(day, ts, "hyperliquid", rows)
    except ProviderError as e:
        errors.append(f"hyperliquid: {e}")
    for venue, category in (("polymarket", "OVERALL"), ("polymarket_crypto", "CRYPTO")):
        try:
            written += await rt.store.insert_trader_snapshot(day, ts, venue, await _pm_leaderboard(rt, category, s.traders_pm_pages))
        except ProviderError as e:
            errors.append(f"{venue}: {e}")
    if errors:
        if not written:
            raise RuntimeError("; ".join(errors))
        log.warning("traders snapshot partly failed: %s", "; ".join(errors))
    return written


async def _collect_trader_positions(rt: "Runtime") -> int:
    """Positions of the watched traders (top by month and all-time PnL in the latest snapshot) into `trader_positions`
    as a change log (baseline, then opened / changed / closed positions). Polymarket: the 100 largest open positions
    worth >= $100."""
    from ..core.errors import ProviderError  # noqa: PLC0415
    from ..providers.hyperliquid import parse_positions  # noqa: PLC0415

    assert rt.store is not None
    s = rt.settings
    ts = int(rt.now().timestamp())
    written = failed = tried = 0
    for account in await rt.store.tracked_traders("hyperliquid", s.traders_top_n, s.traders_min_account_usd):
        tried += 1
        try:
            positions = parse_positions((await rt.hyperliquid.clearinghouse_state(account)).data)
        except ProviderError as e:
            failed += 1
            log.debug("positions hyperliquid %s: %s", account, e)
            continue
        written += await rt.store.record_trader_positions(ts, "hyperliquid", account, positions)
    pm_accounts: list[str] = []
    for venue in ("polymarket", "polymarket_crypto"):
        pm_accounts += [a for a in await rt.store.tracked_traders(venue, s.traders_top_n) if a not in pm_accounts]
    for account in pm_accounts:
        tried += 1
        try:
            raw = (await rt.polymarket.positions(account, limit=100, min_value_usd=100.0)).data  # the 100 largest open
        except ProviderError as e:
            failed += 1
            log.debug("positions polymarket %s: %s", account, e)
            continue
        positions = [  # data-api v2 fields; `market` is the outcome token id, as v1's `asset` was
            {
                "market": str(r.get("token_id")),
                "side": str(r.get("outcome") or ""),
                "size": _num(r.get("current_size")),
                "entry_px": _num(r.get("avg_price")),
                "value_usd": _num(r.get("current_value")),
                "upnl": _num(r.get("unrealized_pnl")),
                "title": r.get("title"),
            }
            for r in raw
            if isinstance(r, dict)
            and r.get("token_id")
            and not r.get("redeemable")
            and str(r.get("status") or "OPEN") == "OPEN"
            and (_num(r.get("current_value")) or 0) >= 100
        ]
        written += await rt.store.record_trader_positions(ts, "polymarket", account, positions)
    if tried and failed == tried:
        raise RuntimeError(f"positions: all {tried} requests failed")
    return written


async def _paper_carry(rt: "Runtime") -> int:
    """Run the due forward-paper cycle of each carry strategy (no orders: bookkeeping and public market data only).
    A's new entries are stopped unless ``paper_carry_a_entries_enabled`` (kill criterion (b), 2026-11-11)."""
    from .carry_paper_engine import PARAMS, entry_stop_reason, run_due  # noqa: PLC0415
    from .carry_paper_feed import LiveFeed  # noqa: PLC0415

    assert rt.store is not None
    feed = LiveFeed(rt)
    done = 0

    def clock() -> int:
        return int(rt.now().timestamp() * 1000)

    for params in PARAMS.values():
        stopped = entry_stop_reason(params.strategy, a_entries_enabled=rt.settings.paper_carry_a_entries_enabled)
        out = await run_due(rt.store, feed, params, clock=clock, entries_stopped=stopped)
        done += out.get("status") == "done"
        if out.get("status") == "failed":
            log.warning("paper carry %s cycle %s failed: %s", params.strategy, out.get("cycle_ts"), out.get("error"))
    return done


async def _retention_sweep(rt: "Runtime") -> int:
    """Trim stored history to the retention windows configured in Settings."""
    assert rt.store is not None
    s = rt.settings
    now_ts = int(rt.now().timestamp())
    deleted = 0
    pairs = (
        ("atm_term", s.retention_atm_term_days),
        ("funding_hist", s.retention_funding_days),
        ("basis_hist", s.retention_basis_days),
        ("ohlcv_hist", s.retention_ohlcv_hist_days),
        ("option_xvenue_snapshot", s.retention_option_xvenue_days),
        ("coinalyze_history", s.retention_coinalyze_history_days),
        ("tradfi_spread", s.retention_tradfi_spread_days),
    )
    for table, days in pairs:
        if days <= 0:
            continue
        # ohlcv_hist stores ts in milliseconds; everything else is seconds.
        cutoff = (now_ts - days * 86_400) * (1000 if table == "ohlcv_hist" else 1)
        deleted += await rt.store.prune(table, cutoff)
    return deleted


def build_default_scheduler(rt: "Runtime") -> Scheduler | None:
    """Return a scheduler with the default jobs, or None when disabled or no store."""
    s = rt.settings
    if not s.collector_enabled or rt.store is None:
        return None
    jobs = [
        PeriodicJob(name="atm_term", period_seconds=s.collect_atm_term_seconds, fn=_collect_atm_term),
        PeriodicJob(name="funding", period_seconds=s.collect_funding_seconds, fn=_collect_funding),
        PeriodicJob(name="basis", period_seconds=s.collect_basis_seconds, fn=_collect_basis),
        PeriodicJob(name="retention", period_seconds=s.retention_seconds, fn=_retention_sweep),
    ]
    if s.collect_traders_enabled:
        jobs.append(PeriodicJob(name="traders", period_seconds=s.collect_traders_seconds, fn=_collect_traders))
        jobs.append(PeriodicJob(name="trader_positions", period_seconds=s.collect_traders_positions_seconds, fn=_collect_trader_positions))
    if s.collect_supply_enabled:
        jobs.append(PeriodicJob(name="supply", period_seconds=s.collect_supply_seconds, fn=_collect_supply))
    if s.paper_carry_enabled:
        jobs.append(PeriodicJob(name="paper_carry", period_seconds=s.paper_carry_seconds, fn=_paper_carry))
    if s.assistant_snapshot_enabled:
        from .assistant_snapshot import collect_assistant_snapshot  # noqa: PLC0415

        jobs.append(PeriodicJob(name="assistant_snapshot", period_seconds=s.assistant_snapshot_seconds, fn=collect_assistant_snapshot))
    if s.pm_gap_enabled:
        from .pm_gap import collect_pm_gap  # noqa: PLC0415

        jobs.append(PeriodicJob(name="pm_gap", period_seconds=s.pm_gap_seconds, fn=collect_pm_gap))
    if s.assistant_paper_enabled:
        from .assistant_paper import collect_assistant_paper  # noqa: PLC0415

        jobs.append(PeriodicJob(name="assistant_paper", period_seconds=s.assistant_paper_seconds, fn=collect_assistant_paper))
    if s.plan_resolve_enabled:
        from .trade_plan import collect_plan_resolve  # noqa: PLC0415

        jobs.append(PeriodicJob(name="plan_resolve", period_seconds=s.plan_resolve_seconds, fn=collect_plan_resolve))
    if s.forecast_log_enabled:
        from .forecast_log import collect_forecast_log  # noqa: PLC0415

        jobs.append(PeriodicJob(name="forecast_log", period_seconds=s.forecast_log_seconds, fn=collect_forecast_log))
    if s.collect_radar_enabled:
        from .radar_archive import collect_radar_archive  # noqa: PLC0415

        jobs.append(PeriodicJob(name="radar_archive", period_seconds=s.collect_radar_seconds, fn=collect_radar_archive))
    if s.collect_option_xvenue_enabled:
        from .option_xvenue import MINUTE_TICK_SECONDS, OptionXvenueJob  # noqa: PLC0415

        # Ticks every minute; the job itself keeps the hourly cadence and opens minute windows after a crossing.
        xvenue = OptionXvenueJob(hourly_seconds=s.collect_option_xvenue_seconds)
        jobs.append(PeriodicJob(name="option_xvenue", period_seconds=MINUTE_TICK_SECONDS, fn=xvenue))
    if s.collect_coinalyze_history_enabled:
        from .coinalyze_history import CoinalyzeHistoryJob  # noqa: PLC0415

        jobs.append(PeriodicJob(name="coinalyze_history", period_seconds=s.collect_coinalyze_history_seconds, fn=CoinalyzeHistoryJob()))
    if s.collect_tradfi_spread_enabled:
        from .tradfi_spread import TradfiSpreadJob  # noqa: PLC0415

        jobs.append(PeriodicJob(name="tradfi_spread", period_seconds=s.collect_tradfi_spread_seconds, fn=TradfiSpreadJob()))
    if s.collect_hl_oi_cap_enabled:
        from .hl_oi_cap import collect_hl_oi_cap  # noqa: PLC0415

        jobs.append(PeriodicJob(name="hl_oi_cap", period_seconds=s.collect_hl_oi_cap_seconds, fn=collect_hl_oi_cap))
    if s.collect_flow_snapshot_enabled:
        from .flow_snapshot import collect_flow_snapshot  # noqa: PLC0415

        jobs.append(PeriodicJob(name="flow_snapshot", period_seconds=s.collect_flow_snapshot_seconds, fn=collect_flow_snapshot))
    if s.collect_cycle_regime_enabled:
        from .cycle_regime_log import collect_cycle_regime_log  # noqa: PLC0415

        jobs.append(PeriodicJob(name="cycle_regime_log", period_seconds=s.collect_cycle_regime_seconds, fn=collect_cycle_regime_log))
    if s.collect_coingecko_trending_enabled:
        from .coingecko_trending import collect_coingecko_trending  # noqa: PLC0415

        jobs.append(
            PeriodicJob(name="coingecko_trending", period_seconds=s.collect_coingecko_trending_seconds, fn=collect_coingecko_trending)
        )
    if s.collect_option_legs_enabled:
        from .option_legs import collect_option_legs  # noqa: PLC0415

        jobs.append(PeriodicJob(name="option_legs", period_seconds=s.collect_option_legs_seconds, fn=collect_option_legs))
    if s.alerts_enabled:
        # JSON file is optional in Phase 3.1 — DB-backed rules work on their own.
        jobs.append(PeriodicJob(name="alerts", period_seconds=s.alerts_seconds, fn=_evaluate_alerts))
    if s.collect_ohlcv_hist_enabled and s.collect_ohlcv_hist_markets.strip():
        jobs.append(
            PeriodicJob(
                name="ohlcv_hist",
                period_seconds=s.collect_ohlcv_hist_seconds,
                fn=_collect_ohlcv_hist,
            )
        )
    if s.collect_liquidations_enabled:
        from ..providers.liquidations import StreamStats  # noqa: PLC0415

        venues = {v.strip().lower() for v in s.collect_liquidations_venues.split(",") if v.strip()}
        fns = {
            "binance": _collect_liquidations_binance,
            "bybit": _collect_liquidations_bybit,
            "okx": _collect_liquidations_okx,
        }
        for venue in ("binance", "bybit", "okx"):
            if venue not in venues:
                continue
            # One per job: the reconnect backoff and counters carry over between runs.
            stats = StreamStats(clock=lambda: rt.now().timestamp())
            job = PeriodicJob(
                name=f"liquidations:{venue}",
                period_seconds=s.collect_liquidations_restart_seconds,
                fn=partial(fns[venue], stats=stats),
            )
            job.metrics.stream = stats
            jobs.append(job)
    return Scheduler(rt, jobs)
