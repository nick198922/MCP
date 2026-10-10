"""aiosqlite-backed store with PRAGMA user_version migrations.

Thread-safety: aiosqlite serialises calls through one background thread per
connection, so a single ``Store`` instance can be shared across async tasks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from .schema import MIGRATIONS


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Open the connection, enable WAL, apply pending migrations."""
        if self._conn is not None:
            return
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
