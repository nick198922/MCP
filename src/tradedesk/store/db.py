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
