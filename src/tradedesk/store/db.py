"""aiosqlite-backed store with PRAGMA user_version migrations.

Thread-safety: aiosqlite serialises calls through one background thread per
connection, so a single ``Store`` instance can be shared across async tasks.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiosqlite

from .schema import MIGRATIONS


def _parse_journal_row(row: dict[str, Any]) -> dict[str, Any]:
    tags = row.get("tags")
    if isinstance(tags, str):
        try:
            row["tags"] = json.loads(tags)
        except (TypeError, ValueError):
            row["tags"] = []
    return row


def _parse_position_row(row: dict[str, Any]) -> dict[str, Any]:
    return _parse_journal_row(row)  # same tags handling


def _parse_carry_paper_row(row: dict[str, Any]) -> dict[str, Any]:
    for k in ("tags", "entry_flags"):
        v = row.get(k)
        if isinstance(v, str):
            try:
                row[k] = json.loads(v)
            except (TypeError, ValueError):
                row[k] = []
    for k in ("stopped", "entry_tradable"):
        if row.get(k) is not None:
            row[k] = bool(row[k])
    return row


def _parse_json_cols(row: dict[str, Any], cols: tuple[str, ...]) -> dict[str, Any]:
    for k in cols:
        v = row.get(k)
        if isinstance(v, str):
            try:
                row[k] = json.loads(v)
            except (TypeError, ValueError):
                row[k] = None
    return row


_DEMO_TRADE_MUTABLE = frozenset(
    {"status", "order_id", "fill", "opened_ts", "exit", "closed_ts", "pnl_usd", "r_multiple", "exit_reason", "journal_id"}
)


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None
        self._tx_lock: asyncio.Lock | None = None

    async def init(self) -> None:
        """Open the connection, enable WAL, apply pending migrations."""
        if self._conn is not None:
            return
        self._tx_lock = asyncio.Lock()  # serialises multi-statement transactions on the shared connection
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        cur = await self._conn.execute("PRAGMA user_version")
        row = await cur.fetchone()
        current = int(row[0]) if row else 0
        for v in sorted(MIGRATIONS):
            if v > current:
                for stmt in MIGRATIONS[v]:
                    await self._conn.execute(stmt)
                await self._conn.execute(f"PRAGMA user_version = {v}")
        await self._conn.commit()

    async def aclose(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Store not initialised; call Store.init() first")
        return self._conn

    async def version(self) -> int:
        cur = await self.conn.execute("PRAGMA user_version")
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def table_count(self, table: str) -> int:
        cur = await self.conn.execute(f"SELECT COUNT(*) FROM {table}")
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    # ---------- writes ----------

    async def insert_atm_term(self, ts: int, currency: str, rows: list[dict[str, Any]]) -> int:
        """Idempotent per (ts, currency, expiry): REPLACE semantics."""
        if not rows:
            return 0
        payload = [
            (
                ts,
                currency,
                r["expiry"],
                float(r["days"]),
                float(r["forward"]),
                float(r["atm_iv"]),
                r.get("rr25"),
                r.get("fly25"),
                r.get("rr10"),
                r.get("fly10"),
            )
            for r in rows
        ]
        await self.conn.executemany(
            "INSERT OR REPLACE INTO atm_term (ts, currency, expiry, days, forward, atm_iv, rr25, fly25, rr10, fly10)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            payload,
        )
        await self.conn.commit()
        return len(payload)

    async def insert_funding(self, ts: int, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        payload = [
            (ts, r["venue"], r["base"], float(r["rate_per_interval"]), float(r["interval_hours"]), float(r["annualized"])) for r in rows
        ]
        await self.conn.executemany(
            "INSERT OR REPLACE INTO funding_hist (ts, venue, base, rate_per_interval, interval_hours, annualized) VALUES (?,?,?,?,?,?)",
            payload,
        )
        await self.conn.commit()
        return len(payload)

    async def insert_basis(self, ts: int, currency: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        payload = [(ts, currency, r["expiry"], float(r["days"]), float(r["basis_pct"]), r.get("annualized_simple")) for r in rows]
        await self.conn.executemany(
            "INSERT OR REPLACE INTO basis_hist (ts, currency, expiry, days, basis_pct, annualized_simple) VALUES (?,?,?,?,?,?)",
            payload,
        )
        await self.conn.commit()
        return len(payload)

    async def insert_snapshot(self, ts: int, kind: str, payload: dict[str, Any]) -> int:
        cur = await self.conn.execute(
            "INSERT INTO snapshots (ts, kind, payload) VALUES (?,?,?)",
            (ts, kind, json.dumps(payload, separators=(",", ":"))),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    # ---------- reads ----------

    async def atm_term_series(self, currency: str, expiry: str, since_ts: int, until_ts: int) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT ts, days, forward, atm_iv, rr25, fly25, rr10, fly10 FROM atm_term"
            " WHERE currency = ? AND expiry = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            (currency, expiry, since_ts, until_ts),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def funding_series(self, venue: str, base: str, since_ts: int, until_ts: int) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT ts, rate_per_interval, interval_hours, annualized FROM funding_hist"
            " WHERE venue = ? AND base = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            (venue, base, since_ts, until_ts),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def basis_series(self, currency: str, expiry: str, since_ts: int, until_ts: int) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT ts, days, basis_pct, annualized_simple FROM basis_hist"
            " WHERE currency = ? AND expiry = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            (currency, expiry, since_ts, until_ts),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def insert_supply_snapshot(self, day: str, ts: int, rows: list[dict[str, Any]]) -> int:
        """CoinGecko ``/coins/markets`` rows for one UTC day. Idempotent per (day, coin_id): a later snapshot of the
        same day replaces the earlier one. Rows without an id or symbol are skipped."""

        def num(v: Any) -> float | None:
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        payload = []
        for r in rows:
            if not isinstance(r, dict) or not r.get("id") or not r.get("symbol"):
                continue
            rank = r.get("market_cap_rank")
            payload.append(
                (
                    day,
                    str(r["id"]),
                    ts,
                    str(r["symbol"]).upper(),
                    r.get("name"),
                    int(rank) if isinstance(rank, int) else None,
                    num(r.get("current_price")),
                    num(r.get("market_cap")),
                    num(r.get("fully_diluted_valuation")),
                    num(r.get("total_volume")),
                    num(r.get("circulating_supply")),
                    num(r.get("total_supply")),
                    num(r.get("max_supply")),
                    r.get("last_updated"),
                )
            )
        if not payload:
            return 0
        await self.conn.executemany(
            "INSERT OR REPLACE INTO supply_snapshot (day, coin_id, ts, symbol, name, market_cap_rank, price, market_cap, fdv, "
            "volume_24h, circulating_supply, total_supply, max_supply, last_updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            payload,
        )
        await self.conn.commit()
        return len(payload)

    PM_GAP_COLUMNS = (
        "market_id asset kind direction strike event_slug question end_ts window_start hours_to_end yes_bid yes_ask yes_bid_usd "
        "yes_ask_usd no_bid no_ask no_bid_usd no_ask_usd volume_usd fee_rate fee_exp rules rules_hash rules_standard path_high "
        "path_low binance_last model_prob model_note model_iv model_expiry expiry_gap_hours model_covered spot dvol book_ts surface_ts "
        "model_forward"
    ).split()

    async def insert_pm_gap(self, ts: int, rows: list[dict[str, Any]]) -> int:
        """Open Polymarket BTC/ETH price markets with the options-implied probability; idempotent per (ts, market_id)."""
        if not rows:
            return 0
        cols = self.PM_GAP_COLUMNS
        # The resolution text is long and changes rarely (Polymarket appends clarifications): the hash is in every row, the
        # text only when it differs from the previous snapshot's hash of the same market (index (market_id, ts)).
        rows = [r for r in rows if r.get("market_id")]
        prev: dict[str, str | None] = {}
        for r in rows:
            mid = r["market_id"]
            cur = await self.conn.execute(
                "SELECT rules_hash FROM pm_gap_snapshot WHERE market_id = ? AND ts < ? AND rules_hash IS NOT NULL ORDER BY ts DESC LIMIT 1",
                (mid, ts),
            )
            got = await cur.fetchone()
            prev[mid] = got[0] if got else None
        rows = [{**r, "rules": None} if r.get("rules_hash") is not None and prev[r["market_id"]] == r["rules_hash"] else r for r in rows]
        payload = [(ts, *(r.get(c) for c in cols)) for r in rows]
        marks = ",".join("?" * (len(cols) + 1))
        await self.conn.executemany(f"INSERT OR REPLACE INTO pm_gap_snapshot (ts, {', '.join(cols)}) VALUES ({marks})", payload)
        await self.conn.commit()
        return len(payload)

    async def pm_gap_outcome_pending(self, ended_before: int, ended_after: int, limit: int) -> list[dict[str, Any]]:
        """Snapshotted markets that ended in [``ended_after``, ``ended_before``] and still need a fetch: no settled
        outcome yet, or (threshold markets) no Binance close for the cross-check yet. Fewest attempts first, then the
        latest end, so markets Gamma never settles cannot crowd fresh ones out of the batch."""
        cur = await self.conn.execute(
            "SELECT s.market_id, s.asset, s.kind, s.strike, s.end_ts, o.outcome_yes, o.binance_close_end, o.binance_close_next"
            " FROM (SELECT market_id, MAX(asset) AS asset, MAX(kind) AS kind, MAX(strike) AS strike, MAX(end_ts) AS end_ts"
            "       FROM pm_gap_snapshot WHERE end_ts <= ? AND end_ts >= ? GROUP BY market_id) s"
            " LEFT JOIN pm_gap_outcome o ON o.market_id = s.market_id"
            " WHERE o.market_id IS NULL OR o.outcome_yes IS NULL"
            "    OR (s.kind = 'above' AND (o.binance_close_end IS NULL OR o.binance_close_next IS NULL))"
            " ORDER BY COALESCE(o.attempts, 0), s.end_ts DESC, s.market_id LIMIT ?",
            (ended_before, ended_after, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def upsert_pm_gap_outcome(self, row: dict[str, Any], now_ts: int) -> None:
        """Record one fetch attempt. A settled outcome or a Binance close, once stored, is never overwritten by NULL."""
        await self.conn.execute(
            "INSERT INTO pm_gap_outcome (market_id, asset, kind, strike, end_ts, outcome_yes, outcome_prices, outcomes,"
            " binance_close_end, binance_close_next, attempts, checked_ts, resolved_ts) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)"
            " ON CONFLICT(market_id) DO UPDATE SET"
            " outcome_yes = COALESCE(outcome_yes, excluded.outcome_yes),"
            " outcome_prices = COALESCE(excluded.outcome_prices, outcome_prices),"
            " outcomes = COALESCE(excluded.outcomes, outcomes),"
            " binance_close_end = COALESCE(binance_close_end, excluded.binance_close_end),"
            " binance_close_next = COALESCE(binance_close_next, excluded.binance_close_next),"
            " attempts = attempts + 1, checked_ts = excluded.checked_ts,"
            " resolved_ts = COALESCE(resolved_ts, excluded.resolved_ts)",
            (
                row["market_id"], row["asset"], row["kind"], row["strike"], row["end_ts"], row.get("outcome_yes"),
                row.get("outcome_prices"), row.get("outcomes"), row.get("binance_close_end"), row.get("binance_close_next"), now_ts,
                now_ts if row.get("outcome_yes") is not None else None,
            ),
        )  # fmt: skip
        await self.conn.commit()

    async def insert_trader_snapshot(self, day: str, ts: int, venue: str, rows: list[dict[str, Any]]) -> int:
        """Leaderboard rows of one venue for one UTC day; idempotent per (day, venue, account). ``rank_month`` is
        computed here by ``pnl_month`` (descending) among the rows given."""
        ranked = sorted(rows, key=lambda r: -(r.get("pnl_month") or float("-inf")))
        payload = [
            (
                day,
                venue,
                str(r["account"]).lower(),
                ts,
                r.get("name"),
                r.get("account_value"),
                r.get("pnl_day"),
                r.get("pnl_week"),
                r.get("pnl_month"),
                r.get("pnl_all"),
                r.get("roi_month"),
                r.get("roi_all"),
                r.get("vlm_month"),
                i + 1 if r.get("pnl_month") is not None else None,
            )
            for i, r in enumerate(ranked)
            if r.get("account")
        ]
        if not payload:
            return 0
        await self.conn.executemany(
            "INSERT OR REPLACE INTO trader_snapshot (day, venue, account, ts, name, account_value, pnl_day, pnl_week, pnl_month, "
            "pnl_all, roi_month, roi_all, vlm_month, rank_month) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            payload,
        )
        await self.conn.commit()
        return len(payload)

    async def tracked_traders(self, venue: str, top_n: int, min_account_usd: float = 0.0) -> list[str]:
        """Accounts to watch from the latest snapshot of ``venue``: the top ``top_n`` by month PnL and the top ``top_n``
        by all-time PnL (Hyperliquid: among accounts worth at least ``min_account_usd``)."""
        cur = await self.conn.execute("SELECT MAX(day) FROM trader_snapshot WHERE venue = ?", (venue,))
        row = await cur.fetchone()
        if not row or row[0] is None:
            return []
        out: list[str] = []
        for col in ("pnl_month", "pnl_all"):
            cur = await self.conn.execute(
                f"SELECT account FROM trader_snapshot WHERE venue = ? AND day = ? AND {col} IS NOT NULL "
                f"AND COALESCE(account_value, ?) >= ? ORDER BY {col} DESC LIMIT ?",
                (venue, row[0], min_account_usd, min_account_usd, top_n),
            )
            out += [r[0] for r in await cur.fetchall() if r[0] not in out]
        return out

    async def record_trader_positions(self, ts: int, venue: str, account: str, positions: list[dict[str, Any]]) -> int:
        """Record what changed in ``account``'s positions since the last observation: every position at the first one
        (baseline), later only those opened, changed (side, or size to 4 significant digits) or closed (side
        'closed', size 0). Returns the rows written (0: nothing changed)."""

        def key(p: dict[str, Any]) -> str:
            return f"{p['side']}:{float(p.get('size') or 0):.4g}"

        now = {str(p["market"]): p for p in positions}
        cur = await self.conn.execute(
            "SELECT digest, positions_json FROM trader_position_state WHERE venue = ? AND account = ?", (venue, account)
        )
        prev_row = await cur.fetchone()
        prev: dict[str, dict[str, Any]] = {}
        if prev_row and prev_row[1]:
            try:
                prev = {str(p["market"]): p for p in json.loads(prev_row[1])}
            except (ValueError, TypeError, KeyError):
                prev = {}
        changed = [p for m, p in now.items() if m not in prev or key(prev[m]) != key(p)]
        closed = [{"market": m, "side": "closed", "size": 0.0, "title": p.get("title")} for m, p in prev.items() if m not in now]
        rows = changed + closed
        digest = "|".join(sorted(f"{m}:{key(p)}" for m, p in now.items())) or "flat"
        if prev_row and prev_row[0] == digest and not rows:
            return 0
        if rows:
            await self.conn.executemany(
                "INSERT OR REPLACE INTO trader_positions (ts, venue, account, market, side, size, entry_px, value_usd, upnl, "
                "leverage, liq_px, title) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        ts,
                        venue,
                        account,
                        str(p["market"]),
                        str(p["side"]),
                        p.get("size"),
                        p.get("entry_px"),
                        p.get("value_usd"),
                        p.get("upnl"),
                        p.get("leverage"),
                        p.get("liq_px"),
                        p.get("title"),
                    )
                    for p in rows
                ],
            )
        await self.conn.execute(
            "INSERT OR REPLACE INTO trader_position_state (venue, account, digest, ts, positions_json) VALUES (?,?,?,?,?)",
            (venue, account, digest, ts, json.dumps(list(now.values()), separators=(",", ":"))),
        )
        await self.conn.commit()
        return len(rows)

    async def prune(self, table: str, cutoff_ts: int) -> int:
        """Delete rows with ts < cutoff_ts. Returns the number of rows deleted."""
        if table not in {"atm_term", "funding_hist", "basis_hist", "snapshots", "alerts", "liquidations", "ohlcv_hist"}:
            raise ValueError(f"unknown table {table!r}")
        cur = await self.conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff_ts,))
        await self.conn.commit()
        return cur.rowcount or 0

    # ---------- alerts ----------

    async def insert_alert(
        self,
        *,
        rule_id: str,
        ts: int,
        payload: dict[str, Any],
        channel: str,
        delivered: bool,
        delivery_error: str | None = None,
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO alerts (rule_id, ts, payload, channel, delivered, delivery_error) VALUES (?,?,?,?,?,?)",
            (rule_id, ts, json.dumps(payload, separators=(",", ":")), channel, 1 if delivered else 0, delivery_error),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def last_alert_ts(self, rule_id: str) -> int | None:
        cur = await self.conn.execute("SELECT MAX(ts) FROM alerts WHERE rule_id = ?", (rule_id,))
        row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    async def journal_insert(
        self,
        *,
        opened_ts: int,
        symbol: str,
        side: str,
        size: float,
        entry: float,
        stop: float | None = None,
        target: float | None = None,
        thesis: str | None = None,
        tags: list[str] | None = None,
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO journal (opened_ts, symbol, side, size, entry, stop, target, thesis, tags, status) "
            "VALUES (?,?,?,?,?,?,?,?,?, 'open')",
            (opened_ts, symbol, side, float(size), float(entry), stop, target, thesis, json.dumps(tags or [])),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def journal_update_close(
        self,
        trade_id: int,
        *,
        closed_ts: int,
        status: str,
        exit_: float | None,
        outcome: str | None,
        pnl: float | None,
        r_multiple: float | None,
        note: str | None,
    ) -> int:
        cur = await self.conn.execute(
            "UPDATE journal SET closed_ts = ?, status = ?, exit = ?, outcome = ?, pnl = ?, r_multiple = ?, note = ? WHERE id = ?",
            (closed_ts, status, exit_, outcome, pnl, r_multiple, note, trade_id),
        )
        await self.conn.commit()
        return cur.rowcount or 0

    async def journal_get(self, trade_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM journal WHERE id = ?", (trade_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        return _parse_journal_row(dict(row))

    async def journal_list(
        self,
        *,
        status: str | None = None,
        symbol: str | None = None,
        tag: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if symbol:
            where.append("symbol = ?")
            params.append(symbol)
        if tag:
            # tags are stored as JSON array; use LIKE on the quoted value.
            where.append("tags LIKE ?")
            params.append(f'%"{tag}"%')
        sql = "SELECT * FROM journal"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY opened_ts DESC LIMIT ?"
        params.append(limit)
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        return [_parse_journal_row(dict(r)) for r in rows]

    # ---------- carry_paper (paper perp-perp carry pairs, local bookkeeping only) ----------

    async def carry_paper_insert(self, **f: Any) -> int:
        cols = (
            "opened_ts base symbol long_venue short_venue qty notional_usd long_entry short_entry entry_trailing_spread_apr "
            "entry_current_spread_apr leverage stop_move lookback_days exit_apr slippage note"
        ).split()
        f.setdefault("slippage", 0.0005)
        vals = [f.get(c) for c in cols]
        cols += ["entry_flags", "entry_tradable", "tags"]
        tradable = f.get("entry_tradable")
        vals += [json.dumps(f.get("entry_flags") or []), None if tradable is None else int(bool(tradable)), json.dumps(f.get("tags") or [])]
        cur = await self.conn.execute(
            f"INSERT INTO carry_paper ({', '.join(cols)}, status) VALUES ({', '.join('?' * len(cols))}, 'open')", vals
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def carry_paper_get(self, trade_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM carry_paper WHERE id = ?", (trade_id,))
        row = await cur.fetchone()
        return _parse_carry_paper_row(dict(row)) if row else None

    async def carry_paper_list(self, *, status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        sql = "SELECT * FROM carry_paper" + (" WHERE status = ?" if status else "") + " ORDER BY opened_ts DESC, id DESC LIMIT ?"
        cur = await self.conn.execute(sql, [status, limit] if status else [limit])
        return [_parse_carry_paper_row(dict(r)) for r in await cur.fetchall()]

    async def carry_paper_close(
        self,
        trade_id: int,
        *,
        status: str,
        closed_ts: int,
        exit_reason: str,
        long_exit: float | None = None,
        short_exit: float | None = None,
        funding_pnl: float | None = None,
        price_pnl: float | None = None,
        costs: float | None = None,
        pnl_usd: float | None = None,
        return_on_notional: float | None = None,
        max_up: float | None = None,
        max_down: float | None = None,
        stopped: bool = False,
        stop_ts: int | None = None,
        journal: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> int:
        """Claim an open pair with one conditional UPDATE (``WHERE status='open'``) and, when ``journal`` is given,
        write its mirror row in the same transaction. Returns 1 when this call closed the pair, 0 when it was no
        longer open (nothing is written). Calls are serialised so concurrent closes on the shared connection cannot
        interleave their transactions."""
        lock = self._tx_lock or asyncio.Lock()
        async with lock:
            try:
                cur = await self.conn.execute(
                    "UPDATE carry_paper SET status = ?, closed_ts = ?, stop_ts = ?, exit_reason = ?, long_exit = ?, short_exit = ?,"
                    " funding_pnl = ?, price_pnl = ?, costs = ?, pnl_usd = ?, return_on_notional = ?, max_up = ?, max_down = ?,"
                    " stopped = ?, note = COALESCE(?, note) WHERE id = ? AND status = 'open'",
                    (
                        status, closed_ts, stop_ts, exit_reason, long_exit, short_exit, funding_pnl, price_pnl, costs, pnl_usd,
                        return_on_notional, max_up, max_down, 1 if stopped else 0, note, trade_id,
                    ),
                )  # fmt: skip
                if not cur.rowcount:
                    await self.conn.rollback()
                    return 0
                if journal is not None:
                    j = await self.conn.execute(
                        "INSERT INTO journal (opened_ts, closed_ts, symbol, side, size, entry, thesis, tags, status, exit, outcome,"
                        " pnl, r_multiple, note) VALUES (?,?,?,?,?,?,?,?, 'closed', ?,?,?, NULL, ?)",
                        (
                            journal["opened_ts"], closed_ts, journal["symbol"], journal["side"], journal["size"], journal["entry"],
                            journal["thesis"], json.dumps(journal["tags"]), journal["exit"], journal["outcome"], journal["pnl"],
                            note,
                        ),
                    )  # fmt: skip
                    await self.conn.execute("UPDATE carry_paper SET journal_id = ? WHERE id = ?", (j.lastrowid, trade_id))
                await self.conn.commit()
                return 1
            except Exception:
                await self.conn.rollback()
                raise

    # ---------- rec_paper (paper trades of the assistant's option recommendations, bookkeeping only) ----------

    async def rec_paper_insert(self, **f: Any) -> int:
        cols = (
            "opened_ts currency structure expiry_ts spot forward t_years premium_per_btc entry_fees_per_btc max_loss_per_btc "
            "risk_basis size_btc deposit_usd risk_pct risk_usd note method_mean_pct method_q05_pct regime trigger"
        ).split()
        vals = [f.get(c) for c in cols]
        cols += ["legs", "within_risk", "label", "source"]
        vals += [json.dumps(f["legs"]), int(bool(f["within_risk"])), f.get("label") or "hypothesis", f.get("source") or "manual"]
        cur = await self.conn.execute(
            f"INSERT INTO rec_paper ({', '.join(cols)}, status) VALUES ({', '.join('?' * len(cols))}, 'open')", vals
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def rec_paper_list(self, *, status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        sql = "SELECT * FROM rec_paper" + (" WHERE status = ?" if status else "") + " ORDER BY opened_ts DESC, id DESC LIMIT ?"
        cur = await self.conn.execute(sql, [status, limit] if status else [limit])
        rows = [_parse_json_cols(dict(r), ("legs",)) for r in await cur.fetchall()]
        for row in rows:
            row["within_risk"] = bool(row["within_risk"])
        return rows

    async def rec_paper_get(self, trade_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM rec_paper WHERE id = ?", (trade_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        out = _parse_json_cols(dict(row), ("legs",))
        out["within_risk"] = bool(out["within_risk"])
        return out

    async def rec_paper_settle(
        self,
        trade_id: int,
        *,
        settled_ts: int,
        delivery_price: float,
        pnl_usd: float,
        pnl_pct_spot: float,
        r_worst_case: float | None,
        r_study: float | None,
    ) -> int:
        """Settle an open paper trade with one conditional UPDATE; 1 when this call settled it, 0 when it was not open."""
        cur = await self.conn.execute(
            "UPDATE rec_paper SET status = 'settled', settled_ts = ?, delivery_price = ?, pnl_usd = ?, pnl_pct_spot = ?,"
            " r_worst_case = ?, r_study = ? WHERE id = ? AND status = 'open'",
            (settled_ts, delivery_price, pnl_usd, pnl_pct_spot, r_worst_case, r_study, trade_id),
        )
        await self.conn.commit()
        return int(cur.rowcount or 0)

    # ---------- trade_plan (the assistant's trade plans, scored on candles; bookkeeping only) ----------

    async def plan_insert(self, **f: Any) -> int:
        cols = ("created_ts exchange symbol instrument side entry_type entry stop target expires_ts verdict thesis rec_paper_id").split()
        vals = [f.get(c) for c in cols]
        cols += ["rules", "tags"]
        vals += [json.dumps(f.get("rules") or {}), json.dumps(f.get("tags") or [])]
        cur = await self.conn.execute(
            f"INSERT INTO trade_plan ({', '.join(cols)}, status) VALUES ({', '.join('?' * len(cols))}, 'open')", vals
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def plan_get(self, plan_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM trade_plan WHERE id = ?", (plan_id,))
        row = await cur.fetchone()
        return _parse_json_cols(dict(row), ("rules", "tags")) if row else None

    async def plan_list(
        self,
        *,
        status: str | None = None,
        verdict: str | None = None,
        tag: str | None = None,
        symbol: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        for col, v in (("status", status), ("verdict", verdict), ("symbol", symbol)):
            if v:
                where.append(f"{col} = ?")
                params.append(v)
        if tag:
            where.append("tags LIKE ?")  # JSON array: match the quoted value
            params.append(f'%"{tag}"%')
        sql = "SELECT * FROM trade_plan" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_ts DESC, id DESC LIMIT ?"
        cur = await self.conn.execute(sql, [*params, limit])
        return [_parse_json_cols(dict(r), ("rules", "tags")) for r in await cur.fetchall()]

    async def plan_set_fill(self, plan_id: int, *, filled_ts: int, fill: float, timeframe: str) -> int:
        """Record the paper fill of a plan that is still open."""
        cur = await self.conn.execute(
            "UPDATE trade_plan SET filled_ts = ?, fill = ?, timeframe = ? WHERE id = ? AND status = 'open'",
            (filled_ts, fill, timeframe, plan_id),
        )
        await self.conn.commit()
        return int(cur.rowcount or 0)

    async def plan_resolve(
        self,
        plan_id: int,
        *,
        outcome: str,
        resolved_ts: int,
        filled_ts: int | None,
        fill: float | None,
        exit_: float | None,
        r_result: float | None,
        timeframe: str | None,
        note: str | None,
    ) -> int:
        """Resolve an open plan with one conditional UPDATE; 1 when this call resolved it, 0 when it was not open."""
        cur = await self.conn.execute(
            "UPDATE trade_plan SET status = 'resolved', outcome = ?, resolved_ts = ?, filled_ts = ?, fill = ?, exit = ?,"
            " r_result = ?, timeframe = ?, resolve_note = ? WHERE id = ? AND status = 'open'",
            (outcome, resolved_ts, filled_ts, fill, exit_, r_result, timeframe, note, plan_id),
        )
        await self.conn.commit()
        return int(cur.rowcount or 0)

    async def plan_link_journal(self, plan_id: int, journal_id: int) -> int:
        cur = await self.conn.execute("UPDATE trade_plan SET journal_id = ? WHERE id = ?", (journal_id, plan_id))
        await self.conn.commit()
        return int(cur.rowcount or 0)

    # ---------- cp_*: forward paper engine for the carry strategies (bookkeeping only, no orders) ----------

    async def cp_cycle_get(self, strategy: str, cycle_ts: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM cp_cycle WHERE strategy = ? AND cycle_ts = ?", (strategy, cycle_ts))
        row = await cur.fetchone()
        return _parse_json_cols(dict(row), ("payload",)) if row else None

    async def cp_cycles(self, strategy: str, limit: int = 200) -> list[dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM cp_cycle WHERE strategy = ? ORDER BY cycle_ts DESC LIMIT ?", (strategy, limit))
        return [_parse_json_cols(dict(r), ("payload",)) for r in await cur.fetchall()]

    async def cp_positions(self, strategy: str, status: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        sql = (
            "SELECT * FROM cp_position WHERE strategy = ?" + (" AND status = ?" if status else "") + " ORDER BY opened_ts, trade_id LIMIT ?"
        )
        cur = await self.conn.execute(sql, [strategy, status, limit] if status else [strategy, limit])
        return [_parse_json_cols(dict(r), ("state", "trade")) for r in await cur.fetchall()]

    async def cp_logs(self, strategy: str, kind: str, *, trade_id: str | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        sql = "SELECT * FROM cp_log WHERE strategy = ? AND kind = ?" + (" AND trade_id = ?" if trade_id else "") + " ORDER BY id LIMIT ?"
        cur = await self.conn.execute(sql, [strategy, kind, trade_id, limit] if trade_id else [strategy, kind, limit])
        return [_parse_json_cols(dict(r), ("payload",)) for r in await cur.fetchall()]

    async def cp_log_add(self, logs: list[dict[str, Any]]) -> int:
        """Append log rows outside a cycle commit (e.g. a failed attempt)."""
        if not logs:
            return 0
        await self.conn.executemany(
            "INSERT INTO cp_log (strategy, cycle_ts, kind, trade_id, payload) VALUES (?,?,?,?,?)",
            [(x["strategy"], x["cycle_ts"], x["kind"], x.get("trade_id"), json.dumps(x["payload"])) for x in logs],
        )
        await self.conn.commit()
        return len(logs)

    async def cp_cycle_commit(self, cycle: dict[str, Any], positions: list[dict[str, Any]], logs: list[dict[str, Any]]) -> bool:
        """Write one engine cycle in a single transaction: the cycle row, position upserts (a closed position may
        carry a ``journal`` mirror row), and log rows. Returns False, writing nothing, when the cycle already exists."""
        lock = self._tx_lock or asyncio.Lock()
        async with lock:
            try:
                cur = await self.conn.execute(
                    "INSERT OR IGNORE INTO cp_cycle (strategy, cycle_ts, status, reason, started_ms, finished_ms, git_sha, params_hash, payload)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        cycle["strategy"], cycle["cycle_ts"], cycle["status"], cycle.get("reason"), cycle.get("started_ms"),
                        cycle.get("finished_ms"), cycle.get("git_sha"), cycle.get("params_hash"), json.dumps(cycle.get("payload") or {}),
                    ),
                )  # fmt: skip
                if not cur.rowcount:
                    await self.conn.rollback()
                    return False
                for p in positions:
                    journal_id = None
                    j = p.get("journal")
                    if j is not None:
                        jc = await self.conn.execute(
                            "INSERT INTO journal (opened_ts, closed_ts, symbol, side, size, entry, thesis, tags, status, exit, outcome,"
                            " pnl, r_multiple, note) VALUES (?,?,?,?,?,?,?,?, 'closed', ?,?,?, NULL, NULL)",
                            (
                                j["opened_ts"], j["closed_ts"], j["symbol"], j["side"], j["size"], j["entry"], j["thesis"],
                                json.dumps(j["tags"]), j["exit"], j["outcome"], j["pnl"],
                            ),
                        )  # fmt: skip
                        journal_id = jc.lastrowid
                    await self.conn.execute(
                        "INSERT INTO cp_position (trade_id, strategy, symbol, status, opened_ts, closed_ts, slot_notional, state, trade,"
                        " exit_reason, journal_id) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_id) DO UPDATE SET status = excluded.status,"
                        " closed_ts = excluded.closed_ts, state = excluded.state, trade = excluded.trade, exit_reason = excluded.exit_reason,"
                        " journal_id = COALESCE(excluded.journal_id, journal_id)",
                        (
                            p["trade_id"], p["strategy"], p["symbol"], p["status"], p["opened_ts"], p.get("closed_ts"), p["slot_notional"],
                            json.dumps(p["state"]), json.dumps(p["trade"]) if p.get("trade") is not None else None, p.get("exit_reason"),
                            journal_id,
                        ),
                    )  # fmt: skip
                if logs:
                    await self.conn.executemany(
                        "INSERT INTO cp_log (strategy, cycle_ts, kind, trade_id, payload) VALUES (?,?,?,?,?)",
                        [(x["strategy"], x["cycle_ts"], x["kind"], x.get("trade_id"), json.dumps(x["payload"])) for x in logs],
                    )
                await self.conn.commit()
                return True
            except Exception:
                await self.conn.rollback()
                raise

    async def cp_funding_state(self, venue: str, symbol: str) -> dict[str, int] | None:
        """Cached funding history of one symbol: ``covered_from`` / ``covered_to`` and the newest stored payment ``last_ts``."""
        cur = await self.conn.execute("SELECT covered_from, covered_to FROM cp_funding_cov WHERE venue = ? AND symbol = ?", (venue, symbol))
        cov = await cur.fetchone()
        if cov is None:
            return None
        cur = await self.conn.execute("SELECT MAX(ts) AS m FROM cp_funding WHERE venue = ? AND symbol = ?", (venue, symbol))
        last = await cur.fetchone()
        return {
            "covered_from": int(cov["covered_from"]),
            "covered_to": int(cov["covered_to"]),
            "last_ts": int(last["m"]) if last and last["m"] else 0,
        }

    async def cp_funding_rows(self, venue: str, symbol: str, since: int, until: int) -> list[tuple[int, float]]:
        cur = await self.conn.execute(
            "SELECT ts, rate FROM cp_funding WHERE venue = ? AND symbol = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            (venue, symbol, since, until),
        )
        return [(int(r["ts"]), float(r["rate"])) for r in await cur.fetchall()]

    async def cp_funding_put(
        self, venue: str, symbol: str, rows: list[tuple[int, float]], *, covered_from: int | None, covered_to: int | None
    ) -> None:
        """Store payments (idempotent). ``covered_*`` None leaves the coverage unchanged (an incomplete fetch)."""
        async with self._tx_lock or asyncio.Lock():  # do not commit another task's half-built transaction
            if rows:
                await self.conn.executemany(
                    "INSERT OR REPLACE INTO cp_funding (venue, symbol, ts, rate) VALUES (?,?,?,?)",
                    [(venue, symbol, ts, r) for ts, r in rows],
                )
            if covered_from is not None and covered_to is not None:
                await self.conn.execute(
                    "INSERT INTO cp_funding_cov (venue, symbol, covered_from, covered_to) VALUES (?,?,?,?) ON CONFLICT(venue, symbol)"
                    " DO UPDATE SET covered_from = MIN(covered_from, excluded.covered_from),"
                    " covered_to = MAX(covered_to, excluded.covered_to)",
                    (venue, symbol, covered_from, covered_to),
                )
            await self.conn.commit()

    # ---------- rules (DSL) ----------

    async def rule_insert(
        self,
        *,
        name: str,
        expr: str,
        message: str | None,
        cooldown_seconds: int,
        enabled: bool,
        created_ts: int,
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO rules (name, enabled, expr, message, cooldown_seconds, created_ts) VALUES (?,?,?,?,?,?)",
            (name, 1 if enabled else 0, expr, message, int(cooldown_seconds), int(created_ts)),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def rule_get(self, rule_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        return d

    async def rule_set_enabled(self, rule_id: int, enabled: bool, *, ts: int) -> int:
        cur = await self.conn.execute(
            "UPDATE rules SET enabled = ?, disabled_ts = ? WHERE id = ?",
            (1 if enabled else 0, (None if enabled else ts), rule_id),
        )
        await self.conn.commit()
        return cur.rowcount or 0

    async def rule_delete(self, rule_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        await self.conn.commit()
        return cur.rowcount or 0

    async def rules_list(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM rules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name"
        cur = await self.conn.execute(sql)
        rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["enabled"] = bool(d["enabled"])
            out.append(d)
        return out

    # ---------- events (macro calendar) ----------

    async def event_insert(
        self,
        *,
        ts: int,
        kind: str,
        label: str,
        impact: str = "medium",
        meta: dict[str, Any] | None = None,
        ignore_duplicate: bool = True,
    ) -> int:
        """Insert one event. With ``ignore_duplicate`` (default) the UNIQUE
        (ts, kind, label) constraint drops repeats silently; returns the row's
        id if inserted, else 0."""
        verb = "INSERT OR IGNORE" if ignore_duplicate else "INSERT"
        cur = await self.conn.execute(
            f"{verb} INTO events (ts, kind, label, impact, meta) VALUES (?,?,?,?,?)",
            (int(ts), kind, label, impact, json.dumps(meta) if meta else None),
        )
        await self.conn.commit()
        # `lastrowid` sticks around across INSERT OR IGNORE no-ops, so gate on rowcount.
        return int(cur.lastrowid or 0) if (cur.rowcount or 0) > 0 else 0

    async def events_insert_many(self, rows: list[dict[str, Any]]) -> int:
        """Bulk upsert with UNIQUE-based idempotency. Returns the number of
        new rows (SQLite's ``total_changes`` delta)."""
        if not rows:
            return 0
        before = self.conn.total_changes
        payload = []
        for r in rows:
            try:
                payload.append(
                    (
                        int(r["ts"]),
                        str(r["kind"]),
                        str(r["label"]),
                        str(r.get("impact") or "medium"),
                        json.dumps(r["meta"]) if r.get("meta") else None,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        await self.conn.executemany(
            "INSERT OR IGNORE INTO events (ts, kind, label, impact, meta) VALUES (?,?,?,?,?)",
            payload,
        )
        await self.conn.commit()
        return self.conn.total_changes - before

    async def events_list(
        self,
        *,
        kind: str | None = None,
        impact: str | None = None,
        since_ts: int | None = None,
        until_ts: int | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if impact:
            where.append("impact = ?")
            params.append(impact)
        if since_ts is not None:
            where.append("ts >= ?")
            params.append(int(since_ts))
        if until_ts is not None:
            where.append("ts <= ?")
            params.append(int(until_ts))
        sql = "SELECT id, ts, kind, label, impact, meta FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts LIMIT ?"
        params.append(int(limit))
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            m = d.get("meta")
            if isinstance(m, str):
                try:
                    d["meta"] = json.loads(m)
                except (TypeError, ValueError):
                    pass
            out.append(d)
        return out

    async def event_count(self, kind: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM events"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    # ---------- ohlcv history ----------

    async def insert_ohlcv_batch(self, venue: str, symbol: str, timeframe: str, bars: list[Any]) -> int:
        """Insert ccxt-shaped bars ``[[ts_ms, o, h, l, c, v], ...]``. Idempotent per
        (venue, symbol, timeframe, ts) — later rows for the same ts replace earlier ones.
        """
        if not bars:
            return 0
        payload = []
        for b in bars:
            if isinstance(b, (list, tuple)) and len(b) >= 6:
                payload.append((venue, symbol, timeframe, int(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])))
            elif isinstance(b, dict) and "close" in b:
                payload.append(
                    (
                        venue,
                        symbol,
                        timeframe,
                        int(b.get("ts") or b.get("timestamp") or 0),
                        float(b.get("open") or b["close"]),
                        float(b.get("high") or b["close"]),
                        float(b.get("low") or b["close"]),
                        float(b["close"]),
                        float(b.get("volume") or 0.0),
                    )
                )
        if not payload:
            return 0
        await self.conn.executemany(
            "INSERT OR REPLACE INTO ohlcv_hist (venue, symbol, timeframe, ts, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?,?)",
            payload,
        )
        await self.conn.commit()
        return len(payload)

    async def ohlcv_range(
        self,
        *,
        venue: str,
        symbol: str,
        timeframe: str,
        since_ts: int = 0,
        until_ts: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        where = ["venue = ?", "symbol = ?", "timeframe = ?", "ts >= ?"]
        params: list[Any] = [venue, symbol, timeframe, since_ts]
        if until_ts is not None:
            where.append("ts <= ?")
            params.append(until_ts)
        sql = "SELECT ts, open, high, low, close, volume FROM ohlcv_hist WHERE " + " AND ".join(where) + " ORDER BY ts"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def ohlcv_last_ts(self, venue: str, symbol: str, timeframe: str) -> int | None:
        cur = await self.conn.execute(
            "SELECT MAX(ts) FROM ohlcv_hist WHERE venue = ? AND symbol = ? AND timeframe = ?",
            (venue, symbol, timeframe),
        )
        row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    # ---------- liquidations ----------

    async def insert_liquidations(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        payload = [
            (
                int(r["ts"]),
                r["venue"],
                r["symbol"],
                r["side"],
                float(r["qty"]),
                float(r["price"]),
                float(r["notional_usd"]),
            )
            for r in rows
        ]
        await self.conn.executemany(
            "INSERT INTO liquidations (ts, venue, symbol, side, qty, price, notional_usd) VALUES (?,?,?,?,?,?,?)",
            payload,
        )
        await self.conn.commit()
        return len(payload)

    async def liquidations_in_window(
        self,
        *,
        since_ts: int,
        until_ts: int,
        venue: str | None = None,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        where = ["ts >= ?", "ts <= ?"]
        params: list[Any] = [since_ts, until_ts]
        if venue:
            where.append("venue = ?")
            params.append(venue)
        if symbol:
            where.append("symbol = ?")
            params.append(symbol)
        sql = "SELECT ts, venue, symbol, side, qty, price, notional_usd FROM liquidations WHERE " + " AND ".join(where) + " ORDER BY ts"
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ---------- positions ----------

    async def position_insert(
        self,
        *,
        opened_ts: int,
        instrument: str,
        kind: str,
        side: str,
        size: float,
        entry: float | None,
        mark: float | None,
        delta: float | None,
        gamma: float | None,
        vega: float | None,
        theta: float | None,
        tags: list[str] | None,
        source: str = "manual",
        venue: str | None = None,
        external_id: str | None = None,
        note: str | None = None,
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO positions (opened_ts, source, venue, external_id, instrument, kind, side, size, entry, mark,"
            " delta, gamma, vega, theta, tags, status, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?)",
            (
                opened_ts,
                source,
                venue,
                external_id,
                instrument,
                kind,
                side,
                float(size),
                entry,
                mark,
                delta,
                gamma,
                vega,
                theta,
                json.dumps(tags or []),
                note,
            ),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def position_get(self, position_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,))
        row = await cur.fetchone()
        return _parse_position_row(dict(row)) if row else None

    async def position_update_close(self, position_id: int, closed_ts: int, note: str | None = None) -> int:
        cur = await self.conn.execute(
            "UPDATE positions SET status = 'closed', closed_ts = ?, note = COALESCE(?, note) WHERE id = ?",
            (closed_ts, note, position_id),
        )
        await self.conn.commit()
        return cur.rowcount or 0

    async def positions_list(
        self,
        *,
        status: str | None = None,
        instrument: str | None = None,
        tag: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if instrument:
            where.append("instrument = ?")
            params.append(instrument)
        if tag:
            where.append("tags LIKE ?")
            params.append(f'%"{tag}"%')
        sql = "SELECT * FROM positions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY opened_ts DESC LIMIT ?"
        params.append(limit)
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        return [_parse_position_row(dict(r)) for r in rows]

    async def recent_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT id, rule_id, ts, payload, channel, delivered, delivery_error FROM alerts ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"])
            except (TypeError, ValueError):
                pass
            d["delivered"] = bool(d["delivered"])
            out.append(d)
        return out

    # ---------- demo execution (schema 19, ADR 0007) ----------

    async def exec_audit_add(
        self,
        ts: int,
        venue: str,
        kind: str,
        *,
        link_id: str | None = None,
        symbol: str | None = None,
        setup: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Append one row to the append-only audit log (triggers refuse UPDATE and DELETE)."""
        cur = await self.conn.execute(
            "INSERT INTO exec_audit (ts, venue, kind, link_id, symbol, setup, payload) VALUES (?,?,?,?,?,?,?)",
            (ts, venue, kind, link_id, symbol, setup, json.dumps(payload or {}, default=str)),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def exec_audit_list(self, since_ts: int = 0, kinds: tuple[str, ...] = (), limit: int = 500) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM exec_audit WHERE ts >= ?", [since_ts]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params += list(kinds)
        cur = await self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", [*params, limit])
        return [_parse_json_cols(dict(r), ("payload",)) for r in await cur.fetchall()]

    async def demo_trade_insert(self, row: dict[str, Any]) -> None:
        cols = [c for c in row if c.isidentifier()]
        await self.conn.execute(f"INSERT INTO demo_trade ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [row[c] for c in cols])
        await self.conn.commit()

    async def demo_trade_update(self, link_id: str, **fields: Any) -> int:
        if not fields:
            return 0
        bad = set(fields) - _DEMO_TRADE_MUTABLE
        if bad:
            raise ValueError(f"demo_trade: not updatable {sorted(bad)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        cur = await self.conn.execute(f"UPDATE demo_trade SET {sets} WHERE link_id = ?", [*fields.values(), link_id])
        await self.conn.commit()
        return cur.rowcount or 0

    async def demo_trade_list(self, statuses: tuple[str, ...] = (), limit: int = 500) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM demo_trade", []
        if statuses:
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
            params = list(statuses)
        cur = await self.conn.execute(sql + " ORDER BY signal_ts DESC LIMIT ?", [*params, limit])
        return [dict(r) for r in await cur.fetchall()]
