<img src="docs/brand/logo.svg" alt="tradedesk" width="96" align="right">

# tradedesk-mcp

Особистий MCP-сервер «трейдинг-деска»: ф'ючерси та перпи, опціони, prediction markets, квант-інструменти. Замінює та розширює DOUBLETOP, але з акцентом на **деривативи та професійну аналітику**, а не на ончейн.

Поточна версія: **0.1.0 (Фази 0–3, форвард-папір і демо-трейдер у роботі)**: MCP-сервер лише читання; ключі не обов'язкові (опційні лише для читання: Coinalyze, CoinGecko, Telegram-алерти). Ордерний код — лише демо-рахунок Bybit (`execution/`, ігрові гроші, `docs/adr/0007`); live-режиму немає.

## Швидкий старт

```bash
git clone <URL-репозиторію> tradedesk-mcp && cd tradedesk-mcp
make install     # uv sync --extra dev
make test        # 3100+ тестів (≈ 4–5 хв, pytest -n auto), мережа не потрібна
make smoke       # живий прогін по реальних біржах (потрібен інтернет)
make run         # stdio-сервер; make run-http — streamable-http на :8000
```

Вимоги: Python ≥3.11 і [uv](https://docs.astral.sh/uv/). Усі команди — у `Makefile`.

### Підключення до Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tradedesk": {
      "command": "uv",
      "args": ["run", "--directory", "/ABSOLUTE/PATH/TO/tradedesk-mcp", "tradedesk-mcp"]
    }
  }
}
```

### Підключення до Claude Code

```bash
claude mcp add tradedesk -- uv run --directory /ABSOLUTE/PATH/TO/tradedesk-mcp tradedesk-mcp
```

## MCP-промти

- `token_deep_analysis(project)` — глибокий 10-секційний інвест-аналіз крипто-проекту / токена (російською): продукт, залучений капітал, партнерства, токеноміка, розлоки, revenue, value accrual, roadmap, нарратив, тех. аналіз → bull/base/bear + оцінки 1–10 на 3 горизонтах. Промт автоматично підказує моделі, які інструменти tradedesk-mcp використати для частини «графік + позиціювання». Файл: `src/tradedesk/prompts/token_deep_analysis.md` — редагуйте, перезапуск сервера не потрібен (перечитується на кожен виклик).

## Інструменти (105)

| Група | Інструмент | Що дає |
|---|---|---|
| Система | `system_health` | стан провайдерів; `probe=true` робить живий пінг (і віддає повний звіт; типово звіт стислий без втрат, повний — `include_details=true`) |
| Ринок | `market_tickers`, `market_ohlcv`, `market_orderbook`, `market_markets`, `market_trades` | котирування одного символу (через `fetch_ticker`, працює й там, де біржа не дає batch) або кількох / усіх (batch, сортування за обсягом, top-movers; колишній `market_ticker` злито сюди, див. `docs/changes/2026-11-17-mcp-merge-ticker-carry-status.md`), свічки з реалізованою вола, стакан зі slippage для заданого розміру, пошук ринків, останні угоди з aggressor-flow (buy/sell, VWAP, CVD) |
| Розмітка графіка | `chart_markup` | рівні й зони курсу DoubleTop зі свічок біржі: EMA Trend 13/21/34, Daily (EMA 100/200 з 4h), Weekly (EMA 100/200 з 1d), VWAP з якорем і смугою 1σ, профіль об'єму (POC/VAH/VAL), структура курсу на структурних свінгах (захищений HL/LH, HH/LL, BOS/CHoCH лише закриттям, тінь — sweep) і внутрішня структура на всіх 3-свічкових свінгах, неторкнута ліквідність і рівні хаї/лої, FVG зі станом заповнення, OB з прапорцями валідності й брейкери, premium/discount і OTE, ATR/RSI, азійський рейндж і свип на відкритті Франкфурта/Лондона; BingX-форекс приймає `EURUSD` |
| Перпи | `perps_funding_compare` | funding по Binance/Bybit/OKX/Hyperliquid/Deribit, приведений до річних |
| | `perps_funding_history` | історія funding по всіх venues: carry 7d/30d, z-score, перцентиль, polarity flips; крос-венюз: найдорожчий carry, найбільший |z|. `source="coinalyze"` — довга історія Binance з Coinalyze (колишній `coinalyze_funding_history`, див. рядок «Історія Coinalyze» нижче) |
| | `funding_carry_scan` | скан funding carry по біржі: поточний і trailing 7d/30d APR (сума фактичних виплат), статус відносно порогів входу/виходу (вхід за замовчуванням 20%), окупність комісій у днях, чи є спот; за замовчуванням лише крипто-перпи зі спотом; символ із неповною історією funding має `incomplete_history` і не потрапляє в `above_entry_with_spot`. Перевірку на історії див. у `docs/verification/2026-10-12-funding-carry.md` |
| | `funding_spread_scan` | крос-біржовий спред funding для carry перп-перп (Binance/Bybit/OKX, опційно Hyperliquid: моніторинг, не сигнал): на якій біржі шорт, на якій лонг, trailing і поточний спред, окупність витрат, розрив цін, діапазон за 7 днів, прапорці ризику (різні токени під тікером, funding на ліміті, розворот спреду, різні інтервали). Бектест: `docs/verification/2026-10-13-funding-carry-perp-perp.md` |
| | `perps_long_short_ratio` | роздрібна L/S по Binance і Bybit (account-based fractions); z-score, divergence між venues. `source="coinalyze"` — довга історія Binance з Coinalyze (колишній `coinalyze_long_short_history`) |
| | `perps_oi_history` | OI history (Binance в USD, Bybit в base) з latest/mean/change24h/z-score; крос-венюз: biggest gain/drop. `source="coinalyze"` — довга історія з Coinalyze, Binance або сума Binance + Bybit (колишній `coinalyze_oi_history`) |
| | `perps_open_interest` | OI в USD по майданчиках і частки |
| | `perps_basis_compare` | крос-венюз perp premium (bps) + Deribit dated-futures basis, cash-and-carry snapshot |
| | `market_flow` | «читання ринку» з потоків (описово, не сигнал), 1h/4h за ≤ 14 днів: спот-CVD і перп-CVD у USD (свічки Binance, поле taker buy), OI у монетах і USD зі зміною (Binance + Bybit), funding річний, ліквідації лонг/шорт (Coinalyze, потрібен ключ), частка лонгів рахунків, премія Coinbase (сира і мінус медіана 30 днів); квадрант «ціна × OI × перп-CVD» за 4/24 год і коротке речення українською. Поодинці ці метрики напрямок не передбачили (картка `IDEA-20261028-market-flow-reader`) |
| | `cycle_regime` | денний «режим циклу» монети (BTC): дно / накопичення / перегрів / ріст / падіння за порогами, зафіксованими в картці до даних (просадка від 365-денного максимуму, funding7 річний, базис7 перп/спот, RSI14, ціна до SMA365 і нахил SMA365 за 30 днів; Binance, денні закриття); усі складові, найближчий інший стан і що має змінитись, рядок українською, історія станів до 365 днів. Контекст, не сигнал; ще не перевірено на історії (картка `IDEA-20261028-btc-cycle-regime-gauge`) |
| Hyperliquid | `hyperliquid_overview` | скринінг усього Hyperliquid за OI, funding, обсягом |
| | `hyperliquid_candles` | OHLCV + реалізована вола |
| | `hyperliquid_book` | L2-стакан (20 рівнів) + slippage під розмір |
| | `hyperliquid_trades` | останні угоди з aggressor-flow (B/A → buy/sell, VWAP, CVD) |
| | `hyperliquid_funding_history` | історія funding + mean 7d/30d, z-score, percentile, річний carry, polarity flips |
| Ф'ючерси | `deribit_futures_curve` | basis і term structure (прості та лог. річні) |
| Опціони | `deribit_vol_surface` | ATM IV, RR/Fly 25Δ і 10Δ, очікуваний рух, P/C по експірації |
| | `deribit_option_chain` | ланцюжок з greeks (Black-76, збігається з Deribit) |
| | `bybit_option_chain` | ланцюг опціонів Bybit (BTC / ETH, розрахунок у USDT): на експірацію — дні, ціна базового активу Bybit, ATM-страйк і ATM IV, OI колів і путів; на страйк — bid / ask / mid кола й пута в USD, спред у % від mid і в пунктах IV (десятковий дріб), mark IV, OI в монетах. Типово 3 найближчі експірації й ATM ± 3 страйки; усе (плюс розміри на кращому рівні, IV bid / ask, mark, delta) — `include_details=true`. Лише верх стакана; індекс Bybit (USDT) відрізняється від Deribit на кілька bp |
| | `option_structure_quote` | ціна конструкції на живому ланцюзі Bybit (BTC / ETH) тими самими функціями, що й тижневий генератор опціонних ідей: залізний кондор, ведмежий пут- / бичачий кол-дебетовий спред, довгий стренгл або стреддл; страйки за дельтою (і ширина крил кондора в частці ціни), експірація — найближча до `days` у вікні `min_days`–`max_days` (типово 14 у 7–24). Ноги (символ Bybit, сторона, bid / ask, ціна виконання, комісія, delta, mark IV), премія до й після комісії тейкера, макс. прибуток / збиток, точки беззбитку, R:R, ризик-нейтральна ймовірність прибутку (null для стренгла / стреддла, якщо беззбиток поза сіткою страйків путів), ширини крил кондора (нерівні — попередження); USD за 1 монету на експірації. Комісія поставки не врахована; маржа проданих ніг купленою ногою не зменшується (кондор і дебетовий спред); ціна, а не сигнал |
| | `deribit_max_pain`, `deribit_gamma_exposure` | max pain, GEX із рівнем flip |
| | `deribit_expected_move` | ціна ATM-страддла в USD, breakeven-и й 1σ-діапазон за експіраціями |
| | `deribit_oi_by_strike` | OI-стіни по страйках: кол/пут/USD-ноцінал, топ-рівні підтримки/опору |
| | `prob_from_options` | ризик-нейтральна ймовірність above/below/touch з урахуванням skew |
| | `deribit_dvol` | DVOL (річний %) time series + latest / mean / stdev / min / max / change 24h / 7d |
| | `deribit_vrp` | премія за волатильність: DVOL проти реалізованої (7д/30д), перцентиль DVOL за історію і за рік, історична премія і результат хеджованого продажу ATM-стреддла, у т.ч. по квінтилях рівня DVOL. Перевірку на історії див. у `docs/verification/2026-10-13-vrp.md` |
| | `deribit_skew_term` | term structure 25Δ-RR і 25Δ-fly (puts/calls richer, flattening/steepening); `view="atm"` — ATM-IV term structure (contango/backwardation) по експіраціях (колишній `deribit_atm_term`, див. `docs/changes/2026-11-15-mcp-cleanup-atm-term.md`) |
| | `deribit_largest_trades` | останні крупні трейди (options/futures), фільтр блок-угод, buy/sell imbalance |
| Квант | `odds_devig`, `option_calc`, `touch_probability` | маржа й справедливі ймовірності, калькулятор опціону, barrier-ймовірність (fib-retracement ладдер прибрано з MCP 18.11, формула лишилась в `analytics/retracement.py` і в промпті `fomc_playbook`) |
| Настрій | `sentiment_fear_greed` | Crypto Fear & Greed (alternative.me): latest + zone, mean 7d/30d, зміни, частка днів у кожній зоні |
| Pendle (фіксована дохідність) | `pendle_fixed_yields`, `pendle_market`, `pendle_yield_curve`, `pendle_expiry_calendar` | календар експірацій PT за датами з сумарною ліквідністю; крива implied APY за експірацією для активу (без інтерполяції, прапорець thin); скан ринків Pendle: implied/underlying APY, дисконт PT, дні до експірації, ліквідність; один ринок + APY за заданою ціною PT (десяткові частки; зразок знято наживо 18.10) |
| On-chain / DEX | `dex_pairs`, `contract_risk`, `chain_metrics`, `stablecoin_supply` | пули токена за адресою з DexScreener (за ліквідністю, прапорець `likely_fake` для підроблених тікерів); фільтр безпеки контракту EVM-токена (GoPlus + друге джерело honeypot.is — симуляція купівлі й продажу, лише ethereum / bsc / base: для honeypot, cannot_sell_all і податків гірший вердикт двох, плюс окрема перевірка `holder_sell_fail_rate` + ліквідність / кап DexScreener: 25 перевірок pass / fail / unknown за порогами курсу, нема даних = unknown, не pass; евристика, не порада) і Solana-токена (`chain="solana"`, GoPlus `solana/token_security`: 14 перевірок — freeze / mint / close authority, permanent delegate, transfer hook і fee, LP burn, холдери, ліквідність / кап; без симуляції продажу, тому `honeypot` завжди unknown і `overall` не буває pass; EVM-перевірки без поля — у `not_checked`); TVL / DEX-обсяг / fees мережі з DefiLlama зі змінами 7д і 30д; пропозиція стейблкоїнів з DefiLlama (сума всіх, USDT, USDC у USD зі змінами 7д / 30д, десяткові частки; описовий контекст ліквідності, не сигнал) |
| On-chain / DeFi-пули | `yield_pools`, `yield_pool_history` | пули дохідності DefiLlama (`yields.llama.fi`, без ключа): перелік з фільтрами (мережа, проєкт, мінімальний TVL у USD, лише стейблкоїн-пули) і сортуванням за TVL, APY або «найновіші» (найменше днів історії); APY, базовий APY (комісії, відсотки) і винагородний (у токенах) десятковими частками, зміни APY за 1 / 7 / 30 днів, прапорці DefiLlama stablecoin / il_risk / outlier, адреси токенів; історія одного пулу за днями (TVL у USD, APY) зі змінами TVL 7д / 30д і середнім APY 7д / 30д. APY оголошений і змінний, не гарантія; без IL, газу й комісій; назви — недовірений текст; ризик токенів — `contract_risk`. Тест на синтетичному зразку документованої форми; форму й назви полів звірено живцем 19.11 |
| On-chain / Bitcoin | `btc_fees`, `btc_mempool`, `btc_latest_block`, `btc_difficulty_hashrate`, `btc_address` | мережа Bitcoin з публічного API mempool.space (без ключа): рекомендовані комісії в sat/vB; стан мемпулу (кількість транзакцій, розмір у vB, «блоків черги» = vsize / 1 000 000 vB, частки за смугами комісій); останній блок (висота, час, заповненість, комісії, винагорода, пул — недовірений текст); складність і хешрейт в EH/s (оцінка, не вимір); огляд адреси mainnet (баланс у sats і BTC, підтверджене й мемпул окремо; адреса перевіряється за контрольною сумою до запиту). Контекст мережі, не сигнал; адреса ≠ гаманець, власник невідомий |
| On-chain / EVM | `onchain_balance`, `onchain_address_txs`, `onchain_token_info`, `onchain_contract_source` | Etherscan API V2 (безкоштовний ключ `ETHERSCAN_API_KEY` у середовищі служби MCP; без нього — `missing: config`), параметр `chain`: ethereum, bsc, polygon, arbitrum, optimism, base, avalanche. Баланс адреси в нативній монеті або в ERC-20 (`token`): точне ціле в найменших одиницях рядком + число з урахуванням decimals; транзакції адреси (`kind="normal"`) або ERC-20 перекази (`kind="erc20"`, фільтр `token`), нові спершу, сторінки ≤ 100 рядків, ≤ 10 000 на перелік; назва / символ / decimals / total supply токена з контракту (`eth_call`; без ціни й холдерів — це платний Pro); зведення verified-коду контракту (компілятор, proxy й implementation, файли; сам код лише уривком ≤ 4000 символів за запитом). Назви й символи токенів — недовірений текст; адреса ≠ власник; які мережі відкриває безкоштовний ключ, не перевірено |
| Календар | `calendar_expiries` | експірації Deribit (daily/weekly/monthly/quarterly), найближчі funding-моменти, макро-події (FOMC/CPI/NFP) |
| Календар | `macro_calendar` | макрокалендар з високим впливом: ForexFactory (цей і наступний тиждень; USD: FOMC, CPI, Core PCE, NFP, PPI, GDP, Retail Sales, ISM, Jobless Claims + рішення ECB/BoJ/BoE), час UTC і Київ, прогноз і попереднє (фактичного значення нема), далі сід FOMC/CPI/NFP; прапор події в найближчі 48 год |
| Події (Фаза 1) | `events_add`, `events_list`, `event_study` | локальна база макро-подій (FOMC/CPI/NFP); seed 2024-2027 запаковано у wheel і завантажується разово скриптом `uv run python scripts/events_seed_load.py` (колишній інструмент `events_seed_load`, прибрано 18.11); windowed-study поверх `ohlcv_hist` (hit rate, p25/50/75) |
| Prediction markets | `polymarket_search`, `polymarket_market`, `polymarket_history` | Polymarket Gamma+CLOB: пошук, карта ринку з order-book мідами, історія цін yes/no |
| Макро/ринок | `coin_snapshot`, `global_market`, `top_coins` | CoinGecko: ціна/mcap/FDV/supply/ATH, глобальна домінація BTC/ETH/alts, топ-N монет |
| Історія (Фаза 2) | `iv_rank`, `funding_zscore`, `skew_percentile`, `alerts_recent`, `ohlcv_history`, `market_regime`, `range_detect` | ATM-IV rank, funding z-score, 25Δ/10Δ RR percentile, журнал алертів, OHLCV, up/down/chop, рейнджі + sweeps + deviations + accumulation/distribution (`range_detect`); `view="setup"` — SFP-сетап (entry/stop/TP/RR), `view="scan"` — сканер SFP-сетапів по списку тикерів (колишні `range_setup`, `range_setup_scan`, злито 18.11, див. `docs/changes/2026-11-18-mcp-merge-range.md`) |
| Журнал (Фаза 2) | `journal_add`, `journal_list`, `journal_close`, `journal_stats` | трекінг угод у локальній SQLite: PnL, R-multiple, теги, win-rate / expectancy / avg R |
| Розриви Polymarket ↔ опціони | `polymarket_options_gap` | відкриті ринки Polymarket на ціну BTC/ETH («вище $K на дату», «торкнеться $K», строк ≤ 14 днів) проти ймовірності тієї ж події з поверхні Deribit (ризик-нейтральна, без знижки): мід YES, розрив, чистий edge на кращому аску після комісії й спреду, глибина ≥ $200 на кращому рівні. Розрив ≠ доведена перевага (форвард-збір CLV триває). Щогодини те саме пише колектор у `pm_gap_snapshot` (`TRADEDESK_PM_GAP_ENABLED=1`). Розриви з останнього знімка, що проходять фільтри входу паперового тесту (≥ 5 п.п. з обома читаннями моделі, глибина ≥ $200), з R:R і розміром на ризик $100 — вкладка «Polymarket» панелі (`/polymarket`). Ордерів немає |
| Форвард-папір carry (рушій колектора) | `paper_carry_status` | дві книги, які веде колектор без участі людини: A cash-and-carry на альтах Binance (тег `cc-paper`) і B перп-перп Binance↔Bybit (тег `pp-paper`); правила й облік ті самі, що в бектестах (спільні чисті функції); стан рушія, відкриті позиції з маркуванням на останній 8-годинний цикл, статистика закритих, P&L, фактична вартість за книгою проти модельної. Ордерів немає; вмикається `TRADEDESK_PAPER_CARRY_ENABLED=1`. З `strategy="manual"` — ручні пари перп-перп (колишній `carry_paper_status`), відкриті раніше інструментом `carry_paper_open`, позначені живими даними; `carry_paper_open` / `carry_paper_close` прибрано 18.11, нових ручних пар через MCP немає (див. `docs/changes/2026-11-18-mcp-remove-six.md`) |
| Паперовий журнал рекомендацій асистента | `assistant_paper_open`, `assistant_paper_status` | паперова угода з опціонної структури (стреддл, стренгл, кондор, спреди) за марками Deribit ± 1 вп, розмір за депозитом і ризиком, розрахунок на delivery (P&L в USD, % спота як основна метрика, `r_study` і `r_worst_case`; підсумки окремо за `source` method/manual); окрема таблиця з міткою `hypothesis`, не змішується з `journal`. Метод рекомендацій не показав переваги (звіт 18.10); ордерів немає |
| Демо-трейдер Bybit (лише читання) | `demo_status` | стан демо-рахунку (ігрові гроші) з таблиці `demo_trade`: відкриті позиції (ціна входу, стоп — беззбитковий, якщо вже пересунуто, ціль, ризик у USD), закриті угоди й середній R кожного сетапу (S3, S12, S28, S17, S14, S16, S38, S39, S40; R = P&L біржі з комісіями / ризик угоди) проти смуги паспорта бектесту з вердиктом «рано» (< 30 угод) / «узгоджується» / «ні», частка заповнення лімітних сетапів, правило зняття S12, примітки про завислі, orphaned і угоди з розбіжним P&L. Ті самі чисті функції й числа, що блок демо `scripts/checkpoint_report.py`. Типово — 10 найновіших позицій і зведення; усі позиції, примітки сетапів і 50 останніх закритих угод — `include_details=true`. «Узгоджується» не означає переваги; ордерів і ключів немає |
| Журнал прогнозів асистента | `forecast_log_status` | щоденні ризик-нейтральні прогнози діапазонів BTC і ETH з усмішки Deribit на експірацію ≈ 7 днів (вище спота, ±1σ від форварду, ±5 %, ±10 %), розрахунок на delivery: Brier, порівняння з еталоном 0.5 / 0.6827 і кліматологією, калібрування в 10 кошиках, таблиця за тижнями; розмір вибірки — `n_expiries`. Поруч — модель RV30 (логнормальна від спота з реалізованою волою 30 днів Binance, без дрейфу): ті самі оцінки й парне порівняння «опціони проти RV30» з ДІ по датах експірації. Пише колектор (`TRADEDESK_PAPER_FORECAST_LOG_ENABLED=1`, за замовчуванням вимкнено), таблиця `forecast_log` окремо від `journal`. Ймовірності — ціна опціонів, не реальні шанси; ордерів немає |
| Плани угод асистента | `plan_add`, `plan_list`, `plan_review`, `plan_link` | кожен план режиму «план угоди» (вердикт ✅ `enter` / ⏳ `wait` / ❌ `skip`, правила власника 1–8, теги `idea:…`) пишеться в локальну таблицю `trade_plan` і оцінюється на свічках біржі (1h): вхід market / limit / stop, перший дотик стопу чи цілі (свічка з обома — стоп), інакше закриття на горизонті; R нетто комісії тейкера 0.05 % на бік. Підсумок за вердиктом і тегом: hit rate, середній і медіанний R з 95 % ДІ; `wait` / `skip` — контрфактично. Щогодини те саме робить колектор (`TRADEDESK_PAPER_PLAN_RESOLVE_ENABLED=1`). Паперовий облік суджень асистента, не доведена перевага; ордерів немає |
| Позиції (Фаза 2) | `positions_add`, `positions_list`, `positions_close`, `positions_risk` | ручний портфель + сценарії PnL (±5 / ±10 % спот, ±5 пунктів вола, тобто `d_vol_points` = ±5.0, crash+vol-up): net δ/γ/vega/θ |
| Ліквідації (Фаза 2) | `liquidations_summary` | WebSocket збирач Binance `!forceOrder@arr` + агрегати (longs/shorts USD, imbalance, per-symbol, top) |
| Ліквідації, історія | `liquidations_history` | Coinalyze (потрібен `COINALYZE_API_KEY`): ліквідації лонгів/шортів перпа на Binance (`.A`, за замовчуванням) або сума вибраних бірж (`exchanges`: `binance`/`.A`, `bybit`/`.6`, лише USDT-перпи; інші коди відхиляються), daily/hourly, USD або базові одиниці; net, long_share, пік, перелік використаних бірж. USD у Coinalyze = монети × close дня, тож у день обвалу суми зсунуті; точні суми — `liquidations_summary` |
| Історія Coinalyze (Binance) | `perps_oi_history`, `perps_funding_history`, `perps_long_short_ratio` з `source="coinalyze"` | Coinalyze (потрібен `COINALYZE_API_KEY`), лише Binance (суфікс `.A` = Binance, не сума бірж; для OI — параметр `exchanges`: сума Binance `.A` + Bybit `.6`), `interval` daily/hourly, `days` (для funding — `days_back`, цілі дні до 365), `margin`: OI (OHLC, USD = монети × close інтервалу, або базові монети, `in_usd`), funding (OHLC, десятковий дріб за інтервал виплати, не річний; Coinalyze шле відсотки, ділимо на 100), лонг/шорт рахунків (ratio, частки 0–1); latest/mean/stdev/min/max, z-score останнього, зміна. Колишні `coinalyze_oi_history`, `coinalyze_funding_history`, `coinalyze_long_short_history` (злито 18.11, див. `docs/changes/2026-11-18-mcp-merge-coinalyze-perps.md`) |
| Правила (Фаза 3) | `rules_add`, `rules_list`, `rules_test`, `rules_disable` | DSL правил alert'ів (безпечний AST + metric funcs); зберігає й читається alerts job'ом; 8 готових пресетів у wheel ставляться разово скриптом `uv run python scripts/rules_presets_load.py` (колишній інструмент `rules_presets_load`, прибрано 18.11; пороги не бектестовані) |
| Стратегії (Фаза 3) | `strategy_payoff`, `strategy_cash_and_carry`, `strategy_funding_carry`, `strategy_scenario_matrix` | многоногий payoff (власні ноги або `preset`: straddle / strangle / risk_reversal / iron_condor — колишні окремі інструменти, див. `docs/changes/2026-11-15-mcp-cleanup-strategy-presets.md`) + шаблони carry + multi-horizon MTM-grid (τ-aware Black-76): P&L крива, breakevens, max profit/loss, δ/γ/vega/θ, MTM, risk-neutral PoP, scenario matrix |

MCP-промпти: `token_deep_analysis`, `fomc_playbook`, `pre_trade_checklist` (план угоди: безпека контракту `contract_risk` для он-чейн токенів, контекст, розмір від рахунку й ризику, вердикт, запис у журнал), `journal_review` (щомісячний розбір журналу за тегами), `morning_briefing` (шаблони з підказками, які `tradedesk-mcp` інструменти викликати).

Усі відповіді мають однаковий конверт `{ok, data, evidence, missing, warnings, as_of}`: кожне число має джерело й час, а збій одного провайдера не валить виклик. Деталі в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Деплой на VPS

Готовий `Dockerfile` + `docker-compose.yml` + systemd-unit лежать поруч із кодом; поетапна інструкція (Docker або bare systemd), приклад Caddy-конфігурації для TLS+bearer і бекап-скрипт SQLite — у [deploy/README.md](deploy/README.md). Опція «працює, поки сплю» вмикається одним флагом `TRADEDESK_COLLECTOR_ENABLED=1`.

## Як працювати над проєктом

Проєкт розвивається разом із Claude (Claude Code у терміналі). Точка входу для кожної сесії — [CLAUDE.md](CLAUDE.md): правила, команди, інваріанти. Поточний стан — [docs/HANDOFF.md](docs/HANDOFF.md), план — [docs/BACKLOG.md](docs/BACKLOG.md).

## Документація

- [CLAUDE.md](CLAUDE.md), [docs/HANDOFF.md](docs/HANDOFF.md), [docs/BACKLOG.md](docs/BACKLOG.md), [CHANGELOG.md](CHANGELOG.md)
- [docs/adr/](docs/adr/): ключові архітектурні рішення
- [docs/reference/](docs/reference/): каталог DOUBLETOP і нотатки про API
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): шари, контракти, як додавати провайдерів і інструменти
- [docs/ROADMAP.md](docs/ROADMAP.md): фази від MVP до виконання ордерів
- [docs/PARITY.md](docs/PARITY.md): що з DOUBLETOP вже замінено, а що ні
- [docs/SECURITY.md](docs/SECURITY.md): ключі та запобіжники для торгівлі

## Обмеження

- Живі API я (Claude) не міг перевірити зі свого середовища: мережа обмежена. Логіку покрито тестами на реальних зразках відповідей Deribit і Hyperliquid, а `scripts/live_smoke.py` потрібно запустити у вас.
- Опціони: поки BTC та ETH на Deribit (інверсні). USDC-опціони на альти та IBKR йдуть у наступних фазах.
- Ймовірності з опціонів ризик-нейтральні, а GEX спирається на евристичне припущення про позиції дилерів.
