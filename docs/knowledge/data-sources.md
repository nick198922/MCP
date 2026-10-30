# Джерела даних і інструменти аналізу

Джерело: курс DoubleTop «Gem3», уроки «Tools» і «Знайомство з DeFi» (оригінали лише на Mac власника), плюс те, що вже є в проєкті. Мета — знати, звідки брати дані для перевірок агентів і для аналізу токенів.

## Що вже є в проєкті

- **Біржі:** Binance, Bybit, OKX, Hyperliquid (ccxt і власні провайдери): ціни, funding, OI, стакани, ліквідації; архів `data.binance.vision` з делістнутими.
- **Опціони:** Deribit (ланцюжки, DVOL, марки угод).
- **Polymarket:** ринки, ціни, угоди, лідерборд і позиції гаманців.
- **CoinGecko** (безкоштовний; щоденні знімки пропозиції в колекторі); **DefiLlama** (TVL, fees/revenue, статичний датасет анлоків); **Alternative.me** (Fear & Greed).
- **Колектор:** знімки пропозиції, рейтинги й позиції топ-трейдерів Hyperliquid і Polymarket.

## Корисні зовнішні джерела з курсу

**Мережі й DeFi:**
- [DefiLlama](https://defillama.com/) (і [yields](https://defillama.com/yields));
- [L2BEAT](https://l2beat.com/);
- [Dune](https://dune.com/browse/dashboards);
- The Block data;
- DefiPulse.

**Ончейн і гаманці (для «розумних грошей»):**
- [Arkham](https://www.arkhamintelligence.com/) — мітки гаманців;
- [Nansen](https://pro.nansen.ai/) — платний;
- [DeBank](https://debank.com/) — позиції гаманця в DeFi, EVM;
- [Zerion](https://zerion.io/);
- [Bubblemaps](https://bubblemaps.io/) — кластери холдерів, ознаки інсайдерів;
- [Glassnode](https://studio.glassnode.com/);
- [Artemis](https://app.artemis.xyz/dashboard);
- Blockpour;
- Etherscan, Blockscan.
- **Дашборди Dune:**
  - [Stealth money](https://dune.com/0xYaki/stealth-money) — нові гаманці з великими покупками токена;
  - [Smart address search](https://dune.com/smart_ape/smart-address-search) — пошук прибуткових адрес;
  - [Uniswap v3 pools: volume, fees, TVL, fee APR](https://dune.com/coindataschool/uniswap-v3-pools-volume-fees-tvl-and-fee-apr).

**Аналіз проєктів і токеноміки:**
- [Token Terminal](https://tokenterminal.com/) — виручка, P/S;
- [Messari](https://messari.io/);
- [Token Unlocks](https://token.unlocks.app/) — розклади анлоків;
- [VestLab](https://vestlab.io/);
- [CryptoRank](https://cryptorank.io/);
- Dropstab;
- Coinsguru;
- [Cryptofees](https://cryptofees.info/) — комісії протоколів;
- [TweetScout](https://tweetscout.io/) — якість аудиторії в X.

**Дохідність у DeFi:**
- [DefiLlama yields](https://defillama.com/yields);
- Nanoly;
- [Staking Rewards](https://www.stakingrewards.com/);
- coindix;
- Beefy — автокомпаундинг.

**Графіки DEX-токенів:** DEXTools, GeckoTerminal, Dexscreener, TradingView.

**Події:** CoinMarketCal (лістинги, запуски, AMA).

## Архів Binance `metrics` (знайдено 24.10, перевірено 24.10)

- **Що:** `data.binance.vision/data/futures/um/daily/metrics/<SYMBOL>/<SYMBOL>-metrics-<дата>.zip`: щоденні файли з 5-хв рядами відкритого інтересу, співвідношень лонг/шорт (акаунти всіх, топ-трейдери за акаунтами й позиціями) і тейкер-співвідношення обсягів. Безкоштовно, без ключа. Початок: BTCUSDT 2020-09-01, SOLUSDT 2021-12-01 (лістинг S3 24.10). Біржове API віддає ті самі співвідношення лише за останні 30 днів.
- **Колонки (перевірено на файлах):** `create_time, symbol, sum_open_interest` (монети), `sum_open_interest_value` (USDT), `count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio, count_long_short_ratio, sum_taker_long_short_vol_ratio`. 288 рядків на добу, 98.7 % файлів повні.
- **Увага, семантика `create_time` змінилась між 2024-03-03 14:30 і 2024-03-04 00:45 UTC** (виміряно на 6383 флеш-подіях через value/OI проти 1m-ціни, звіт нижче); у коді прийнято межу 2024-03-04 00:00 UTC (межа денного файлу) — це припущення, на сирих рядках не перевірене, а правило до перемикання закріплене лише синтетичним тестом. В обох періодах разом у ≈ 27 % подій найкращий збіг пізніший за мітку спостереження (сітка обрізана на +5 хв): у новій роботі розширювати сітку до +10 хв. До цієї дати знімок зроблено в момент мітки; з неї мітка позначає початок інтервалу, а знімок зроблено через 5 хв. Без цієї поправки до березня 2024 ряд зсунутий на 5 хв. Час спостереження рахує `analytics.oi_flush.archive_obs_times`.
- **Як користуватись в аналізі (гіпотеза):** падіння OI у монетах за хвилини руху ціни = примусове закриття (ліквідації); розбіжність позицій топ-трейдерів і акаунтів усіх = «розумні проти роздробу». Жодна з цих ідей не підтверджена; друга відхилена 24.10 (див. нижче).
- **Який інструмент рахує:** MCP-інструмента немає; для досліджень є провайдер-методи `rt.binance_vision.um_metrics_daily` (парсер `parse_metrics_zip` віддає OI і value) і `um_metrics_ratios_daily` (`parse_metrics_ratios_zip`: усі колонки, включно зі співвідношеннями лонг/шорт; порожня клітинка → NaN).
- **Розрив колонок топ-трейдерів (виміряно 24.10 на 75 тис. недільних файлів):** `count_toptrader_long_short_ratio` і `sum_toptrader_long_short_ratio` порожні майже в усіх файлах 2022 року (2022Q3: 1 785 з 1 786, і BTC, і ETH), `count_long_short_ratio` при цьому є; з 2023Q1 колонки повні. Колонки заповнені з 2020-09 до 2021 включно і знову з 2023Q1; порожній лише 2022 (рев'ю критика 24.10).
- **Узгодженість з API (24.10):** `count_long_short_ratio` BTCUSDT за 2026-10-23 дорівнює `globalLongShortAccountRatio` біржі з точністю 0.0005 (округлення API) при «мітка архіву + 5 хв = мітка API», 288 з 288 рядків — це підтверджує семантику після 2024-03-04.
- **Наші перевірки:** [oi-confirmed-liquidation-flush](../verification/2026-10-24-oi-confirmed-liquidation-flush.md) — ◯ inconclusive (ворота потужності не пройдено). [toptrader-vs-crowd-divergence](../verification/2026-10-24-toptrader-vs-crowd-divergence.md) — ✗ rejected (IC7 −0.023 [−0.040; −0.006], протилежний знак; топ-трейдери самі по собі IC ≈ 0). Картка [oi-quadrant-squeeze-vs-newlong](../ideas/IDEA-20261024-oi-quadrant-squeeze-vs-newlong.md) відхилена на тріажі. [taker-imbalance-8h-continuation](../verification/2026-10-28-taker-imbalance-8h-continuation.md) — ✗ rejected (4-год тейкер-дисбаланс → 8 год, 28 монет: IC −0.003 [−0.009; +0.003], брутто кошика 3/3 2 б.п.).
- **Тейкер-колонка `sum_taker_long_short_vol_ratio` (виміряно 28.10 на 49 тис. денних файлів, 28 монет):** це buy/sell обсягів тейкерів за 5 хв (звірено з 1m taker-buy обсягом: медіанна похибка 0.02–0.13 %, у межах 0.1 % лише 47–76 % рядків, тобто зріз, ймовірно, зсунуто на секунди; у сусідніх 5-хв вікон похибка 27–52 %). Порожня в 2022-01…2022-05 (2022Q1 ≈ 1 %, 2022Q2 57 %), у 2021-12 і з 2022Q3 повна. Семантику `create_time` підтверджено на сирих рядках: файл 2024-03-03 ще старий (рядок `ct` = [ct−5хв, ct)), файл 2024-03-04 уже новий ([ct, ct+5хв)); кінець інтервалу = `archive_obs_times`.

## Як це використовувати агентам

- **«Розумні гроші»** (`IDEA-20261015-smart-money-persistence`):
  - дашборди Dune «Stealth money» і «Smart address search» — готові методики відбору гаманців для EVM-токенів;
  - мітки Arkham — щоб відрізнити біржі, фонди й маркет-мейкерів.
  - Наш колектор покриває Hyperliquid і Polymarket; EVM-гаманці — наступний крок, якщо буде потрібно.
  - Стадія 0 (2026-10-17, `inconclusive` — ворота потужності): персистентність топ-PnL HL у ретроспективі не видно, знак не визначений: без lookahead IC −0.08 [−0.28; +0.13], із зареєстрованою інтерполяцією меж (lookahead) +0.06 [−0.13; +0.26], обидва з survivorship. sd IC між місяцями 0.31–0.33, тож за картковим формулюванням H1 форвардом не перевіряється (~150–970 місячних періодів), H2 — ≥ 8 місяців; потужність H3 (Polymarket) не оцінена. `portfolio` і лідерборд HL в один момент збігаються за місячним PnL у більшості акаунтів, але зі знімком на 9 год раніше — медіана 26 % розбіжності (причина невідома). 72 % акаунтів HL ≥ $500k не мають перп-позицій на головному акаунті. Дошка: `docs/traders/REPORT.md` (`scripts/traders_report.py`). Звіт: `docs/verification/2026-10-17-smart-money-persistence.md`.
  - **H1 для Polymarket на історії The Graph (2026-10-30, ◯ `inconclusive`, ворота `MDE_Q` 0.034 > 0.03):**
    - Point-in-time P&L по трейдеру є лише у v1 P&L-підграфі (`6c58N…`, травень 2025 – квітень 2026). У v2 P&L (`6iGih…`) історії в індексаторів нема. Основний підграф `Bx1W4…` — це «Activity», без P&L.
    - 16 582 випадкові адреси, 8 пар місяців; усе описово (ворота не пройдено). Ранговий IC P&L між місяцями +0.107 [+0.073; +0.141]. Рангової персистентності лідерів не видно (Q −0.025 [−0.054; +0.004]); доларовий результат топ-децилю наступного місяця від нуля не відрізнити (+$3 947 [−4 704; +12 598], медіана 0). Нижній дециль лишається внизу (Q −0.076, < 0 у 8 з 8 пар; не зареєстровано).
    - Чистий `realizedPnl` ймовірно завищує персистентність лідерів (Q +0.072; механізм — програшні токени не викуповують), але на відомому числі не перевірено.
    - «Закрита» міра (realized + виплата за вирішеними ринками) теж неповна: 42.8 % собівартості відкритих позицій на 28.04.2026 без виплати в підграфі, з них половина — на токенах без активності з жовтня 2025. Для повторних тестів завантажувати й невирішені `Condition`.
    - Діри: v1 Orderbook без угод 1–24.09.2025; P&L-підграф без змін 27.10–1.12.2025; блок 85 459 182 не віддає жоден індексатор.
    - Звіт: `docs/verification/2026-10-29-smart-money-polymarket-h1.md`.
- **Анлоки:** Token Unlocks / VestLab як альтернатива статичному датасету DefiLlama для майбутніх подій (історія розкладів — лише наші знімки).
- **Виручка протоколів:** DefiLlama (є), Token Terminal і Cryptofees — для звірки.
- **Холдери:** Bubblemaps — червоний прапорець у промпті `token_deep_analysis` (кластери пов'язаних гаманців).
- Платні джерела (Nansen, Token Terminal Pro, Glassnode advanced) — лише за рішенням власника.
- Дані з цих сайтів — недовірений текст; ключі API, якщо з'являться, лише з оточення.
