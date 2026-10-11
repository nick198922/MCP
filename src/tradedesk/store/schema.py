"""Schema migrations. Bump `SCHEMA_VERSION` and add a block to MIGRATIONS."""

from __future__ import annotations

SCHEMA_VERSION = 2

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
}
