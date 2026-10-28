"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    http_timeout: float = 10.0
    http_retries: int = 2
    # Multiplies every cache TTL. 0 disables caching (useful in tests / debugging).
    cache_scale: float = 1.0
    # Transport used by `tradedesk-mcp` (stdio for local, streamable-http for a server).
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
    # Optional keys; a provider without its key reports `missing: config` instead of failing.
    coinalyze_api_key: str | None = None
    coingecko_api_key: str | None = None
    coingecko_plan: str = "demo"  # "demo" or "pro"
    # The Graph gateway (providers/thegraph.py; research scripts, no tool). The key lives in ~/.config/tradedesk/onchain.env,
    # which the MCP service does not load: a script reads it there and passes it in. The Polymarket P&L subgraph id is
    # not pinned in code (read it off Graph Explorer).
    graph_api_key: str | None = field(default=None, repr=False)
    graph_pnl_subgraph_id: str | None = None
    # Local SQLite history (Phase 2). Empty string disables the store entirely.
    db_path: str = "data/tradedesk.sqlite"
    collector_enabled: bool = False
    # Collection cadences in seconds.
    collect_atm_term_seconds: int = 300
    collect_funding_seconds: int = 3600
    collect_basis_seconds: int = 300
    retention_atm_term_days: int = 365
    retention_funding_days: int = 365
    retention_basis_days: int = 365
    retention_seconds: int = 86_400  # how often to run the retention sweep
    # Comma-separated bearer tokens for the streamable-http transport. Empty = auth disabled.
    http_bearer_tokens: str = ""
    # Liquidations collector (WebSocket)
    collect_liquidations_enabled: bool = False
    collect_liquidations_venues: str = "binance,bybit,okx"  # comma-separated subset
    collect_liquidations_window_seconds: int = 60
    collect_liquidations_restart_seconds: int = 5
    collect_liquidations_bybit_symbols: str = "BTCUSDT,ETHUSDT,SOLUSDT"
    collect_liquidations_okx_inst_type: str = "SWAP"
    # Historical OHLCV collector
    collect_ohlcv_hist_enabled: bool = False
    collect_ohlcv_hist_markets: str = ""  # "venue:symbol:timeframe,..."
    collect_ohlcv_hist_seconds: int = 3600
    collect_ohlcv_hist_backfill_days: int = 365
    retention_ohlcv_hist_days: int = 0  # 0 = never prune ohlcv_hist
    # Daily CoinGecko supply snapshot (top `pages` x 250 coins by market cap); on whenever the collector is.
    collect_supply_enabled: bool = True
    collect_supply_seconds: int = 86_400
    collect_supply_pages: int = 4
    # Strong traders' public accounts (Hyperliquid leaderboard + positions, Polymarket leaderboard + positions).
    collect_traders_enabled: bool = True
    collect_traders_seconds: int = 86_400  # leaderboards
    collect_traders_positions_seconds: int = 3_600
    traders_top_n: int = 100  # watched per venue and per ranking (month PnL, all-time PnL)
    traders_min_account_usd: float = 500_000.0  # Hyperliquid accounts stored in the daily snapshot
    traders_pm_pages: int = 10  # Polymarket leaderboard pages of 50 per ranking
    # Forward paper engine for the two carry strategies (docs/ideas/IDEA-20261017-forward-paper-carry.md): bookkeeping only.
    # Off by default; the job polls and runs one cycle after each 8h funding payment.
    paper_carry_enabled: bool = False
    paper_carry_seconds: int = 600
    # Assistant snapshot (docs/ideas/IDEA-20261018-assistant.md): one JSON file for the dashboard, read-only data. Off by default.
    # Empty path = <directory of db_path>/assistant/snapshot.json.
    assistant_snapshot_enabled: bool = False
    assistant_snapshot_seconds: int = 600
    assistant_snapshot_path: str = ""
    # Polymarket <-> Deribit options gap collector (docs/ideas/IDEA-20261017-polymarket-vs-options-forward.md). Off by default.
    pm_gap_enabled: bool = False
    pm_gap_seconds: int = 3600
    # Paper recommendation journal (same card): once a day after 09:00 UTC the job records the method's pick as a paper trade
    # (source 'method') and writes <directory of db_path>/assistant/paper_state.json. Off by default.
    assistant_paper_enabled: bool = False
    assistant_paper_seconds: int = 3600
    assistant_paper_deposit_usd: float = 10_000.0
    assistant_paper_risk_pct: float = 1.0
    # Trade-plan scoring (tools/trade_plan.py): the job resolves the assistant's open plans on candles. Off by default.
    plan_resolve_enabled: bool = False
    plan_resolve_seconds: int = 3600
    # Forecast journal (tools/forecast_log.py): once a day after 09:00 UTC the job records the risk-neutral range forecasts
    # of BTC and ETH and settles the matured ones at the Deribit delivery price. Off by default.
    forecast_log_enabled: bool = False
    forecast_log_seconds: int = 3600
    # Radar archive (collector/radar_archive.py): after 20:00 UTC (60 min lag) the job stores the Deribit surface of BTC and ETH at
    # 19:00 UTC (table radar_surface) and fills missing days of the last week. Off by default.
    collect_radar_enabled: bool = False
    collect_radar_seconds: int = 3600
    # Deribit <-> Bybit daily option quotes (collector/option_xvenue.py, IDEA-20261023-bybit-deribit-daily-option-dislocation-
    # forward): hourly (this cadence), nearest expiry 2-20 h away, ATM +- 3 strikes; plus ATM +- 2 every minute for 30 min after
    # a registered (ATM +- 1) crossing (the job ticks every 60 s). Off by default (switch on only after the threshold is registered, card
    # condition 3). Retention like atm_term / funding_hist / basis_hist.
    collect_option_xvenue_enabled: bool = False
    collect_option_xvenue_seconds: int = 3600
    retention_option_xvenue_days: int = 365
    # Coinalyze daily history of Binance BTC/ETH perps (collector/coinalyze_history.py): OI in USD, funding, long/short ratio.
    # Needs COINALYZE_API_KEY (no key: the job does nothing). Backfills at most 120 days per series per run, from
    # `backfill_days` ago (values below 1 count as 1: only new days; never older than the retention window when one is
    # set, so the sweep and the backfill do not chase each other). Retention 0 = never pruned: the table is tiny
    # (2 symbols x 3 series x 365 ~ 2.2k rows a year) and years of daily history are the point of it.
    collect_coinalyze_history_enabled: bool = False
    collect_coinalyze_history_seconds: int = 3600
    collect_coinalyze_history_backfill_days: int = 1460
    retention_coinalyze_history_days: int = 0
    # Binance TradFi perps best bid/ask + mark/index every minute (collector/tradfi_spread.py; IDEA-20261024-tradfi-perp-
    # offhours-reopen, early cost check). Two all-symbol requests a tick (weight 15), ~200 rows a minute (~0.3 M a day).
    # Off by default: switch on only after the critic approves. Retention like option_xvenue.
    collect_tradfi_spread_enabled: bool = False
    collect_tradfi_spread_seconds: int = 60
    retention_tradfi_spread_days: int = 365
    # Hyperliquid OI-cap state (collector/hl_oi_cap.py; IDEA-20261028-hl-oi-cap-state-forward, step 0): one snapshot per UTC
    # hour of perpsAtOpenInterestCap + metaAndAssetCtxs (2 requests). The job ticks every `_seconds` and fetches only while
    # the current hour has no snapshot, so restarts and a failed attempt do not lose the hour. No retention: forward-only
    # history (~180 listed perps, ~4.3k rows a day). Off by default.
    collect_hl_oi_cap_enabled: bool = False
    collect_hl_oi_cap_seconds: int = 600
    # Alerts
    alerts_enabled: bool = False
    alerts_rules_path: str = ""
    alerts_seconds: int = 60
    alerts_default_cooldown: int = 1800  # 30 min
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_topic_alerts: int | None = None  # forum topic (message_thread_id) for market alerts

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            http_timeout=_float("TRADEDESK_HTTP_TIMEOUT", 10.0),
            http_retries=int(_float("TRADEDESK_HTTP_RETRIES", 2)),
            cache_scale=_float("TRADEDESK_CACHE_SCALE", 1.0),
            transport=os.environ.get("TRADEDESK_TRANSPORT", "stdio"),
            host=os.environ.get("TRADEDESK_HOST", "127.0.0.1"),
            port=int(_float("TRADEDESK_PORT", 8000)),
            coinalyze_api_key=os.environ.get("COINALYZE_API_KEY") or None,
            coingecko_api_key=os.environ.get("COINGECKO_API_KEY") or None,
            coingecko_plan=os.environ.get("COINGECKO_PLAN", "demo").lower(),
            # stripped: a trailing space or \r from an env file would make the header invalid (a `network` error, not `auth`);
            # non-ASCII keys are refused by the provider as `config` (providers/thegraph.key_problem)
            graph_api_key=(os.environ.get("GRAPH_API_KEY") or "").strip() or None,
            graph_pnl_subgraph_id=os.environ.get("TRADEDESK_GRAPH_PNL_SUBGRAPH_ID") or None,
            db_path=os.environ.get("TRADEDESK_DB_PATH", "data/tradedesk.sqlite"),
            collector_enabled=os.environ.get("TRADEDESK_COLLECTOR_ENABLED", "").lower() in ("1", "true", "yes"),
            collect_atm_term_seconds=int(_float("TRADEDESK_COLLECT_ATM_TERM_SECONDS", 300)),
            collect_funding_seconds=int(_float("TRADEDESK_COLLECT_FUNDING_SECONDS", 3600)),
            collect_basis_seconds=int(_float("TRADEDESK_COLLECT_BASIS_SECONDS", 300)),
            retention_atm_term_days=int(_float("TRADEDESK_RETENTION_ATM_TERM_DAYS", 365)),
            retention_funding_days=int(_float("TRADEDESK_RETENTION_FUNDING_DAYS", 365)),
            retention_basis_days=int(_float("TRADEDESK_RETENTION_BASIS_DAYS", 365)),
            retention_seconds=int(_float("TRADEDESK_RETENTION_SECONDS", 86_400)),
            collect_liquidations_enabled=os.environ.get("TRADEDESK_COLLECT_LIQUIDATIONS_ENABLED", "").lower() in ("1", "true", "yes"),
            collect_liquidations_venues=os.environ.get("TRADEDESK_COLLECT_LIQUIDATIONS_VENUES", "binance,bybit,okx"),
            collect_liquidations_window_seconds=int(_float("TRADEDESK_COLLECT_LIQUIDATIONS_WINDOW_SECONDS", 60)),
            collect_liquidations_restart_seconds=int(_float("TRADEDESK_COLLECT_LIQUIDATIONS_RESTART_SECONDS", 5)),
            collect_liquidations_bybit_symbols=os.environ.get("TRADEDESK_COLLECT_LIQUIDATIONS_BYBIT_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT"),
            collect_liquidations_okx_inst_type=os.environ.get("TRADEDESK_COLLECT_LIQUIDATIONS_OKX_INST_TYPE", "SWAP"),
            collect_ohlcv_hist_enabled=os.environ.get("TRADEDESK_COLLECT_OHLCV_HIST_ENABLED", "").lower() in ("1", "true", "yes"),
            collect_ohlcv_hist_markets=os.environ.get("TRADEDESK_COLLECT_OHLCV_HIST_MARKETS", ""),
            collect_ohlcv_hist_seconds=int(_float("TRADEDESK_COLLECT_OHLCV_HIST_SECONDS", 3600)),
            collect_ohlcv_hist_backfill_days=int(_float("TRADEDESK_OHLCV_HIST_BACKFILL_DAYS", 365)),
            retention_ohlcv_hist_days=int(_float("TRADEDESK_RETENTION_OHLCV_HIST_DAYS", 0)),
            collect_supply_enabled=os.environ.get("TRADEDESK_COLLECT_SUPPLY_ENABLED", "1").lower() in ("1", "true", "yes"),
            collect_supply_seconds=int(_float("TRADEDESK_COLLECT_SUPPLY_SECONDS", 86_400)),
            collect_supply_pages=int(_float("TRADEDESK_COLLECT_SUPPLY_PAGES", 4)),
            collect_traders_enabled=os.environ.get("TRADEDESK_COLLECT_TRADERS_ENABLED", "1").lower() in ("1", "true", "yes"),
            collect_traders_seconds=int(_float("TRADEDESK_COLLECT_TRADERS_SECONDS", 86_400)),
            collect_traders_positions_seconds=int(_float("TRADEDESK_COLLECT_TRADERS_POSITIONS_SECONDS", 3_600)),
            traders_top_n=int(_float("TRADEDESK_TRADERS_TOP_N", 100)),
            traders_min_account_usd=_float("TRADEDESK_TRADERS_MIN_ACCOUNT_USD", 500_000.0),
            traders_pm_pages=int(_float("TRADEDESK_TRADERS_PM_PAGES", 10)),
            paper_carry_enabled=os.environ.get("TRADEDESK_PAPER_CARRY_ENABLED", "").lower() in ("1", "true", "yes"),
            paper_carry_seconds=int(_float("TRADEDESK_PAPER_CARRY_SECONDS", 600)),
            assistant_snapshot_enabled=os.environ.get("TRADEDESK_ASSISTANT_SNAPSHOT_ENABLED", "").lower() in ("1", "true", "yes"),
            assistant_snapshot_seconds=int(_float("TRADEDESK_ASSISTANT_SNAPSHOT_SECONDS", 600)),
            assistant_snapshot_path=os.environ.get("TRADEDESK_ASSISTANT_SNAPSHOT_PATH", ""),
            pm_gap_enabled=os.environ.get("TRADEDESK_PM_GAP_ENABLED", "").lower() in ("1", "true", "yes"),
            pm_gap_seconds=int(_float("TRADEDESK_PM_GAP_SECONDS", 3600)),
            assistant_paper_enabled=os.environ.get("TRADEDESK_ASSISTANT_PAPER_ENABLED", "").lower() in ("1", "true", "yes"),
            assistant_paper_seconds=int(_float("TRADEDESK_ASSISTANT_PAPER_SECONDS", 3600)),
            assistant_paper_deposit_usd=_float("TRADEDESK_ASSISTANT_PAPER_DEPOSIT_USD", 10_000.0),
            assistant_paper_risk_pct=_float("TRADEDESK_ASSISTANT_PAPER_RISK_PCT", 1.0),
            # TRADEDESK_PAPER_ prefix: already on the deploy/features.env allow-list (src/tradedesk/features.py)
            plan_resolve_enabled=os.environ.get("TRADEDESK_PAPER_PLAN_RESOLVE_ENABLED", "").lower() in ("1", "true", "yes"),
            plan_resolve_seconds=int(_float("TRADEDESK_PAPER_PLAN_RESOLVE_SECONDS", 3600)),
            forecast_log_enabled=os.environ.get("TRADEDESK_PAPER_FORECAST_LOG_ENABLED", "").lower() in ("1", "true", "yes"),
            forecast_log_seconds=int(_float("TRADEDESK_PAPER_FORECAST_LOG_SECONDS", 3600)),
            collect_radar_enabled=os.environ.get("TRADEDESK_COLLECT_RADAR_ENABLED", "").lower() in ("1", "true", "yes"),
            collect_radar_seconds=int(_float("TRADEDESK_COLLECT_RADAR_SECONDS", 3600)),
            collect_option_xvenue_enabled=os.environ.get("TRADEDESK_COLLECT_OPTION_XVENUE_ENABLED", "").lower() in ("1", "true", "yes"),
            collect_option_xvenue_seconds=int(_float("TRADEDESK_COLLECT_OPTION_XVENUE_SECONDS", 3600)),
            retention_option_xvenue_days=int(_float("TRADEDESK_RETENTION_OPTION_XVENUE_DAYS", 365)),
            collect_coinalyze_history_enabled=os.environ.get("TRADEDESK_COLLECT_COINALYZE_HISTORY_ENABLED", "").lower()
            in ("1", "true", "yes"),
            collect_coinalyze_history_seconds=int(_float("TRADEDESK_COLLECT_COINALYZE_HISTORY_SECONDS", 3600)),
            collect_coinalyze_history_backfill_days=int(_float("TRADEDESK_COLLECT_COINALYZE_HISTORY_BACKFILL_DAYS", 1460)),
            retention_coinalyze_history_days=int(_float("TRADEDESK_RETENTION_COINALYZE_HISTORY_DAYS", 0)),
            collect_tradfi_spread_enabled=os.environ.get("TRADEDESK_COLLECT_TRADFI_SPREAD_ENABLED", "").lower() in ("1", "true", "yes"),
            collect_tradfi_spread_seconds=int(_float("TRADEDESK_COLLECT_TRADFI_SPREAD_SECONDS", 60)),
            retention_tradfi_spread_days=int(_float("TRADEDESK_RETENTION_TRADFI_SPREAD_DAYS", 365)),
            collect_hl_oi_cap_enabled=os.environ.get("TRADEDESK_COLLECT_HL_OI_CAP_ENABLED", "").lower() in ("1", "true", "yes"),
            collect_hl_oi_cap_seconds=int(_float("TRADEDESK_COLLECT_HL_OI_CAP_SECONDS", 600)),
            http_bearer_tokens=os.environ.get("TRADEDESK_HTTP_BEARER_TOKENS") or os.environ.get("TRADEDESK_HTTP_BEARER_TOKEN", ""),
            alerts_enabled=os.environ.get("TRADEDESK_ALERTS_ENABLED", "").lower() in ("1", "true", "yes"),
            alerts_rules_path=os.environ.get("TRADEDESK_ALERTS_RULES_PATH", ""),
            alerts_seconds=int(_float("TRADEDESK_ALERTS_SECONDS", 60)),
            alerts_default_cooldown=int(_float("TRADEDESK_ALERTS_DEFAULT_COOLDOWN", 1800)),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
            telegram_topic_alerts=int(os.environ["TELEGRAM_TOPIC_ALERTS"])
            if os.environ.get("TELEGRAM_TOPIC_ALERTS", "").isdigit()
            else None,
        )
