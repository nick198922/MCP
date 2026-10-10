# Changelog

## Unreleased
- Фаза 1.8: `calendar_expiries(currencies, horizon_days, funding_hours_ahead)` — один dashboard-інструмент із трьома секціями. Deribit експірації класифікуються `classify_expiry` як daily/weekly/monthly/quarterly (вічко — 08:00 UTC пʼятниці; остання пʼятниця Mar/Jun/Sep/Dec → quarterly, остальні — monthly). `funding_schedule(now, hours_ahead)` будує список найближчих payouts для Binance/Bybit/OKX (8h, 00/08/16 UTC) та Hyperliquid (щогодини). `filter_macro` фільтрує події з `src/tradedesk/data/macro_events.json` (FOMC/CPI/NFP; оновлюється вручну, у wheel через `force-include`).
- Фаза 1.6: `sentiment_fear_greed(days=1..365)` — крипто-індекс Fear & Greed з alternative.me. Новий провайдер `alternative_me.py` (`/fng/`, rate 2 req/s). Нова чиста аналітика `summarise_fear_greed` (zones extreme_fear/fear/neutral/greed/extreme_greed, mean 7d/30d, min/max, change_7d/30d, zone_share). Фікстура `tests/fixtures/fear_greed_sample.json` (30 днів навколо NOW); тест happy-path і провайдер-фейл. Доданий кейс у `live_smoke.py` (поріг 11/15).
- Фаза 0.6: `perps_open_interest` тепер рахує `oi_usd = openInterestAmount × last` коли біржа не повертає `openInterestValue` (Binance/Bybit). Fallback позначається попередженням; якщо ticker недоступний — `oi_usd` лишається `None` як раніше.
- Фаза 0.7: `scripts/live_smoke.py` розширено п'ятьма новими кейсами — `market_tickers`, `market_markets`, `market_trades`, `deribit_expected_move`, `deribit_oi_by_strike` (разом 14 кейсів; порог успіху 10/14).
- Три нові ринкові інструменти (Фаза 1.1): `market_tickers` (batch з сортуванням і top-movers), `market_markets` (пошук/фільтр, кеш 1 год), `market_trades` (aggressor-signed flow: buy/sell notional, imbalance, VWAP, CVD, топ-прінти).
- Нова аналітика: `analytics/trades.py` — чиста функція `summarise` для ccxt-trades.
- `Exchanges`: методи `tickers`, `markets`, `trades`; `has.fetchTickers` поважається (NotSupported → `missing`).
- Два Deribit-інструменти (Фаза 1.12 частково): `deribit_expected_move` (ATM-straddle USD, breakeven-и, 1σ-діапазон) і `deribit_oi_by_strike` (OI-стіни: кол/пут/ноцінал USD, топ-рівні). Чиста аналітика поверх уже наявного option-book.
- Якість smile (Фаза 1.10 частково): `OptionRow` тепер несе bid/ask і spread_pct; `build_smile` приймає `min_oi` та `max_spread_pct`; `deribit_vol_surface` отримав ці параметри плюс `thin_oi_threshold`, повертає `strikes_used` і `thin_wing_25d/10d` з попередженнями про тонкі крила. Залишилось: вагові LS-фіти smile за ліквідністю.
- Fix: OKX `fetch_order_book` повертає рівні з 4+ колонок — `book_stats`/`walk_book` тепер толерантні до будь-якого числа колонок після `[price, size]`; тест `test_book_tolerates_extra_level_columns`.
- Фаза 0.5: `make smoke` виконано 2026-10-10, 9/9 ok (Deribit greeks звірено з their ticker до 4 знаків); живі зразки ccxt і HL задокументовано в `docs/reference/api-notes.md`.
- Фаза 1.4: чотири Hyperliquid-інструменти (`hyperliquid_candles`, `hyperliquid_book`, `hyperliquid_trades`, `hyperliquid_funding_history`); провайдер отримав `recent_trades`; нова чиста аналітика `summarise_funding_history` (mean 7d/30d, z-score, percentile, carry APR, polarity flips). Live-протестовано на реальному BTC/ETH 2026-10-10.
- Фаза 1.2: `perps_funding_history` — крос-венюз історія funding (Binance/Bybit/OKX через ccxt `fetch_funding_rate_history` + Hyperliquid), автодетект інтервалу з медіани ts-різниць (`detect_interval_hours`), крос-венюз-summary (highest/lowest carry, biggest |z|), опція `include_series` для сирих рядів. Live-перевірено на BTC: HL carry 9.1%, OKX 5.1%, Binance 4.5%, Bybit 4.3%; Bybit `z = −1.45` (mean-reversion сигнал).
- MCP-промт `token_deep_analysis(project)` — глибокий 10-секційний інвест-аналіз криптопроекту (RU), шаблон `src/tradedesk/prompts/token_deep_analysis.md` (bundled у wheel), з хінтом про релевантні tradedesk-mcp інструменти для графіка/позиціювання.
- Фаза 1.3: нові провайдери `binance_futures.py` і `bybit.py` під окремими `Http` з лімітами. Два нові інструменти: `perps_long_short_ratio` (роздрібні account-based fractions Binance+Bybit, latest/mean/z-score, cross-venue divergence) і `perps_oi_history` (OI history Binance в USD, Bybit в base, latest/mean/change24h/z, cross-venue biggest gain/drop). Live-перевірено 2026-10-10 на BTC.
- Тестів: 75 (+24 від 0.1.0), lint чистий.

## 0.1.0 — 2026-10-10
- Ядро: `Http` (ретраї, rate-limit, кеш із single-flight, класифікація помилок), `HealthRegistry`, `Envelope/Evidence`.
- Провайдери: Deribit (публічний REST), Hyperliquid (info API), ccxt-біржі (публічні дані).
- Аналітика: Black-76 + greeks, smile, RR/Fly, max pain, GEX, basis, funding-нормалізація, ймовірності (above/below/touch), odds/de-vig, метрики стакана.
- 16 MCP-інструментів (див. README).
- Режими запуску: stdio і streamable-http.
- Тести: 51; lint: ruff.
