"""Schema migrations. Bump `SCHEMA_VERSION` and add a block to MIGRATIONS."""

from __future__ import annotations

SCHEMA_VERSION = 10

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
    7: [
        """
        CREATE TABLE IF NOT EXISTS ohlcv_hist (
            venue TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            ts INTEGER NOT NULL,            -- unix ms of the bar's open (ccxt convention)
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            PRIMARY KEY (venue, symbol, timeframe, ts)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ohlcv_market_ts ON ohlcv_hist (venue, symbol, timeframe, ts)",
    ],
    8: [
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,          -- unix seconds (UTC)
            kind TEXT NOT NULL,           -- 'fomc' | 'cpi' | 'nfp' | …
            label TEXT NOT NULL,
            impact TEXT NOT NULL DEFAULT 'medium',   -- 'low' | 'medium' | 'high'
            meta TEXT,                     -- optional JSON
            UNIQUE (ts, kind, label)
        )
        """,
        "CREATE INDEX IF NOT EXISTS events_kind_ts ON events (kind, ts)",
    ],
    9: [
        """
        CREATE TABLE IF NOT EXISTS carry_paper (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_ts INTEGER NOT NULL,            -- unix seconds
            base TEXT NOT NULL,
            symbol TEXT NOT NULL,                  -- ccxt symbol, e.g. WIF/USDT:USDT
            long_venue TEXT NOT NULL,
            short_venue TEXT NOT NULL,
            qty REAL NOT NULL,                     -- coins, same on both legs
            notional_usd REAL NOT NULL,
            long_entry REAL NOT NULL,
            short_entry REAL NOT NULL,
            entry_trailing_spread_apr REAL,        -- decimal, held direction
            entry_current_spread_apr REAL,
            entry_flags TEXT,                      -- JSON array of scan flags at entry
            entry_tradable INTEGER,                -- 1 when funding_spread_scan's tradable_now held in this direction at open
            leverage REAL NOT NULL,
            stop_move REAL NOT NULL,               -- 1 / leverage
            lookback_days REAL NOT NULL,
            exit_apr REAL NOT NULL,
            slippage REAL NOT NULL DEFAULT 0.0005,
            status TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'closed' | 'cancelled'
            closed_ts INTEGER,                     -- economic close: the stop moment when stopped, else the close call
            stop_ts INTEGER,                       -- unix seconds the stop triggered (from bar data), if it did
            long_exit REAL,
            short_exit REAL,
            funding_pnl REAL,                      -- USD
            price_pnl REAL,
            costs REAL,
            pnl_usd REAL,
            return_on_notional REAL,               -- fraction of notional_usd
            max_up REAL,
            max_down REAL,
            stopped INTEGER,
            exit_reason TEXT,                      -- 'signal' | 'stop' | 'manual'
            journal_id INTEGER,
            tags TEXT,                             -- JSON array of strings
            note TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS carry_paper_status_opened ON carry_paper (status, opened_ts)",
    ],
    10: [
        # One CoinGecko /coins/markets snapshot a day: a point-in-time record of supply that the provider's own
        # history (retroactively corrected, supply history Enterprise-only) cannot give.
        """
        CREATE TABLE IF NOT EXISTS supply_snapshot (
            day TEXT NOT NULL,                     -- UTC date of the snapshot, YYYY-MM-DD
            coin_id TEXT NOT NULL,                 -- CoinGecko id
            ts INTEGER NOT NULL,                   -- unix seconds of the snapshot
            symbol TEXT NOT NULL,                  -- upper-case ticker as CoinGecko reports it
            name TEXT,
            market_cap_rank INTEGER,
            price REAL,                            -- USD
            market_cap REAL,                       -- USD
            fdv REAL,                              -- USD
            volume_24h REAL,                       -- USD
            circulating_supply REAL,               -- coins
            total_supply REAL,
            max_supply REAL,
            last_updated TEXT,                     -- CoinGecko's own timestamp of the row
            PRIMARY KEY (day, coin_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS supply_snapshot_symbol_day ON supply_snapshot (symbol, day)",
    ],
}
