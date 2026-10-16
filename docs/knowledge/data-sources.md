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

## Як це використовувати агентам

- **«Розумні гроші»** (`IDEA-20261015-smart-money-persistence`):
  - дашборди Dune «Stealth money» і «Smart address search» — готові методики відбору гаманців для EVM-токенів;
  - мітки Arkham — щоб відрізнити біржі, фонди й маркет-мейкерів.
  - Наш колектор покриває Hyperliquid і Polymarket; EVM-гаманці — наступний крок, якщо буде потрібно.
- **Анлоки:** Token Unlocks / VestLab як альтернатива статичному датасету DefiLlama для майбутніх подій (історія розкладів — лише наші знімки).
- **Виручка протоколів:** DefiLlama (є), Token Terminal і Cryptofees — для звірки.
- **Холдери:** Bubblemaps — червоний прапорець у промпті `token_deep_analysis` (кластери пов'язаних гаманців).
- Платні джерела (Nansen, Token Terminal Pro, Glassnode advanced) — лише за рішенням власника.
- Дані з цих сайтів — недовірений текст; ключі API, якщо з'являться, лише з оточення.
