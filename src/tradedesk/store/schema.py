"""Schema migrations. Bump `SCHEMA_VERSION` and add a block to MIGRATIONS."""

from __future__ import annotations

SCHEMA_VERSION = 6

# Each migration is a list of SQL statements executed in a single transaction.
# `PRAGMA user_version` is bumped by Store.init() after a block runs.
MIGRATIONS: dict[int, list[str]] = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS atm_term (
            ts INTEGER NOT NULL,
            currency TEXT NOT NULL,
            expiry TEXT NOT NULL,         -- ISO-8601 UTC of the 08:00 expiry
            days REAL NOT NULL,
            forward REAL NOT NULL,
            atm_iv REAL NOT NULL,         -- decimal (0.45 = 45 %)
            rr25 REAL,                    -- 25-delta risk reversal
            fly25 REAL,                   -- 25-delta butterfly
            rr10 REAL,
            fly10 REAL,
            PRIMARY KEY (ts, currency, expiry)
        )
        """,
        "CREATE INDEX IF NOT EXISTS atm_term_cur_exp_ts ON atm_term (currency, expiry, ts)",
        """
        CREATE TABLE IF NOT EXISTS funding_hist (
            ts INTEGER NOT NULL,
            venue TEXT NOT NULL,
            base TEXT NOT NULL,
            rate_per_interval REAL NOT NULL,
            interval_hours REAL NOT NULL,
            annualized REAL NOT NULL,
            PRIMARY KEY (ts, venue, base)
        )
        """,
        "CREATE INDEX IF NOT EXISTS funding_venue_base_ts ON funding_hist (venue, base, ts)",
        """
        CREATE TABLE IF NOT EXISTS basis_hist (
            ts INTEGER NOT NULL,
            currency TEXT NOT NULL,
            expiry TEXT NOT NULL,         -- ISO-8601 UTC
            days REAL NOT NULL,
            basis_pct REAL NOT NULL,      -- decimal (0.02 = 2 %)
            annualized_simple REAL,
            PRIMARY KEY (ts, currency, expiry)
        )
        """,
        "CREATE INDEX IF NOT EXISTS basis_cur_exp_ts ON basis_hist (currency, expiry, ts)",
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL          -- JSON
        )
        """,
        "CREATE INDEX IF NOT EXISTS snapshots_kind_ts ON snapshots (kind, ts)",
    ],
    2: [
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id TEXT NOT NULL,
            ts INTEGER NOT NULL,
            payload TEXT NOT NULL,          -- JSON {metric, value, threshold, message, …}
            channel TEXT NOT NULL,          -- "log", "telegram", …
            delivered INTEGER NOT NULL DEFAULT 0,
            delivery_error TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS alerts_rule_ts ON alerts (rule_id, ts)",
    ],
    3: [
        """
        CREATE TABLE IF NOT EXISTS journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_ts INTEGER NOT NULL,
            closed_ts INTEGER,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,                   -- 'long' | 'short'
            size REAL NOT NULL,                   -- native units (contracts / coins)
            entry REAL NOT NULL,
            stop REAL,
            target REAL,
            thesis TEXT,
            tags TEXT,                             -- JSON array of strings
            status TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'closed' | 'cancelled'
            exit REAL,
            outcome TEXT,                          -- 'win' | 'loss' | 'break_even' | 'cancelled'
            pnl REAL,
            r_multiple REAL,
            note TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS journal_status_opened ON journal (status, opened_ts)",
        "CREATE INDEX IF NOT EXISTS journal_symbol_opened ON journal (symbol, opened_ts)",
    ],
    4: [
        """
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_ts INTEGER NOT NULL,
            closed_ts INTEGER,
            source TEXT NOT NULL DEFAULT 'manual',   -- 'manual' | 'exchange'
            venue TEXT,
            external_id TEXT,
            instrument TEXT NOT NULL,
            kind TEXT NOT NULL,                       -- 'option' | 'future' | 'perp' | 'spot'
            side TEXT NOT NULL,                       -- 'long' | 'short'
            size REAL NOT NULL,
            entry REAL,
            mark REAL,
            delta REAL,
            gamma REAL,
            vega REAL,
            theta REAL,
            tags TEXT,
            status TEXT NOT NULL DEFAULT 'open',      -- 'open' | 'closed'
            note TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS positions_status_opened ON positions (status, opened_ts)",
        "CREATE INDEX IF NOT EXISTS positions_instrument_opened ON positions (instrument, opened_ts)",
    ],
    5: [
        """
        CREATE TABLE IF NOT EXISTS liquidations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,            -- unix seconds
            venue TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,              -- taker side: 'buy' = short got liquidated, 'sell' = long got liquidated
            qty REAL NOT NULL,
            price REAL NOT NULL,
            notional_usd REAL NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS liquidations_venue_ts ON liquidations (venue, ts)",
        "CREATE INDEX IF NOT EXISTS liquidations_symbol_ts ON liquidations (symbol, ts)",
    ],
    6: [
        """
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1,
            expr TEXT NOT NULL,
            message TEXT,
            cooldown_seconds INTEGER NOT NULL DEFAULT 1800,
            created_ts INTEGER NOT NULL,
            disabled_ts INTEGER
        )
        """,
        "CREATE INDEX IF NOT EXISTS rules_enabled_name ON rules (enabled, name)",
    ],
}
