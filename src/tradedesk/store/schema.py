"""Schema migrations. Bump `SCHEMA_VERSION` and add a block to MIGRATIONS."""

from __future__ import annotations

SCHEMA_VERSION = 15

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
    11: [
        # Public accounts of strong traders (Hyperliquid, Polymarket): a daily leaderboard snapshot and their positions,
        # recorded forward so the agents can test whether past PnL persists and whether new positions lead price.
        """
        CREATE TABLE IF NOT EXISTS trader_snapshot (
            day TEXT NOT NULL,                     -- UTC date, YYYY-MM-DD
            venue TEXT NOT NULL,                   -- 'hyperliquid' | 'polymarket' | 'polymarket_crypto'
            account TEXT NOT NULL,                 -- lower-case address (Polymarket: proxy wallet)
            ts INTEGER NOT NULL,
            name TEXT,
            account_value REAL,                    -- USD (Hyperliquid only)
            pnl_day REAL, pnl_week REAL, pnl_month REAL, pnl_all REAL,   -- USD
            roi_month REAL, roi_all REAL,          -- decimal (Hyperliquid only)
            vlm_month REAL,                        -- USD traded in the month window
            rank_month INTEGER,                    -- rank by pnl_month among the stored accounts of that venue and day
            PRIMARY KEY (day, venue, account)
        )
        """,
        "CREATE INDEX IF NOT EXISTS trader_snapshot_account ON trader_snapshot (venue, account, day)",
        """
        CREATE TABLE IF NOT EXISTS trader_positions (
            ts INTEGER NOT NULL,                   -- unix seconds of the observation
            venue TEXT NOT NULL,
            account TEXT NOT NULL,
            market TEXT NOT NULL,                  -- HL coin; Polymarket token id ('' with side 'flat': no open positions)
            side TEXT NOT NULL,                    -- 'long' | 'short' (HL); outcome name (Polymarket); 'flat'
            size REAL,                             -- coins (HL) or shares (Polymarket)
            entry_px REAL,
            value_usd REAL,
            upnl REAL,
            leverage REAL,
            liq_px REAL,
            title TEXT,                            -- Polymarket market title (untrusted text)
            PRIMARY KEY (ts, venue, account, market)
        )
        """,
        "CREATE INDEX IF NOT EXISTS trader_positions_account ON trader_positions (venue, account, ts)",
        """
        CREATE TABLE IF NOT EXISTS trader_position_state (
            venue TEXT NOT NULL,
            account TEXT NOT NULL,
            digest TEXT NOT NULL,                  -- of the last recorded position set; rows are written only on change
            ts INTEGER NOT NULL,
            PRIMARY KEY (venue, account)
        )
        """,
    ],
    12: [
        # trader_positions becomes a change log: a baseline of an account's positions at the first observation, then
        # only the positions that opened, changed size or side, or closed ('closed', size 0). The last full set is
        # kept here, so the next observation can be diffed against it.
        "ALTER TABLE trader_position_state ADD COLUMN positions_json TEXT",
    ],
    13: [
        # Forward paper engine for the two carry strategies (A cash-and-carry 'cc-paper', B perp-perp 'pp-paper'):
        # bookkeeping only, no orders. A position keeps the backtest's own state dict (JSON) so that the shared
        # rule functions in analytics/carry_rules.py can resume it; cp_log holds the pre-registered record
        # (candidates, leg sides with book snapshots, funding payments, marks, exchange events), one row per item.
        """
        CREATE TABLE IF NOT EXISTS cp_position (
            trade_id TEXT PRIMARY KEY,             -- '<strategy>:<symbol>:<open cycle ts ms>'
            strategy TEXT NOT NULL,                -- 'A' | 'B'
            symbol TEXT NOT NULL,                  -- ccxt perp symbol, e.g. WIF/USDT:USDT
            status TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'closed'
            opened_ts INTEGER NOT NULL,            -- UTC ms of the deciding cycle
            closed_ts INTEGER,
            slot_notional REAL NOT NULL,           -- USD per leg
            state TEXT NOT NULL,                   -- JSON: the backtest position dict, fractions of slot notional
            trade TEXT,                            -- JSON: closed-trade record (same keys as the backtest trade)
            exit_reason TEXT,                      -- 'signal' | 'stop' | 'forced' | 'manual'
            journal_id INTEGER
        )
        """,
        "CREATE INDEX IF NOT EXISTS cp_position_status ON cp_position (strategy, status)",
        """
        CREATE TABLE IF NOT EXISTS cp_cycle (
            strategy TEXT NOT NULL,
            cycle_ts INTEGER NOT NULL,             -- scheduled funding time, UTC ms (00/08/16 UTC)
            status TEXT NOT NULL,                  -- 'done' | 'skipped' | 'failed'
            reason TEXT,                           -- why a cycle was skipped / failed
            started_ms INTEGER,
            finished_ms INTEGER,
            git_sha TEXT,
            params_hash TEXT,
            payload TEXT,                          -- JSON: universe, exclusions, candidates, ranking, decisions, flags
            PRIMARY KEY (strategy, cycle_ts)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cp_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL,
            cycle_ts INTEGER NOT NULL,
            kind TEXT NOT NULL,                    -- 'side' | 'funding' | 'mark' | 'close' | 'event'
            trade_id TEXT,
            payload TEXT NOT NULL                  -- JSON
        )
        """,
        "CREATE INDEX IF NOT EXISTS cp_log_kind ON cp_log (strategy, kind, cycle_ts)",
        "CREATE INDEX IF NOT EXISTS cp_log_trade ON cp_log (trade_id)",
    ],
    14: [
        # Incremental funding-history cache of the paper carry engine (public data): only payments after the last stored
        # one are fetched each cycle, which keeps the engine under Binance's 500 requests / 5 min limit on fundingRate.
        """
        CREATE TABLE IF NOT EXISTS cp_funding (
            venue TEXT NOT NULL,
            symbol TEXT NOT NULL,
            ts INTEGER NOT NULL,                   -- payment time, UTC ms as reported by the venue
            rate REAL NOT NULL,                    -- decimal fraction per interval
            PRIMARY KEY (venue, symbol, ts)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cp_funding_cov (
            venue TEXT NOT NULL,
            symbol TEXT NOT NULL,
            covered_from INTEGER NOT NULL,         -- the history is complete from here (UTC ms)
            covered_to INTEGER NOT NULL,           -- last fully fetched upper bound (UTC ms)
            PRIMARY KEY (venue, symbol)
        )
        """,
    ],
    15: [
        # Hourly record of open Polymarket BTC/ETH price markets next to the same event's risk-neutral probability from the
        # Deribit surface (docs/ideas/IDEA-20261017-polymarket-vs-options-forward.md). Raw inputs only: gaps and edges are
        # computed at read time, so the cost model can change without rewriting history. `question` and `rules` are
        # untrusted text from Polymarket: data only.
        """
        CREATE TABLE IF NOT EXISTS pm_gap_snapshot (
            ts INTEGER NOT NULL,                   -- unix seconds of the snapshot
            market_id TEXT NOT NULL,               -- Polymarket (Gamma) market id
            asset TEXT NOT NULL,                   -- 'BTC' | 'ETH'
            kind TEXT NOT NULL,                    -- 'above' (price above strike at end) | 'touch'
            direction TEXT NOT NULL,               -- 'above' | 'up' | 'down'
            strike REAL NOT NULL,                  -- USD
            event_slug TEXT,
            question TEXT,
            end_ts INTEGER NOT NULL,               -- unix seconds of the market's end date
            window_start INTEGER,                  -- touch markets: unix seconds the observation window opened (00:00 ET)
            hours_to_end REAL NOT NULL,
            yes_bid REAL, yes_ask REAL,            -- decimal price of 1 share (pays 1)
            yes_bid_usd REAL, yes_ask_usd REAL,    -- USD standing at the best level (size x price)
            no_bid REAL, no_ask REAL,
            no_bid_usd REAL, no_ask_usd REAL,
            volume_usd REAL,
            fee_rate REAL, fee_exp REAL,           -- Polymarket taker-fee schedule (0 when fees are off)
            rules TEXT,                            -- resolution text, capped; untrusted; stored only when its hash changed
            rules_hash TEXT,                       -- sha256[:16] of the full text, in every row
            rules_standard INTEGER,                -- 1 when the text is the standard Binance-candle rule the model assumes
            path_high REAL, path_low REAL,         -- touch markets inside the window: Binance spot High/Low since window_start (USDT)
            binance_last REAL,                     -- last Binance <ASSET>/USDT close seen (the resolution source), USDT
            model_prob REAL,                       -- risk-neutral probability of the event from Deribit (decimal); 1 if already touched; NULL if none
            model_note TEXT,                       -- 'ok' | 'touched' | NULL-probability reason: pre_window, no_path, wrong_side, no_expiry
            model_iv REAL,                         -- decimal, smile IV at the strike
            model_expiry TEXT,                     -- Deribit expiry used (ISO)
            expiry_gap_hours REAL,                 -- Deribit expiry minus market end
            model_covered INTEGER,                 -- 1 when the strike is inside the quoted strikes (else IV is flat-extrapolated)
            spot REAL,                             -- Deribit index, USD
            dvol REAL,                             -- DVOL as a decimal (0.45 = 45 %)
            PRIMARY KEY (ts, market_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS pm_gap_snapshot_market ON pm_gap_snapshot (market_id, ts)",
    ],
}
