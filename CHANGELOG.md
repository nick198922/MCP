# Changelog

## Unreleased
- Три нові ринкові інструменти (Фаза 1.1): `market_tickers` (batch з сортуванням і top-movers), `market_markets` (пошук/фільтр, кеш 1 год), `market_trades` (aggressor-signed flow: buy/sell notional, imbalance, VWAP, CVD, топ-прінти).
- Нова аналітика: `analytics/trades.py` — чиста функція `summarise` для ccxt-trades.
- `Exchanges`: методи `tickers`, `markets`, `trades`; `has.fetchTickers` поважається (NotSupported → `missing`).
- Два Deribit-інструменти (Фаза 1.12 частково): `deribit_expected_move` (ATM-straddle USD, breakeven-и, 1σ-діапазон) і `deribit_oi_by_strike` (OI-стіни: кол/пут/ноцінал USD, топ-рівні). Чиста аналітика поверх уже наявного option-book.
- Якість smile (Фаза 1.10 частково): `OptionRow` тепер несе bid/ask і spread_pct; `build_smile` приймає `min_oi` та `max_spread_pct`; `deribit_vol_surface` отримав ці параметри плюс `thin_oi_threshold`, повертає `strikes_used` і `thin_wing_25d/10d` з попередженнями про тонкі крила. Залишилось: вагові LS-фіти smile за ліквідністю.
- Тестів: 61 (+10 від 0.1.0), lint чистий.

## 0.1.0 — 2026-10-10
- Ядро: `Http` (ретраї, rate-limit, кеш із single-flight, класифікація помилок), `HealthRegistry`, `Envelope/Evidence`.
- Провайдери: Deribit (публічний REST), Hyperliquid (info API), ccxt-біржі (публічні дані).
- Аналітика: Black-76 + greeks, smile, RR/Fly, max pain, GEX, basis, funding-нормалізація, ймовірності (above/below/touch), odds/de-vig, метрики стакана.
- 16 MCP-інструментів (див. README).
- Режими запуску: stdio і streamable-http.
- Тести: 51; lint: ruff.
