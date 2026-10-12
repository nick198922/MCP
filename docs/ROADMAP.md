# Дорожня карта

Легенда: ✅ зроблено і протестовано · 🔜 заплановано · ❓ потребує вашого рішення

## Фаза 0: основа ✅
Ядро (Http з ретраями, rate-limit, кеш із single-flight, health, Envelope/Evidence), каркас MCP-сервера, тести.

## Фаза 1: ринкові дані та деривативна аналітика (≈ 60 %)
✅ ccxt: ticker, OHLCV із реалізованою вола, стакан зі slippage для заданого розміру
✅ Funding по 5 майданчиках з нормалізацією інтервалів; OI по майданчиках; скринер Hyperliquid
✅ Deribit: крива ф'ючерсів (basis), vol surface (ATM, RR/Fly 25Δ/10Δ), ланцюжок із greeks, max pain, GEX, ймовірності above/below/touch
✅ Квант: de-vig (proportional, power), калькулятор опціону, barrier-ймовірність
🔜 Історія funding (ccxt + Hyperliquid), long/short ratio (публічні ендпоїнти Binance/Bybit), свічки/книга/угоди Hyperliquid, `get-markets`/`get-trades` з ccxt
🔜 Polymarket (Gamma + CLOB): ціни, OI, історія. Зв'язка з опціонами: порівняння ймовірності ринку з options-implied та маржа ринку
🔜 Крипто Fear & Greed + домінація BTC (замість US-індексу з DOUBLETOP)
🔜 Календар: експірації Deribit/CME, час funding, макро-події
🔜 CoinGecko-інструменти (ціни, mcap, історія) без code-sandbox

## Фаза 2: пам'ять і автономність («працює, поки я сплю»)
🔜 `store/`: SQLite (WAL): знімки метрик (ATM IV по тенорах, RR/Fly, funding, OI, basis, universe Hyperliquid)
🔜 `workers/collector`: періодичний збір; з'являються IV rank/percentile, VRP (implied мінус realized), історія basis
🔜 `workers/liquidations`: WebSocket Binance/Bybit/OKX/Hyperliquid → агрегати ліквідацій (історії в API бірж немає)
🔜 Рушій алертів + Telegram-бот ❓
🔜 Журнал угод і нотаток (`journal_*`)
🔜 Ризик-калькулятори: розмір позиції, ціна ліквідації, портфельні greeks із ручного списку позицій, VaR/Monte Carlo
🔜 Деплой на VPS (Docker, TLS, тунель) ❓

## Фаза 3: правила та стратегії
🔜 Декларативні правила (YAML): умови над метриками → сигнали
🔜 Сканер: ринок проти правил за розкладом
🔜 Шаблони стратегій: cash-and-carry, funding-арбітраж між біржами, продаж/купівля вола за RV/IV, risk reversal за skew, календарні спреди
🔜 Бектестер на збережених даних і історичних свічках: PnL, просадка, Sharpe/Sortino, walk-forward, комісії та funding

## Фаза 4: виконання ордерів (поетапно)
🔜 `ExecutionGuard`: ліміти, список дозволених інструментів, kill-switch, журнал аудиту
🔜 Paper-брокер → testnet (Bybit/Binance/Deribit) → live з малими лімітами
🔜 Двокроковий потік: `order_propose` (план + перевірки ризику + токен) → `order_confirm`

## Фаза 5: розширення
⏸ IBKR — відкладено за рішенням власника (ADR 0006). Повернутись лише за явним запитом.
🔜 Паритет із DOUBLETOP по ончейну: Etherscan V2, Helius, DefiLlama, DexScreener, mempool, Pendle (див. PARITY.md)
🔜 Новини та макро-стрічки

## Фаза 6: посилення
🔜 Автентифікація для віддаленого режиму (bearer/OAuth), ліміти на клієнта
🔜 Спостережуваність (логи, метрики), резервні копії бази

## Критерії готовності кожної фази
Тести проходять офлайн. `scripts/live_smoke.py` зелений на вашій машині. Для нових інструментів є тест на збій провайдера. Оновлені README, PARITY і цей файл.
