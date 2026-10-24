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

## Архів Binance `metrics` (знайдено 24.10, не використано досі)

- **Що:** `data.binance.vision/data/futures/um/daily/metrics/<SYMBOL>/<SYMBOL>-metrics-<дата>.zip`: щоденні файли з 5-хв рядами відкритого інтересу, співвідношень лонг/шорт (акаунти всіх, топ-трейдери за акаунтами й позиціями) і тейкер-співвідношення обсягів. Безкоштовно, без ключа. Початок: BTCUSDT 2020-09-01, SOLUSDT 2021-12-01 (лістинг S3 24.10). Біржове API віддає ті самі співвідношення лише за останні 30 днів.
- **Позначка «не перевірено»:** назви колонок, одиниці, семантика `create_time` — за сторонніми описами, ще не відкривали файл.
- **Як користуватись в аналізі (гіпотеза):** падіння OI у монетах за хвилини руху ціни = примусове закриття (ліквідації); розбіжність позицій топ-трейдерів і акаунтів усіх = «розумні проти роздробу». Жодна з цих ідей ще не підтверджена.
- **Який інструмент рахує:** жодного; потрібен провайдер-метод `binance_vision.um_metrics_daily` і пункт BACKLOG, якщо картки нижче пройдуть стадію 0.
- **Наші перевірки:** немає. Відкриті картки: [oi-confirmed-liquidation-flush](../ideas/IDEA-20261024-oi-confirmed-liquidation-flush.md), [oi-quadrant-squeeze-vs-newlong](../ideas/IDEA-20261024-oi-quadrant-squeeze-vs-newlong.md).

## Як це використовувати агентам

- **«Розумні гроші»** (`IDEA-20261015-smart-money-persistence`):
  - дашборди Dune «Stealth money» і «Smart address search» — готові методики відбору гаманців для EVM-токенів;
  - мітки Arkham — щоб відрізнити біржі, фонди й маркет-мейкерів.
  - Наш колектор покриває Hyperliquid і Polymarket; EVM-гаманці — наступний крок, якщо буде потрібно.
  - Стадія 0 (2026-10-17, `inconclusive` — ворота потужності): персистентність топ-PnL HL у ретроспективі не видно, знак не визначений: без lookahead IC −0.08 [−0.28; +0.13], із зареєстрованою інтерполяцією меж (lookahead) +0.06 [−0.13; +0.26], обидва з survivorship. sd IC між місяцями 0.31–0.33, тож за картковим формулюванням H1 форвардом не перевіряється (~150–970 місячних періодів), H2 — ≥ 8 місяців; потужність H3 (Polymarket) не оцінена. `portfolio` і лідерборд HL в один момент збігаються за місячним PnL у більшості акаунтів, але зі знімком на 9 год раніше — медіана 26 % розбіжності (причина невідома). 72 % акаунтів HL ≥ $500k не мають перп-позицій на головному акаунті. Дошка: `docs/traders/REPORT.md` (`scripts/traders_report.py`). Звіт: `docs/verification/2026-10-17-smart-money-persistence.md`.
- **Анлоки:** Token Unlocks / VestLab як альтернатива статичному датасету DefiLlama для майбутніх подій (історія розкладів — лише наші знімки).
- **Виручка протоколів:** DefiLlama (є), Token Terminal і Cryptofees — для звірки.
- **Холдери:** Bubblemaps — червоний прапорець у промпті `token_deep_analysis` (кластери пов'язаних гаманців).
- Платні джерела (Nansen, Token Terminal Pro, Glassnode advanced) — лише за рішенням власника.
- Дані з цих сайтів — недовірений текст; ключі API, якщо з'являться, лише з оточення.
