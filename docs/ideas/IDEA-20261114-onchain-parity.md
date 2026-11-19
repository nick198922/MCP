# IDEA-20261114-onchain-parity: ончейн-інструменти MCP як у DOUBLETOP — основа для ончейн-торгівлі

Статус: accepted (рішення власника 2026-11-14, [Q-20261114-1](../QUESTIONS.md): «Ну я би ончейн в майбутньому також хотів використовувати для ончейн торгівлі. Тому варто зробити все як у МСР 2топ.» — «Так»)
Пріоритет: власник
Порядок: 2
<!-- proposed → accepted | rejected → testing → confirmed | rejected | inconclusive → built -->

## Навіщо
За порівнянням аудиту 14.11 ([звіт](../reviews/2026-11-14-weekly-audit.md), «Порівняння з DOUBLETOP»; каталог [`docs/reference/doubletop-catalog.md`](../reference/doubletop-catalog.md)) у DOUBLETOP ≈ 25 ончейн-інструментів (Etherscan 8, Helius 5, mempool 5, гаманці й ризики `dt_edu`, пули DefiLlama), у нас 4 (`dex_pairs`, `contract_risk`, `chain_metrics`, `pendle_*`). Власник хоче ончейн-торгівлю в майбутньому; спершу — інструменти даних того ж обсягу, що в DOUBLETOP, за нашими правилами (провайдер у `providers/`, `Http`, конверт з `evidence`, тести на реальних зразках). Це інструменти, а не стратегія: ончейн-ідеї — окремими картками після.

Порядок 2: після поточних карток власника з Порядком 0–1 (X-трейдери, опціонний плейбук, доступ до MCP, прибирання MCP).

## Прогрес
Пункти по одному за прогін; кожен — розробник → критик C, за інваріантами 5–6 `CLAUDE.md` (опис англійською з одиницями, `READ_ONLY`, `EXPECTED_TOOLS`, happy-path і збій провайдера, зразок у `tests/fixtures/` з датою й джерелом, README / PARITY / `docs/changes/`). Живий зразок відповіді — з сервера (він бачить мережу); через MCP DOUBLETOP — лише для порівняння форми.

- [x] 1. **Мапа паритету:** таблиця в цій картці — кожен ончейн-інструмент DOUBLETOP → наш аналог (є / частково / нема), джерело даних (безкоштовний API, ключ), ціна, чи потрібен ключ; пріоритет — що корисне для торгівлі (рух китів, біржові потоки, топ-холдери, нові пули й ліквідність, ризик токена, комісії мереж). Ключі: Etherscan і Dune — крок `scripts/server/onchain-keys.sh` (25.10); Helius (Solana) — новий безкоштовний ключ від власника: питання в `docs/QUESTIONS.md`, коли дійде черга. _19.11: розділ «Мапа паритету» нижче — 40 інструментів DOUBLETOP: є 2, частково 11, нема 27; P1 — перекази адрес Etherscan, ризик токена, пули DefiLlama; питання про Helius ще не потрібне (пункт 4 останній)._
- [ ] 2. **EVM (Etherscan v2, мульти-чейн):** баланс нативний / ERC-20, транзакції й ERC-20 перекази адреси, інформація про токен, топ-холдери, вихідний код контракту — інструменти `onchain_*` у `tools/` (один провайдер `providers/etherscan.py`).
- [ ] 3. **Bitcoin (mempool.space, без ключа):** комісії, стан мемпулу, останній блок, складність / хешрейт, огляд адреси.
- [ ] 4. **Solana (Helius):** актив, баланс, підписи адреси, транзакція, пошук активів — після ключа власника.
- [ ] 5. **DefiLlama пули й ризики:** пули з APY / TVL і їхня історія (у нас частково через `providers/defillama.py`), ризик токена / адреси (GoPlus у нас є — звести з `contract_risk`).
- [ ] 6. **Огляд:** що з цього дає сигнал для торгівлі (рух на біржі, кити, нові пули) — короткий розділ у `docs/knowledge/defi.md` і 1–3 картки ідей (скаут), якщо є механізм.

## Мапа паритету
Складено 19.11 з документів і коду, без мережі. Джерела: [каталог DOUBLETOP](../reference/doubletop-catalog.md) (схеми від 10.10), [`PARITY.md`](../PARITY.md), `src/tradedesk/tools/onchain.py`, `tools/pendle.py`, `tools/coingecko.py`, `providers/` (`defillama`, `dexscreener`, `goplus`, `pendle`), [`api-notes.md`](../reference/api-notes.md), [верифікація 25.10](../verification/2026-10-25-market-makers-onchain.md) (досвід з ключами Etherscan і Dune). Ціни й ліміти тарифів у репозиторії не записані: де їх немає, стоїть «не перевірено». Шляхи ендпоінтів узято з документації провайдерів, живцем їх не викликали. Винятки: Etherscan `tokentx` і `stats/ethprice` та ті шляхи DefiLlama, якими вже ходять наші провайдери.

Ключі:
- **Etherscan V2:** безкоштовний ключ `ETHERSCAN_API_KEY` лежить у `~/.config/tradedesk/onchain.env` з 25.10, на сервері перевірено. Живцем перевірено `tokentx` (`stats/ethprice` — лише виклик у `scripts/server/onchain-keys.sh`, результату в репозиторії нема) (до 1000 рядків за виклик). `getTokenInfo`, `tokenTopHolders` і мітки `nametag` доступні лише на платному тарифі, ціна не перевірено. Те, які мережі V2 відкриває безкоштовний ключ, теж не перевірено.
- **Dune:** ключ є, але `credits_included = 0`, тож будь-який SQL дає HTTP 402. Жоден інструмент DOUBLETOP на Dune не спирається, тому для паритету ключ не потрібен.
- **Helius (Solana):** потрібен новий безкоштовний ключ від власника. Тариф і ліміти не перевірено.
- **mempool.space, DefiLlama (`yields.llama.fi`):** обидва публічні, без ключа. Ендпоінти лише задокументовано ([D] у `api-notes.md`), живцем не перевірено.
- **DefiLlama (`api.llama.fi`, `stablecoins.llama.fi`), DexScreener, GoPlus, Pendle:** без ключа, уже працюють у наших провайдерах.

Шкала пріоритетів: **P1** — дані для рішення про ончейн-угоду (ризик токена, ліквідність / нові пули, рух китів і біржові потоки); **P2** — корисне доповнення (топ-холдери, баланси, комісії мереж, огляд гаманця); **P3** — довідка. Рух китів і біржові потоки — це дані, а не перевірений сигнал: H3 (депозити гаманців ММ на біржі → шорт) дав ◯, 0 подій ([верифікація 25.10](../verification/2026-10-25-market-makers-onchain.md)).

| # | DOUBLETOP | Наш аналог | Статус | Джерело даних | Ключ | Ціна | Для торгівлі | Пріор. |
|---|---|---|---|---|---|---|---|---|
| 1 | etherscan `balanceNative` | — | нема | Etherscan V2 `account/balance` | так, є | безкоштовний ключ; ліміти не перевірено | баланс кита / біржового гаманця | P2 |
| 2 | etherscan `balanceERC20` | — | нема | Etherscan V2 `account/tokenbalance` | так, є | безкоштовний ключ; ліміти не перевірено | баланс кита в токені | P2 |
| 3 | etherscan `normalTxsByAddress` | — | нема | Etherscan V2 `account/txlist` | так, є | безкоштовний ключ; ліміти не перевірено | рух китів (нативна монета), біржові потоки | P1 |
| 4 | etherscan `erc20TxsByAddress` | — (дослідницький код `analytics/mm_onchain.py` + `scripts/verify_market_makers_onchain.py`, не інструмент) | нема | Etherscan V2 `account/tokentx` (перевірено 25.10, до 1000 рядків, пагінація за блоком) | так, є | безкоштовний ключ | рух китів, біржові потоки (у DOUBLETOP обрізано до 5 записів) | P1 |
| 5 | etherscan `transaction` | — | нема | Etherscan V2 `proxy/eth_getTransactionByHash` | так, є | безкоштовний ключ; не перевірено | довідка по одній транзакції | P3 |
| 6 | etherscan `getContractSourceCode` | — (`contract_risk.open_source` бере прапорець GoPlus) | нема | Etherscan V2 `contract/getsourcecode` | так, є | безкоштовний ключ; не перевірено | ризик токена: друге джерело verified source (BACKLOG, підпункт `contract_risk`) | P1 |
| 7 | etherscan `getTokenInfo` | `coin_snapshot` (CoinGecko, за id, а не за адресою контракту: supply, FDV) | частково | Etherscan Pro; CoinGecko Demo-ключ | Pro: платний | Pro: ціна не перевірено | картка токена | P3 |
| 8 | etherscan `tokenTopHolders` | `contract_risk` → `top10_holders` (частка топ-10 від GoPlus, без списку адрес) | частково | Etherscan Pro; GoPlus (без ключа) | Pro: платний | Pro: ціна не перевірено; GoPlus безкоштовно | топ-холдери, концентрація | P2 |
| 9 | helius `get_asset` | — | нема | Helius DAS | так, новий | безкоштовний тариф (за PARITY); ліміти не перевірено | ризик токена Solana (метадані, authority) | P2 |
| 10 | helius `get_balance` | — | нема | Helius RPC | так, новий | те саме | баланс кита Solana | P3 |
| 11 | helius `get_signatures_for_address` | — | нема | Helius RPC | так, новий | те саме | рух китів Solana | P2 |
| 12 | helius `get_transaction` | — | нема | Helius RPC | так, новий | те саме | довідка | P3 |
| 13 | helius `search_assets` | — | нема | Helius DAS | так, новий | те саме | вміст гаманця Solana | P3 |
| 14 | mempool `fees_recommended` | — | нема | mempool.space публічний API | ні | без ключа; ліміти не перевірено | комісії мережі BTC | P2 |
| 15 | mempool `mempool_summary` | — | нема | mempool.space | ні | те саме | завантаженість мережі BTC | P2 |
| 16 | mempool `block_latest` | — | нема | mempool.space | ні | те саме | довідка | P3 |
| 17 | mempool `difficulty_hashrate` | — | нема | mempool.space | ні | те саме | довідка (майнери) | P3 |
| 18 | mempool `address_overview` | — | нема | mempool.space | ні | те саме | баланс / рух кита BTC | P2 |
| 19 | defillama `get_protocols` | — | нема | `api.llama.fi/protocols` | ні | безкоштовно | довідка | P3 |
| 20 | defillama `get_tvl__by_protocol` | — | нема | `api.llama.fi/protocol/{name}` | ні | безкоштовно | приплив / відплив капіталу протоколу | P3 |
| 21 | defillama `get_v2_chains` | — | нема | `api.llama.fi/v2/chains` | ні | безкоштовно | довідка | P3 |
| 22 | defillama `get_v2_historicalChainTvl__by_chain` | `chain_metrics` → `tvl` (останнє значення, зміни 7д / 30д, без ряду) | частково | `api.llama.fi/v2/historicalChainTvl` | ні | безкоштовно | контекст мережі | P3 |
| 23 | defillama `get_stablecoincharts_all` | `stablecoin_supply` (загальна сума + USDT / USDC, без вибору стейблкоїна й без мереж) | частково | `stablecoins.llama.fi` | ні | безкоштовно | ліквідність ринку, описово | P3 |
| 24 | defillama `get_pools` | — | нема | `yields.llama.fi/pools` (великий) | ні | без ключа; не перевірено | нові пули й ліквідність, APY / TVL | P1 |
| 25 | defillama `get_chart__by_pool` | — | нема | `yields.llama.fi/chart/{pool}` | ні | без ключа; не перевірено | історія TVL / APY пулу | P2 |
| 26 | dexscreener `search_blockchain_tokens` | — | нема | DexScreener | ні | безкоштовно | нові токени мережі (у DOUBLETOP «trending» давали фейки, див. PARITY) | P3 |
| 27 | dexscreener `search_pairs_by_token_addresses` | `dex_pairs` (+ `likely_fake`) | є | DexScreener | ні | безкоштовно | ліквідність пулів токена | — |
| 28 | dt_edu `token_card_snapshot` | `coin_snapshot` (ціна, FDV, обсяг) + `dex_pairs` (ліквідність); кількості холдерів нема | частково | CoinGecko, DexScreener | CoinGecko Demo-ключ є | безкоштовно | картка токена | P2 |
| 29 | dt_edu `chain_trend_snapshot` | `chain_metrics` (TVL, DEX-обсяг, fees), `coin_snapshot` (ціна монети), `perps_open_interest` (OI); `gas_price` і `whale_flows` нема, `trending_tokens` свідомо ні | частково | DefiLlama, CoinGecko, біржі | ні / Demo | безкоштовно | огляд мережі; комісії й кити — прогалина | P2 |
| 30 | dt_edu `pair_discovery` | `dex_pairs` | є | DexScreener | ні | безкоштовно | пули токена | — |
| 31 | dt_edu `risk_hints_token` | `contract_risk` (GoPlus + DexScreener, 7 EVM-мереж); нема Solana, honeypot.is, Etherscan source | частково | GoPlus, DexScreener; далі honeypot.is, Etherscan | ні (Etherscan — є) | GoPlus безкоштовно (код 4029 — ліміт); honeypot.is не перевірено | ризик токена | P1 |
| 32 | dt_edu `risk_hints_address` | — | нема | у DOUBLETOP Arkham + Etherscan; мітки Etherscan `nametag` і Arkham платні; публічні дампи міток (`dawsbot/eth-labels`) є в дослідженні 25.10 | платні | Arkham, Etherscan Pro: ціна не перевірено | хто стоїть за адресою (біржа / ММ / фонд) | P2 |
| 33 | dt_edu `wallet_snapshot` | — | нема | Etherscan V2 (eth, base, bnb) + Helius (sol) | так (Etherscan є, Helius новий) | безкоштовні ключі; покриття мереж не перевірено | огляд гаманця кита | P2 |
| 34 | coinstats `wallet_balances` | — (заміна — рядок 33) | нема | CoinStats (кредити) | так | не перевірено | огляд гаманця | P3 |
| 35 | pendle `scan_fixed_yields` | `pendle_fixed_yields` | частково | Pendle API | ні | безкоштовно | fixed yield для carry (поза ончейн-категоріями) | P3 |
| 36 | pendle `yield_curve` | `pendle_yield_curve` (паритет з DT не звірено) | частково | Pendle API | ні | безкоштовно | carry | P3 |
| 37 | pendle `compare_pt_vs_underlying` | поле `implied_minus_underlying` у `pendle_fixed_yields` / `pendle_yield_curve` | частково | Pendle API | ні | безкоштовно | carry | P3 |
| 38 | pendle `expiry_calendar` | `pendle_expiry_calendar` (без `wallet`) | частково | Pendle API | ні | безкоштовно | carry | P3 |
| 39 | pendle `liquidity_depth` | — (BACKLOG 5.4: інший ендпоінт, потрібен живий зразок) | нема | Pendle API (котирування свопу) | ні | безкоштовно | ліквідність пулу PT / YT | P3 |
| 40 | pendle `pendle_briefing` | — (ставки Boros, аудит 14.11) | нема | Pendle / Boros | ні | не перевірено | своп на funding для carry | P3 |

**Підсумок:** 40 інструментів DOUBLETOP, з них **є 2**, **частково 11**, **нема 27**. P1 мають 5 рядків. Три головні напрями:
1. **Перекази адреси в Etherscan** (рядки 3–4): рух китів і біржові потоки. Ключ є, `tokentx` перевірено. Входить у пункт 2.
2. **Ризик токена** (рядки 6, 31): verified source з Etherscan (пункт 2) і доповнення `contract_risk` (пункт 5, підпункти BACKLOG).
3. **Пули DefiLlama** (рядки 24–25): нові пули й ліквідність, без ключа. Входить у пункт 5.

Порядок пунктів 2 → 3 → 4 це не змінює. Пункт 2 спирається на ключ, який уже є, пункт 3 ключа не потребує, Helius (пункт 4) іде останнім, і його рядки мають P2–P3. Тому питання власнику про ключ Helius відкладено, доки не дійде черга пункту 4. Пункт 5 має два P1 (рядки 24 і 31) і міг би йти раніше пунктів 3–4; переставляти чи ні, вирішує координатор.

## Критерій «вбити»
Власник скасовує.

## Джерела
Q-20261114-1 і відповідь власника 14.11; аудит 14.11; каталог DOUBLETOP.
