"""Schema migrations. Bump `SCHEMA_VERSION` and add a block to MIGRATIONS."""

from __future__ import annotations

SCHEMA_VERSION = 34

# Each migration is a list of SQL statements executed in a single transaction.
# `PRAGMA user_version` is bumped by Store.init() inside the same explicit transaction,
# so a block that fails half-way leaves neither its tables/columns nor the version bump.
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
    16: [
        # Paper trades of the assistant's option recommendations (bookkeeping only, no orders). Deliberately NOT
        # mirrored into `journal`: that table holds the owner's real trades, and a hypothesis must not mix with them.
        """
        CREATE TABLE IF NOT EXISTS rec_paper (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_ts INTEGER NOT NULL,            -- unix seconds
            currency TEXT NOT NULL,
            structure TEXT NOT NULL,               -- key of option_replay.STRUCTURES
            expiry_ts INTEGER NOT NULL,            -- unix seconds, 08:00 UTC delivery
            spot REAL NOT NULL,                    -- USD at entry
            forward REAL NOT NULL,
            t_years REAL NOT NULL,
            legs TEXT NOT NULL,                    -- JSON [[strike, is_call, qty, exec_iv, entry_price_usd], ...]
            premium_per_btc REAL NOT NULL,         -- USD per 1 BTC of structure, debit > 0
            entry_fees_per_btc REAL NOT NULL,
            max_loss_per_btc REAL NOT NULL,
            risk_basis TEXT NOT NULL,              -- 'max_loss' | 'stress_up_30pct'
            size_btc REAL NOT NULL,
            deposit_usd REAL NOT NULL,
            risk_pct REAL NOT NULL,                -- percent of the deposit
            risk_usd REAL NOT NULL,                -- max_loss_per_btc * size_btc
            within_risk INTEGER NOT NULL,
            label TEXT NOT NULL DEFAULT 'hypothesis',
            status TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'settled'
            settled_ts INTEGER,
            delivery_price REAL,
            pnl_usd REAL,
            pnl_pct_spot REAL,                     -- fraction: P&L per 1 BTC of structure / entry spot
            r_worst_case REAL,                     -- P&L / worst-loss risk of the trade (NOT the study's R)
            r_study REAL,                          -- per-unit P&L / (premium for a debit, |method_q05| * spot for a credit)
            source TEXT NOT NULL DEFAULT 'manual', -- 'method' (picked by the method) | 'manual' (picked by a person/model)
            method_mean_pct REAL,                  -- method simulation mean at entry, fraction of the entry FORWARD (as method_summary returns it)
            method_q05_pct REAL,                   -- method simulation 5 % quantile at entry, fraction of the entry FORWARD (as method_summary returns it)
            regime TEXT,                           -- method regime bucket at entry
            trigger TEXT,                          -- why the job booked it: 'chain' (first / after expiry; H1-comparable) | 'regime' | 'event'; NULL for manual
            note TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS rec_paper_status_expiry ON rec_paper (status, expiry_ts)",
    ],
    17: [
        # Every trade plan the assistant gives (whatever its verdict), scored later on candles (analytics/trade_plan.py):
        # a forward record of the assistant's judgment. Bookkeeping only, no orders; the owner's real trades stay in
        # `journal` (linked through journal_id when he traded the plan).
        """
        CREATE TABLE IF NOT EXISTS trade_plan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_ts INTEGER NOT NULL,           -- unix seconds; always the time of plan_add (no backdating)
            exchange TEXT NOT NULL,                -- ccxt id the candles come from
            symbol TEXT NOT NULL,                  -- ccxt symbol (options: the underlying)
            instrument TEXT NOT NULL,              -- 'perp' | 'spot' | 'option'
            side TEXT NOT NULL,                    -- 'long' | 'short'
            entry_type TEXT NOT NULL,              -- 'market' | 'limit' | 'stop'
            entry REAL NOT NULL,
            stop REAL NOT NULL,
            target REAL NOT NULL,
            expires_ts INTEGER NOT NULL,           -- plan horizon, unix seconds
            verdict TEXT NOT NULL,                 -- 'enter' | 'wait' | 'skip' (checklist ✅ / ⏳ / ❌)
            thesis TEXT,
            rules TEXT,                            -- JSON {rule number: 'pass' | 'fail' | 'warn'}
            tags TEXT,                             -- JSON array of strings
            rec_paper_id INTEGER,                  -- option plans: the linked rec_paper row
            journal_id INTEGER,                    -- set when the owner actually traded the plan
            status TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'resolved'
            outcome TEXT,                          -- 'target' | 'stop' | 'expired' | 'not_filled'
            filled_ts INTEGER,
            fill REAL,                             -- paper fill price (entry, or the open after a gap through a stop entry)
            resolved_ts INTEGER,
            exit REAL,
            r_result REAL,                         -- net R: move / |entry - stop| minus a flat taker fee on both sides
            timeframe TEXT,                        -- candles the plan was scored on
            resolve_note TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS trade_plan_status_expires ON trade_plan (status, expires_ts)",
        "CREATE INDEX IF NOT EXISTS trade_plan_symbol_created ON trade_plan (symbol, created_ts)",
    ],
    18: [
        # Polymarket <-> Deribit gap, registration section 13 (docs/ideas/IDEA-20261017-polymarket-vs-options-forward.md):
        # read times of the two quotes (the 60 s synchronicity filter) and the smile forward; rows before this migration
        # keep NULL (they pass the filter and are counted in the report).
        "ALTER TABLE pm_gap_snapshot ADD COLUMN book_ts INTEGER",  # unix s: when this market's Polymarket book was read
        "ALTER TABLE pm_gap_snapshot ADD COLUMN surface_ts INTEGER",  # unix s: when the Deribit option surface was read
        "ALTER TABLE pm_gap_snapshot ADD COLUMN model_forward REAL",  # USD: forward of the Deribit smile used by the model
        # Settled result of each snapshotted market, fetched once after its end (registration section 7: Gamma
        # outcomePrices; threshold markets also get the Binance 1m closes around the end for the cross-check).
        """
        CREATE TABLE IF NOT EXISTS pm_gap_outcome (
            market_id TEXT PRIMARY KEY,            -- Polymarket (Gamma) market id
            asset TEXT NOT NULL,                   -- 'BTC' | 'ETH'
            kind TEXT NOT NULL,                    -- 'above' | 'touch'
            strike REAL NOT NULL,                  -- USD
            end_ts INTEGER NOT NULL,               -- unix seconds of the market's end
            outcome_yes INTEGER,                   -- 1 YES paid 1, 0 NO paid 1, NULL not settled yet (Gamma outcomePrices)
            outcome_prices TEXT,                   -- raw Gamma outcomePrices of the last attempt (untrusted)
            outcomes TEXT,                         -- raw Gamma outcomes (labels of outcome_prices, untrusted)
            binance_close_end REAL,                -- 'above': close of the Binance <ASSET>/USDT 1m candle closing at end_ts (USDT)
            binance_close_next REAL,               -- 'above': close of the 1m candle opening at end_ts (USDT)
            attempts INTEGER NOT NULL DEFAULT 0,
            checked_ts INTEGER NOT NULL,           -- unix seconds of the last attempt
            resolved_ts INTEGER                    -- unix seconds when outcome_yes was first stored
        )
        """,
    ],
    19: [
        # Demo execution (ADR 0007, stage testnet on the Bybit Demo Trading account): append-only audit log of every
        # decision and venue answer (ADR 0005 p. 6), and the trader's own book of demo trades.
        """
        CREATE TABLE IF NOT EXISTS exec_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,                   -- unix ms
            venue TEXT NOT NULL,                   -- 'bybit-demo'
            kind TEXT NOT NULL,                    -- signal | refused | sent | rejected | filled | closed | error | halt
            link_id TEXT,                          -- orderLinkId (idempotency key of one signal)
            symbol TEXT,
            setup TEXT,                            -- e.g. 'S14'
            payload TEXT                           -- JSON: proposal, violations, venue answer (never keys)
        )
        """,
        "CREATE INDEX IF NOT EXISTS exec_audit_ts ON exec_audit (ts)",
        "CREATE INDEX IF NOT EXISTS exec_audit_link ON exec_audit (link_id)",
        "CREATE TRIGGER IF NOT EXISTS exec_audit_no_update BEFORE UPDATE ON exec_audit BEGIN SELECT RAISE(ABORT, 'exec_audit is append-only'); END",
        "CREATE TRIGGER IF NOT EXISTS exec_audit_no_delete BEFORE DELETE ON exec_audit BEGIN SELECT RAISE(ABORT, 'exec_audit is append-only'); END",
        """
        CREATE TABLE IF NOT EXISTS demo_trade (
            link_id TEXT PRIMARY KEY,              -- orderLinkId sent to the venue
            venue TEXT NOT NULL,
            symbol TEXT NOT NULL,                  -- ccxt unified, e.g. 'BTC/USDT:USDT'
            side TEXT NOT NULL,                    -- 'long' | 'short'
            setup TEXT NOT NULL,                   -- 'S14', 'S16', ...
            timeframe TEXT NOT NULL,               -- candles the signal came from, e.g. '4h'
            signal_ts INTEGER NOT NULL,            -- unix ms: open time of the signal bar
            entry REAL NOT NULL,                   -- planned entry (quote currency)
            stop REAL NOT NULL,
            target REAL NOT NULL,
            qty REAL NOT NULL,                     -- base units
            leverage INTEGER NOT NULL,             -- isolated leverage set on the venue (tier, lowered for the liquidation)
            risk_usd REAL NOT NULL,                -- loss at the stop with both taker fees (USD)
            score INTEGER,                         -- confidence points (analytics/setup_signals.score)
            score_reasons TEXT,                    -- JSON list of the points, Ukrainian
            comment TEXT NOT NULL,                 -- why: setup, indicators and their values, levels (shown to the owner)
            status TEXT NOT NULL,                  -- 'unknown' (written before the order) | 'open' | 'closed' | 'rejected'
            order_id TEXT,                         -- venue order id
            fill REAL,                             -- average entry fill
            opened_ts INTEGER,                     -- unix ms of the fill
            exit REAL,
            closed_ts INTEGER,
            pnl_usd REAL,                          -- venue closed P&L (fees included)
            r_multiple REAL,                       -- pnl_usd / risk_usd
            exit_reason TEXT,                      -- 'target' | 'stop' | 'breakeven' | 'time'
            be_ts INTEGER,                         -- unix ms when the stop was moved to break-even (+30 % ROE rule)
            be_stop REAL,                          -- that break-even stop (entry +- both taker fees)
            journal_id INTEGER,                    -- mirror row in journal (tags demo-paper, setup:<S>)
            plan_id INTEGER                        -- shadow row in trade_plan (same levels, no break-even rule)
        )
        """,
        "CREATE INDEX IF NOT EXISTS demo_trade_status ON demo_trade (status, symbol)",
    ],
    20: [
        # Daily probabilistic range forecasts of the assistant (analytics/forecast_log.py), scored at the expiry delivery.
        # Risk-neutral probabilities from the Deribit smile, a hypothesis under test; kept apart from `journal`.
        """
        CREATE TABLE IF NOT EXISTS forecast_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,                     -- UTC date of issue, YYYY-MM-DD (one forecast per day, asset and kind)
            issued_ts INTEGER NOT NULL,            -- unix seconds
            asset TEXT NOT NULL,                   -- 'BTC' | 'ETH'
            kind TEXT NOT NULL,                    -- 'above_spot' | 'in_1sigma' | 'in_pct_5' | 'in_pct_10'
            expiry_ts INTEGER NOT NULL,            -- unix seconds, Deribit 08:00 UTC delivery the event is settled on
            spot REAL NOT NULL,                    -- Deribit index at issue, USD
            forward REAL NOT NULL,                 -- forward of the expiry at issue, USD
            atm_iv REAL NOT NULL,                  -- decimal, smile IV at the forward
            t_years REAL NOT NULL,                 -- days / 365 at issue
            lo REAL,                               -- USD; NULL = open side
            hi REAL,                               -- USD; NULL = open side
            prob REAL NOT NULL,                    -- risk-neutral P(lo < S_T < hi), decimal
            ref_prob REAL,                         -- fixed reference (0.5 above_spot, 0.6827 in_1sigma); NULL if none
            covered INTEGER NOT NULL,              -- 1 when the bounds lie inside the quoted strikes
            label TEXT NOT NULL DEFAULT 'risk_neutral',
            status TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'settled'
            settled_ts INTEGER,
            delivery_price REAL,                   -- Deribit delivery price, USD
            outcome INTEGER,                       -- 1 the event happened, 0 not
            brier REAL,                            -- (prob - outcome)^2
            UNIQUE (day, asset, kind)
        )
        """,
        "CREATE INDEX IF NOT EXISTS forecast_log_status_expiry ON forecast_log (status, expiry_ts)",
    ],
    21: [
        # Demo trader order types (owner 22.10): maker entries and targets, the market only when the situation asks.
        # New statuses: 'pending' (a resting entry order, not complete yet) and 'missed' (cancelled with nothing filled).
        "ALTER TABLE demo_trade ADD COLUMN entry_order TEXT",  # orderLinkId of the working entry order (NULL = link_id)
        "ALTER TABLE demo_trade ADD COLUMN entry_px REAL",  # its limit price; NULL = a market entry
        "ALTER TABLE demo_trade ADD COLUMN entry_kind TEXT",  # 'maker' | 'taker' | 'mixed' (how the entry filled)
        "ALTER TABLE demo_trade ADD COLUMN tp_link TEXT",  # orderLinkId of the resting reduce-only target limit
        "ALTER TABLE demo_trade ADD COLUMN exit_order TEXT",  # orderLinkId of a calm (time) exit limit
        "ALTER TABLE demo_trade ADD COLUMN exit_px REAL",  # its limit price
        "ALTER TABLE demo_trade ADD COLUMN exit_ts INTEGER",  # unix ms when the calm exit started
        "ALTER TABLE demo_trade ADD COLUMN exit_kind TEXT",  # 'maker' | 'taker' (how the position left)
        "ALTER TABLE demo_trade ADD COLUMN fees_usd REAL",  # open + close fees from the venue's closed-pnl, USD
        "ALTER TABLE demo_trade ADD COLUMN note TEXT",  # why an entry was missed or went to the market
    ],
    22: [
        # Radar archive (docs/ideas/IDEA-20261018-assistant.md, «Радар»): the Deribit surface of BTC and ETH at 19:00 UTC,
        # one row per day and asset (analytics/radar.py; live job `radar_archive` and scripts/radar_backfill.py).
        # Read-only market data, independent of `journal`.
        """
        CREATE TABLE IF NOT EXISTS radar_surface (
            day TEXT NOT NULL,                     -- UTC date YYYY-MM-DD of the 19:00 UTC reference
            asset TEXT NOT NULL,                   -- 'BTC' | 'ETH'
            ts INTEGER NOT NULL,                   -- unix seconds of the reference time (19:00 UTC)
            spot REAL NOT NULL,                    -- Deribit index, USD
            atm_7d REAL,                           -- decimal, ATM IV at a constant 7-day tenor (total variance); NULL = not bracketed
            atm_30d REAL,
            atm_90d REAL,
            atm_180d REAL,
            put_skew_25 REAL,                      -- decimal vol difference, 30 d: IV(25-delta put) - ATM
            rr_25 REAL,                            -- decimal vol difference, 30 d: IV(25-delta call) - IV(25-delta put)
            smile TEXT NOT NULL,                   -- JSON {"10P","25P","35P","ATM","35C","25C","10C": IV decimal | null}, 30 d
            expiries TEXT NOT NULL,                -- JSON [[expiry unix s, days, forward USD, ATM IV decimal | null], ...]
            n_prints INTEGER NOT NULL,             -- option prints in the 6 h window that built the surface
            method TEXT NOT NULL,                  -- analytics.radar.METHOD
            created_ts INTEGER NOT NULL,           -- unix seconds when the row was written
            PRIMARY KEY (day, asset)
        )
        """,
    ],
    23: [
        # Forecast log: the RV30 model beside each risk-neutral forecast (analytics/forecast_log.py), scored on the same outcome.
        "ALTER TABLE forecast_log ADD COLUMN rv30 REAL",  # decimal, trailing 30-day zero-mean realized vol at issue; NULL = none
        "ALTER TABLE forecast_log ADD COLUMN prob_rv REAL",  # P(lo < S_T < hi) of the RV30 model, decimal; NULL = none
        "ALTER TABLE forecast_log ADD COLUMN brier_rv REAL",  # (prob_rv - outcome)^2 once settled
    ],
    24: [
        # The dashboard's Polymarket tab reads `SELECT MAX(ts)` and `WHERE ts = ?`; the (market_id, ts) index cannot serve them.
        "CREATE INDEX IF NOT EXISTS pm_gap_snapshot_ts ON pm_gap_snapshot (ts)",
    ],
    25: [
        # Deribit <-> Bybit quotes of the same daily option (docs/ideas/IDEA-20261023-bybit-deribit-daily-option-dislocation-
        # forward.md; collector/option_xvenue.py): nearest expiry 2-20 h away, ATM +- 3 strikes, calls and puts. Raw quotes
        # only, both venues side by side; the crossing is computed at read time (analytics/option_xvenue.py). A venue
        # without a quote leaves its columns NULL. Read-only market data, independent of `journal`.
        """
        CREATE TABLE IF NOT EXISTS option_xvenue_snapshot (
            ts INTEGER NOT NULL,                   -- unix seconds of the snapshot
            currency TEXT NOT NULL,                -- 'BTC' | 'ETH'
            expiry_ts INTEGER NOT NULL,            -- unix seconds, 08:00 UTC expiry
            tau_hours REAL NOT NULL,               -- hours from ts to expiry_ts
            strike REAL NOT NULL,                  -- USD
            opt TEXT NOT NULL,                     -- 'C' | 'P'
            d_instrument TEXT,                     -- Deribit instrument name
            b_symbol TEXT,                         -- Bybit symbol (e.g. BTC-24OCT26-110000-C-USDT)
            d_bid REAL, d_ask REAL,                -- Deribit best bid/ask, in the coin per 1 coin of underlying (x d_index = USD)
            d_bid_size REAL, d_ask_size REAL,      -- Deribit size at the best level, base-coin units
            d_bid_iv REAL, d_ask_iv REAL, d_mark_iv REAL,  -- decimal
            d_index REAL,                          -- Deribit index (spot), USD
            d_underlying REAL,                     -- Deribit underlying (forward of the expiry), USD
            d_ts_ms INTEGER,                       -- Deribit ticker timestamp, unix ms
            b_bid REAL, b_ask REAL,                -- Bybit best bid/ask, USD(T) per 1 coin of underlying
            b_bid_size REAL, b_ask_size REAL,      -- Bybit size at the best level, base-coin units (unverified contract size)
            b_bid_iv REAL, b_ask_iv REAL, b_mark_iv REAL,  -- decimal
            b_index REAL,                          -- Bybit index price, USD
            b_underlying REAL,                     -- Bybit underlying price of the expiry, USD
            b_ts_ms INTEGER,                       -- Bybit tickers response time, unix ms
            PRIMARY KEY (ts, currency, strike, opt)
        )
        """,
    ],
    26: [
        # option_xvenue minute mode (card «Час життя»): after a crossing the collector reads ATM +- 2 every minute for 30 min.
        # 'hourly' = the registered sample (frequency p), 'minute' = the window rows (episode lifetime). Earlier rows: hourly.
        "ALTER TABLE option_xvenue_snapshot ADD COLUMN mode TEXT NOT NULL DEFAULT 'hourly'",
        # 1 = the row is in its snapshot's registered ATM +- 1 (analytics/option_xvenue.registered_rows, the trigger's set),
        # 0 = read for tracking only (ATM +- 2 / +- 3); NULL on rows written before this column.
        "ALTER TABLE option_xvenue_snapshot ADD COLUMN reg_atm INTEGER",
        # Local receive time of each leg, unix ms (rt.now() right after the response; NULL if the leg failed or came from
        # the in-process cache). d_ts_ms is Deribit's last-change time (0.3-5.7 s before the request, live 23.10), so
        # b_recv_ms - d_recv_ms is the read gap and d_recv_ms - d_ts_ms the age of the Deribit quote.
        "ALTER TABLE option_xvenue_snapshot ADD COLUMN d_recv_ms INTEGER",
        "ALTER TABLE option_xvenue_snapshot ADD COLUMN b_recv_ms INTEGER",
    ],
    27: [
        # Coinalyze daily history of Binance perps (collector/coinalyze_history.py; symbol suffix '.A' = Binance, not an
        # all-exchange sum): open interest in USD, funding and the long/short account ratio. Closed UTC days only; one row
        # per (symbol, metric, ts), rewritten idempotently. Read-only market data, independent of `journal`.
        """
        CREATE TABLE IF NOT EXISTS coinalyze_history (
            symbol TEXT NOT NULL,                  -- Coinalyze symbol, e.g. BTCUSDT_PERP.A
            metric TEXT NOT NULL,                  -- 'oi_usd' | 'funding' | 'long_short'
            ts INTEGER NOT NULL,                   -- unix seconds, START of the daily bar (00:00 UTC)
            open REAL, high REAL, low REAL, close REAL,  -- oi_usd: USD; funding: decimal per funding interval (API % / 100);
                                                   -- NULL for long_short. Funding open/low may carry the previous day's rate.
            ratio REAL,                            -- long_short: longs / shorts (accounts, not notional)
            long_share REAL, short_share REAL,     -- long_short: fractions of accounts in [0, 1]
            fetched_ts INTEGER NOT NULL,           -- unix seconds when the row was written
            PRIMARY KEY (symbol, metric, ts)
        )
        """,
    ],
    28: [
        # Demo trader option leg (owner 23.10, slice 2; execution/option_leg.py): a BTC / ETH setup signal also bought as a
        # long call / put next to the futures trade, compared on the same signal (analytics/demo_option_pairs.py).
        """
        CREATE TABLE IF NOT EXISTS demo_option_leg (
            link_id TEXT PRIMARY KEY,              -- orderLinkId of the first entry order ('tdo-...')
            pair_id TEXT NOT NULL,                 -- demo_trade.link_id of the futures trade of the same signal
            venue TEXT NOT NULL,
            symbol TEXT NOT NULL,                  -- Bybit option symbol, e.g. 'BTC-13NOV26-84000-C-USDT'
            underlying TEXT NOT NULL,              -- ccxt perp the target is read on, e.g. 'BTC/USDT:USDT'
            side TEXT NOT NULL,                    -- the signal's side: 'long' (a call) | 'short' (a put)
            is_call INTEGER NOT NULL,
            strike REAL NOT NULL,
            expiry_ms INTEGER NOT NULL,            -- unix ms, 08:00 UTC
            tick REAL NOT NULL,                    -- premium tick, USDT
            lot_step REAL NOT NULL,                -- contracts (1 contract = 1 coin)
            min_qty REAL NOT NULL,
            setup TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            signal_ts INTEGER NOT NULL,            -- unix ms, open of the signal bar
            target REAL NOT NULL,                  -- the setup's target on the underlying (same level as the futures)
            delta REAL,                            -- Black-76 delta at decision (venue mark IV, r = 0)
            mark_iv REAL,                          -- decimal
            underlying_px REAL,                    -- the expiry's underlying price at decision, USD
            dte_days REAL,                         -- days to expiry at decision
            comment TEXT NOT NULL,                 -- why (shown to the owner); says it is a paired test, not an edge
            status TEXT NOT NULL,                  -- unknown | pending | open | closing | closed | missed | rejected
            placed_ts INTEGER NOT NULL,            -- unix ms of the first entry order
            entry_order TEXT,                      -- orderLinkId of the working entry order (re-placed: link_id-N)
            entry_n INTEGER NOT NULL DEFAULT 0,
            entry_px REAL,                         -- its limit premium, USDT per contract
            qty REAL NOT NULL,                     -- planned contracts; the filled size once open
            risk_usd REAL NOT NULL,                -- premium + fee: planned (guard's fee bound), then the actual at the fill
            fill REAL,                             -- average entry premium, USDT per contract
            fee_open REAL,                         -- actual entry fee from the venue's execution list, USD
            fee_open_bound REAL,                   -- the guard's fee upper bound for the same fill, USD
            opened_ts INTEGER,                     -- unix ms of the complete fill
            exit_reason TEXT,                      -- target | time | expiry | gone
            exit_ts INTEGER,                       -- unix ms when the close started
            exit_phase TEXT,                       -- 'maker' (ask - tick) | 'bid'
            exit_order TEXT,                       -- orderLinkId of the working close order
            exit_n INTEGER NOT NULL DEFAULT 0,
            exit_px REAL,                          -- its limit premium
            exit_kind TEXT,                        -- 'post_only' | 'gtc_fallback' | 'bid' (how the close was sent last)
            exit_links TEXT,                       -- JSON list of every close order's orderLinkId (their fills = the exit)
            exit REAL,                             -- average exit premium
            fee_close REAL,                        -- actual close fees, USD
            closed_ts INTEGER,
            pnl_usd REAL,                          -- exit value - entry value - both fees, USD
            r_multiple REAL,                       -- pnl_usd / risk_usd (premium + fee at entry)
            journal_id INTEGER,                    -- mirror row in journal (tags demo-paper, option)
            note TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS demo_option_leg_status ON demo_option_leg (status)",
        "CREATE INDEX IF NOT EXISTS demo_option_leg_pair ON demo_option_leg (pair_id)",
    ],
    29: [
        # Best bid/ask, mark and index of Binance TradFi perps (US equities/ETFs + commodities), one snapshot a minute
        # (collector/tradfi_spread.py; IDEA-20261024-tradfi-perp-offhours-reopen, early cost check). No depth. ~200 symbols
        # x 1440 a day: WITHOUT ROWID so the (ts, symbol) key is the table itself. Read-only market data.
        """
        CREATE TABLE IF NOT EXISTS tradfi_spread (
            ts INTEGER NOT NULL,                   -- unix seconds of the snapshot (one clock read per tick)
            symbol TEXT NOT NULL,                  -- Binance USD-M symbol, e.g. SPYUSDT
            bid REAL, ask REAL,                    -- best bid / ask, USDT (bookTicker); NULL if that request failed
            mark_price REAL, index_price REAL,     -- USDT (premiumIndex); NULL if that request failed
            book_ms INTEGER,                       -- bookTicker `time` (venue transaction time), unix ms
            premium_ms INTEGER,                    -- premiumIndex `time`, unix ms
            PRIMARY KEY (ts, symbol)
        ) WITHOUT ROWID
        """,
    ],
    30: [
        # Demo trader S12 v7 (owner 26.10, docs/verification/2026-10-26-prelimit-entries.md): a limit resting at the
        # gap's middle until this time (unix ms) instead of the 10-minute maker entry; NULL = the usual entry
        "ALTER TABLE demo_trade ADD COLUMN rest_until INTEGER",
    ],
    31: [
        # Hyperliquid open-interest-cap state, one snapshot per UTC hour of every listed main-dex perp
        # (collector/hl_oi_cap.py, analytics/hl_oi_cap.py; IDEA-20261028-hl-oi-cap-state-forward, step 0). The venue only
        # exposes the current state, so this table is the only history. ~180 listed perps x 24 a day. Read-only market data.
        """
        CREATE TABLE IF NOT EXISTS hl_oi_cap_snapshot (
            ts INTEGER NOT NULL,                   -- unix seconds, start of the UTC hour (the snapshot key)
            coin TEXT NOT NULL,                    -- Hyperliquid ticker, exact case (e.g. kPEPE)
            at_cap INTEGER NOT NULL,               -- 1: in perpsAtOpenInterestCap at fetch time, 0: not
            oi_usd REAL,                           -- openInterest (coins) x markPx, USD
            funding REAL,                          -- hourly funding rate, decimal fraction, as Hyperliquid reports it
            mark REAL, oracle REAL,                -- markPx, oraclePx, USD
            vol24h REAL,                           -- dayNtlVlm, 24 h notional volume, USD
            fetched_ts INTEGER NOT NULL,           -- unix seconds when both answers were in (inside the hour `ts`)
            PRIMARY KEY (ts, coin)
        ) WITHOUT ROWID
        """,
    ],
    32: [
        # Hourly flow snapshot of BTC / ETH on Binance, Bybit, OKX (spot + perp) and Hyperliquid (perp) (collector/
        # flow_snapshot.py, analytics/flow_snapshot.py; IDEA-20261028-market-flow-reader, step 2). Forward-only history of
        # book depth, OI, funding and long/short; 14 rows an hour (336 a day, ~123k a year). Not pruned. Read-only data.
        """
        CREATE TABLE IF NOT EXISTS flow_snapshot (
            ts INTEGER NOT NULL,                   -- unix seconds, start of the UTC hour (the snapshot key)
            base TEXT NOT NULL,                    -- 'BTC' | 'ETH'
            venue TEXT NOT NULL,                   -- 'binance' | 'bybit' | 'okx' | 'hyperliquid'
            market TEXT NOT NULL,                  -- 'spot' (BASE/USDT) | 'perp' (USDT linear perp; Hyperliquid USDC)
            mid REAL,                              -- book mid, USD(T)
            reach_bid REAL, reach_ask REAL,        -- how far the fetched levels went from the mid, decimal (0.01 = 1 %)
            bid_usd_10bp REAL, ask_usd_10bp REAL,  -- quote notional resting within 0.1 % of the mid, USD;
            bid_usd_50bp REAL, ask_usd_50bp REAL,  -- 0.5 %; NULL = the fetched book does not reach the band on that side
            bid_usd_100bp REAL, ask_usd_100bp REAL,  -- 1 % (or, Hyperliquid, its aggregated levels are too coarse)
            oi_coins REAL,                         -- open interest, base coins (perp only)
            oi_usd REAL,                           -- oi_coins x mid, USD
            funding_rate REAL,                     -- current (predicted) rate per interval, decimal fraction
            funding_hours REAL,                    -- funding interval, hours
            funding_annualized REAL,               -- simple: funding_rate x 8760 / funding_hours, decimal
            long_share REAL,                       -- fraction of accounts long (latest 5-min value; not notional)
            missing TEXT,                          -- comma list of the parts that failed (book, oi, funding, ...); NULL = none
            fetched_ts INTEGER NOT NULL,           -- unix seconds of the tick that wrote the row (inside the hour `ts`)
            PRIMARY KEY (ts, base, venue, market)
        ) WITHOUT ROWID
        """,
    ],
    33: [
        # Daily journal of the cycle regime (collector/cycle_regime_log.py, analytics/cycle_regime_journal.py;
        # IDEA-20261028-btc-cycle-regime-gauge, step 2): one row per closed UTC day and base, written from 00:05 UTC.
        # A stored row is never replaced (INSERT OR IGNORE). Forward-only, not pruned. Read-only market data.
        """
        CREATE TABLE IF NOT EXISTS cycle_regime_log (
            date TEXT NOT NULL,                    -- UTC date YYYY-MM-DD of the closed daily bar (closes at 00:00 next day)
            base TEXT NOT NULL,                    -- 'BTC'
            state TEXT NOT NULL,                   -- bottom | accumulation | overheat | growth | decline
            trend TEXT,                            -- 'growth' | 'decline': side of the price against SMA365
            trend_confirmed INTEGER,               -- 1: SMA365's 30-day slope agrees with `trend`, 0: not, NULL: no slope
            close REAL NOT NULL,                   -- Binance spot daily close, USDT
            perp_close REAL,                       -- Binance USD-M perp daily close, USDT
            sma365 REAL,                           -- USDT
            price_to_sma REAL,                     -- decimal: close / sma365 - 1
            sma_slope_30d REAL,                    -- decimal: sma365 / sma365 30 days earlier - 1
            drawdown_365d REAL,                    -- decimal <= 0: close / max(365 closes) - 1
            dist_30d_low REAL,                     -- decimal: close / min(previous 29 closes) - 1
            rsi14 REAL,                            -- Wilder RSI, points 0-100
            basis7 REAL,                           -- decimal: mean of 7 daily perp close / spot close - 1
            funding7 REAL,                         -- decimal a year, simple-annualised mean funding of 7 days
            premium7 REAL,                         -- decimal: mean daily premium-index close (no rule uses it)
            components TEXT NOT NULL,              -- JSON: every component of analytics.cycle_regime (lossless)
            missing TEXT,                          -- comma list of informational sources that failed (premium_index); NULL = none
            version TEXT NOT NULL,                 -- analytics.cycle_regime.VERSION of the rules that labelled the day
            created_ts INTEGER NOT NULL,           -- unix seconds when the row was written
            PRIMARY KEY (date, base)
        ) WITHOUT ROWID
        """,
    ],
    34: [
        # CoinGecko trending list (free /search/trending), one snapshot per UTC hour, one row per listed coin
        # (collector/coingecko_trending.py, analytics/coingecko_trending.py; IDEA-20261028-coingecko-trending-attention-
        # fade-forward, step 0). The API has no history of the list, so this table is the only one. ~15 rows an hour, not
        # pruned. Names and symbols are untrusted display text (cleaned, capped). Read-only market data.
        """
        CREATE TABLE IF NOT EXISTS coingecko_trending (
            ts INTEGER NOT NULL,                   -- unix seconds, start of the UTC hour (the snapshot key)
            rank INTEGER NOT NULL,                 -- 1-based position in the list, as CoinGecko orders it
            coin_id TEXT NOT NULL,                 -- CoinGecko API id, e.g. 'usd-coin'
            coin_num INTEGER,                      -- CoinGecko numeric coin_id
            symbol TEXT, name TEXT,                -- untrusted display text, as listed (symbol case kept)
            market_cap_rank INTEGER,               -- CoinGecko market-cap rank; NULL = unranked
            score INTEGER,                         -- CoinGecko `score` as given (documented: 0-based position)
            price_usd REAL,                        -- data.price, USD
            price_btc REAL,                        -- price_btc, BTC
            change_24h REAL,                       -- data.price_change_percentage_24h.usd / 100, decimal fraction
            market_cap_usd REAL, volume_usd REAL,  -- data.market_cap, data.total_volume, USD (parsed display strings)
            fetched_ts INTEGER NOT NULL,           -- unix seconds when the answer was in (normally inside the hour `ts`)
            PRIMARY KEY (ts, rank)
        ) WITHOUT ROWID
        """,
    ],
}
