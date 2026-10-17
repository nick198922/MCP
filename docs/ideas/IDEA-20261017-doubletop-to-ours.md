# IDEA-20261017-doubletop-to-ours: використати MCP DoubleTop, поки є доступ, щоб добудувати наше (Coinalyze, ончейн, звірка)

Статус: accepted (рішення власника 2026-10-17: «хочу щоб ми використали їх МСР для побудови нашого»)
Пріоритет: власник
<!-- proposed → accepted | rejected → testing → confirmed | rejected | inconclusive → built -->

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
5. **Pendle** (Фаза 5.4) — фіксована дохідність і крива. Нижчий пріоритет; зразки знімати лише коли дійде черга.

На кожен інструмент: інваріанти `CLAUDE.md` (п. 5–6, `Fetched` + `Evidence`, `READ_ONLY`, тест на збій провайдера), реальний зразок з публічного API в `tests/fixtures/`, звірка з відповіддю DoubleTop на тому самому вході, рядок у `docs/PARITY.md`.

## Що потрібно від власника
- Безкоштовний ключ Coinalyze: акаунт на https://coinalyze.net, ключ на сторінці API.
- Далі в Terminal: `bash ~/MCP/tradedesk-mcp/scripts/server/coinalyze-key.sh`. Скрипт перевіряє ключ і кладе його в середовище MCP-сервісу; агентам ключ не дістається.
- Крок 4 чекає на це; кроки 1–3 від ключа не залежать.

## Критерій «готово»
- Кроки 1–3 злиті з тестами.
- Звірка записана.
- Крок 4 — коли буде ключ.
- `docs/PARITY.md` оновлено.

## Тріаж критика
Замінено рішенням власника. Критик перевіряє код (режим C):
- немає жодного виклику DOUBLETOP з `src/` чи `scripts/`;
- ключ не потрапляє в логи й `evidence`.
