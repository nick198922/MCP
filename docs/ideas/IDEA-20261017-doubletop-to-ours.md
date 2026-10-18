# IDEA-20261017-doubletop-to-ours: використати MCP DoubleTop, поки є доступ, щоб добудувати наше (Coinalyze, ончейн, звірка)

Статус: accepted (рішення власника 2026-10-17: «хочу щоб ми використали їх МСР для побудови нашого»)
Пріоритет: власник
Порядок: 5
<!-- proposed → accepted | rejected → testing → confirmed | rejected | inconclusive → built -->

## Прогрес
- [x] 1a. Звірка наших інструментів з DOUBLETOP на одному вході (18.10, шість пар, BTC/Xi; docs/reviews/2026-10-18-doubletop-parity.md)
- [ ] 1b. Звірка (зроблено: BTC, ETH, SOL проти DT, `.A` = Binance; лишилась Bybit-гілка — потрібні коди бірж `/exchanges`, ключ у власника): ETH і альт (до 3–5 входів, як у кроці 1), Bybit-гілки, з'ясувати, що означає суфікс `.A` у Coinalyze (дані звірки вказують на Binance, а не агрегат; див. docs/reviews/2026-10-18-doubletop-parity-critic.md)
- [x] 2. `dex_pairs`
- [x] 3. `chain_metrics`
- [x] Ключ Coinalyze (власник, 17.10)
- [x] 4a. Провайдер Coinalyze + `liquidations_history`
- [ ] 4b. Жива перевірка власником (USD/одиниці) і бекфіл колектором
- [ ] 5. Повтор перевірок на історії Coinalyze
- [x] 6a. Pendle (нижчий пріоритет): фіксована дохідність, `pendle_fixed_yields`, `pendle_market` змерджено (PR #78)
- [x] 6b. Pendle: крива дохідності — інструмент `pendle_yield_curve` (критик: approve, `docs/reviews/2026-10-18-pendle-yield-curve.md`)
- [ ] 6c. Звірка `pendle_yield_curve` з відповіддю DoubleTop `pendle__yield_curve` на тому самому вході (потрібен зразок DT від Claude в інтерактивній сесії)

## Черга
- Після [IDEA-20261017-deviation-weekly-trend](IDEA-20261017-deviation-weekly-trend.md) (H1 — першою).
- Це інженерна картка: розробник + критик (рев'ю коду). Квант — лише для звірки чисел.
- Правила користування конектором — `docs/AGENTS.md`, розділ «MCP DOUBLETOP».

## Що вже знято (17.10, Claude)
- Зразки — `tests/fixtures/doubletop/`:
  - Coinalyze, історія ліквідацій;
  - `dt_edu`: `pair_discovery`, `chain_trend_snapshot`, невдалі `token_card_snapshot` і `risk_hints_token`, `provider_readiness`.
- **Висновок:** їхній власний шар `dt_edu` слабкий.
  - Картка ETH порожня (у них падає CoinGecko).
  - «Трендові токени» Solana — фейкові «SOL».
  - Ризики PEPE — порожні.
  - Справжня цінність — у даних **Coinalyze** (ключ у них свій) і в публічних API, які вони обгортають (DexScreener, DefiLlama, Pendle).

## Кроки
1. **Звірка того, що в нас уже є** (швидко, без нового коду). На 3–5 однакових входах порівняти наші інструменти з їхніми і записати розбіжності в `docs/reviews/2026-10-…-doubletop-parity.md` (одиниці, часові пояси, покриття бірж, хвости історії):
   - `market_ohlcv` ↔ `ccxt__get-ohlcv`;
   - `perps_oi_history` ↔ `coinalyze__open_interest_history`;
   - `perps_funding_history` ↔ `coinalyze__funding_rate_history`;
   - `perps_long_short_ratio` ↔ `coinalyze__long_short_ratio_history`;
   - `hyperliquid_funding_history` ↔ `hyperliquid_info__funding_history`;
   - `polymarket_history` ↔ `graph_polymarket__get_price_history`.
2. **`dex_pairs`** — пари токена з DexScreener (публічний API `/latest/dex/tokens/{addresses}`):
   - адреса чи кілька адрес; фільтр мережі; сортування за ліквідністю;
   - поля як у їхньому `pair_discovery`, але числа — числами, symbol без пробілів;
   - прапорець «ймовірна підробка» (символ збігається з великим активом, а ліквідність неправдоподібна відносно обсягу).
   - Потрібно для закликів трейдерів на DEX-токени (CASHCAT, PENGUIN, DEGEN…) і для промпту `token_deep_analysis`.
3. **`chain_metrics`** — TVL, DEX-обсяг за 24 год і fees за 24 год по мережі (DefiLlama: `/v2/historicalChainTvl/{chain}`, `/overview/dexs/{chain}`, `/overview/fees/{chain}`), з динамікою за 7 і 30 днів. Це єдині корисні панелі їхнього `chain_trend_snapshot`.
4. **Coinalyze** (чекає ключ власника, див. нижче):
   - провайдер з власним rate-limit, ~40 запитів/хв;
   - інструмент `liquidations_history`: лонг/шорт ліквідації по символу й біржі, агреговано по біржах, денні й годинні;
   - бекфіл у сховище колектором MCP-сервісу, не агентами: денна історія ліквідацій, OI, funding, лонг/шорт для BTC, ETH і топ-30 по Binance, Bybit, OKX, Hyperliquid — наскільки дає Coinalyze (денні — щонайменше з 2024-01).
   - Це дані для майбутніх перевірок: каскади ліквідацій, натовп у funding.
5. **Повтор перевірок на історії Coinalyze** (пропозиція 4 звіту; квант → критик, режим B, після кроку 4). Раніше бракувало саме історії ліквідацій і OI по біржах:
   - [IDEA-20261013-liquidation-flush-overshoot](IDEA-20261013-liquidation-flush-overshoot.md) — каскади ліквідацій із сумарними ліквідаціями всіх бірж замість проксі;
   - [IDEA-20261015-funding-crowding-longshort](IDEA-20261015-funding-crowding-longshort.md) — натовп у funding з OI і лонг/шорт по біржах.
   Нова реєстрація кожної; результати попередніх версій не використовувати для вибору порогів.
6. **Pendle** (Фаза 5.4) — фіксована дохідність і крива. Нижчий пріоритет; зразки знімати лише коли дійде черга.

На кожен інструмент: інваріанти `CLAUDE.md` (п. 5–6, `Fetched` + `Evidence`, `READ_ONLY`, тест на збій провайдера), реальний зразок з публічного API в `tests/fixtures/`, звірка з відповіддю DoubleTop на тому самому вході, рядок у `docs/PARITY.md`.

## Ключ Coinalyze — є (2026-10-17)
Власник додав безкоштовний ключ (`scripts/server/coinalyze-key.sh`): він у середовищі MCP-сервісу, з сервера Coinalyze відповідає 200 (28 бірж; перевірка — `scripts/server/coinalyze-check.sh`, ключ не друкує). Крок 4 розблоковано. Агентам ключ не видно: історію качає MCP-сервіс (колектор), агенти читають сховище.

## Критерій «готово»
- Кроки 1–3 злиті з тестами.
- Звірка записана.
- Крок 4 — коли буде ключ.
- `docs/PARITY.md` оновлено.

## Тріаж критика
Замінено рішенням власника. Критик перевіряє код (режим C):
- немає жодного виклику DOUBLETOP з `src/` чи `scripts/`;
- ключ не потрапляє в логи й `evidence`.
