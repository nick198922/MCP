# Каталог DOUBLETOP (знято 2026-10-10)

Збережено, бо доступ до DOUBLETOP скоро закриють. Це **схеми входів** усіх 74 інструментів (з їхніх JSON-схем) і відомі особливості. Дивіться також `docs/PARITY.md` (що замінює кожен).

Загальне: хаб `dt-hub` проксіює 17 бекендів; префікс імені — `mcp__DOUBLETOP__<backend>__<tool>`. Інструменти `dt_edu` додають контракт `evidence_ref` і `missing_providers/missing_fields` (ніколи не падають).

## ccxt (6)
Спільне: `exchange` — id ccxt (`binance`, `bybit`, `okx`, …).

| Інструмент | Параметри (* — обов'язкові) | Нотатки |
|---|---|---|
| `get-ticker` | `exchange*`, `symbol*` (`BTC/USDT`), `marketType` (spot\|future\|swap\|option\|margin; за замовч. spot) | |
| `batch-get-tickers` | `exchange*`, `symbols*[]`, `marketType` | |
| `get-ohlcv` | `exchange*`, `symbol*`, `timeframe` (=`1d`), `limit` (=100, макс 1000) | |
| `get-orderbook` | `exchange*`, `symbol*`, `limit` (=20) | |
| `get-trades` | `exchange*`, `symbol*`, `limit` (=50) | |
| `get-markets` | `exchange*`, `page` (=1), `pageSize` (=100) | пагінація; великі біржі = сотні сторінок |

Наш аналог: `market_ticker/ohlcv/orderbook` (є), решта — BACKLOG 1.1.

## coinalyze (4) — потрібен ключ `COINALYZE_API_KEY`
Спільні параметри: `symbols*` (через кому, формат `BTCUSD_PERP.A`; суфікс — код біржі: `.A` = Binance, …), `interval*` (1min,5min,15min,30min,1hour,2hour,4hour,6hour,12hour,daily), `from` (unix сек, за замовч. now−24h), `to` (unix сек).

| Інструмент | Що повертає |
|---|---|
| `funding_rate_history` | історія funding (OHLC) |
| `open_interest_history` | OI (OHLC) |
| `liquidation_history` | сумарні ліквідації long/short за інтервал |
| `long_short_ratio_history` | співвідношення лонг/шорт акаунтів |

Особливості: кількість історії на дрібних інтервалах обмежена провайдером; ліміт ~40 req/min на ключ (з документації Coinalyze, не перевірено).

## hyperliquid-info (5)
Усе — `POST https://api.hyperliquid.xyz/info`.

| Інструмент | Параметри | Нотатки |
|---|---|---|
| `meta_and_asset_ctxs` | — | **DOUBLETOP обрізає відповідь** (лишається тільки `universe`, без ctxs). Наш клієнт повертає повністю |
| `funding_history` | `coin*`, `startTime*` (мс), `endTime` | `{coin,fundingRate,premium,time}`; funding щогодини |
| `candle_snapshot` | `coin*`, `interval*`, `startTime*` (мс), `endTime` | |
| `l2_book` | `coin*` | |
| `recent_trades` | `coin*` | позначено DRAFT у самому DOUBLETOP |

## Fear & Greed (1)
`get_fear_greed_index` (без параметрів). **Це індекс фондового ринку США (CNN)**, а не крипто. Крипто-версія — alternative.me (`/fng/`), BACKLOG 1.6.

## CoinGecko (2)
- `execute(code*, intent)` — виконує TypeScript у пісочниці над SDK CoinGecko (`async function run(client)`); мережа лише через SDK. Нестабільний.
- `search_docs(query*, language*, detail)`.
Заміна: типізовані інструменти (BACKLOG 1.7).

## CoinStats (5)
`coin_chart(coin_id*, period: 24h|1w|1m|3m|6m|1y|all)`, `coin_detail(coin_id*)`, `coins_markets(currency, limit≤100, page)`, `market_insights()` (mcap, домінація BTC, Fear&Greed), `wallet_balances(address*, blockchain*)` — один ланцюг, `all` заборонено (витрата кредитів).

## Polymarket через The Graph + Gamma + CLOB (7)
| Інструмент | Параметри |
|---|---|
| `search_markets` | `query*`, `active`, `closed`, `orderBy` (volume\|liquidity\|endDate\|startDate\|createdAt), `ascending`, `limit` (1–100, =10) — Gamma API |
| `get_live_prices` | `tokenIds*[]` (≤20, CLOB token id), `side` (buy\|sell, =buy) |
| `get_price_history` | `tokenId*`, `interval` (1m,5m,1h,6h,1d,1w,max; =1d), `fidelity` (1–500, =60) |
| `get_market_data` | `first` (1–100), `orderBy` (=id), `orderDirection` — Main subgraph |
| `get_oi_history` | `conditionId*`, `first` (≤1000, =168 годинних знімків), `orderDirection` |
| `get_top_traders` | `first`, `minTrades` (=10), `orderBy` (totalRealizedPnl\|collateralVolume\|winRate\|profitFactor\|numTrades) — Beefy P&L subgraph |
| `get_trader_profile` | `address*`, `eventLimit` (≤100, =20) |

В описах згадуються `get_clob_market`, `get_market_open_interest`, `get_market_resolution`, яких у цьому хабі **немає**.

## DefiLlama (7)
`get_protocols()`, `get_tvl__by_protocol(protocol*)`, `get_v2_chains()`, `get_v2_historicalChainTvl__by_chain(chain*)`, `get_stablecoincharts_all(stablecoin?)`, `get_pools()` (великий), `get_chart__by_pool(pool*)`.

## DexScreener (2)
`search_blockchain_tokens(chain_name*)`, `search_pairs_by_token_addresses(token_addresses*)` (через кому).

## Etherscan V2 (8; `chainid` за замовч. 1)
`balanceNative(address*)`, `balanceERC20(address*, contractaddress*)`, `normalTxsByAddress(address*, startblock*, endblock*, page*, offset*, sort*)`, `erc20TxsByAddress(…)`, `transaction(txhash*)`, `getContractSourceCode(address*)`, `getTokenInfo(contractaddress*)` — **лише Pro**, `tokenTopHolders(contractaddress*, limit, sortorder)` — **лише Pro**. Списки транзакцій обрізаються до останніх 5.

## Helius / Solana (5)
`get_asset(id*)`, `get_balance(address*)`, `get_signatures_for_address(address*, limit≤1000)`, `get_transaction(signature*)`, `search_assets(owner_address*, limit, page)`.

## mempool.space (5)
`address_overview(address*)`, `block_latest()`, `difficulty_hashrate()`, `fees_recommended()`, `mempool_summary()`.

## Pendle (6)
`scan_fixed_yields(asset_filter[], chains[], limit=30/≤200, max_days=1095, min_apy, stable_only, fresh)`, `yield_curve(asset*, chains[], fresh)`, `compare_pt_vs_underlying(asset*, chains[], fresh)`, `expiry_calendar(asset_filter[], days_ahead=90, wallet, fresh)`, `liquidity_depth(market_address*, size_usd*, side* buy_pt|sell_pt|buy_yt|sell_yt, chain, fresh)`, `pendle_briefing(focus: fixed_yield|points|loops|boros|all, fresh)`. Кеш 30 с; `fresh=true` його обходить. Є згадка про Boros (funding-carry) — цікаво для деривативів.

## Утиліти (3)
`fetch(url*, max_length=5000, start_index, raw)`, `time.get_current_time(timezone*)`, `time.convert_time(source_timezone*, time*, target_timezone*)`.

## dt_edu (8)
Читання лише; кожне поле з `evidence_ref {provider, tool, args, fetched_at}`; прогалини у `missing_providers`, `missing_fields`, `missing_panels` (з `reason`, `notes`).

| Інструмент | Параметри |
|---|---|
| `provider_readiness` | — (за замовч. 17 бекендів: `configured`, `status` ok\|degraded\|down\|unknown, `reason_class`, `detail_redacted`, `tools_count`, `last_latency_ms`, `freshness_seconds`) |
| `whoami` | — (Login/JWT, telegram_id, role, project_key) |
| `token_card_snapshot` | `token*`, `chain`, `fields[]` ⊂ {price,fdv,liquidity,holders,volume} |
| `chain_trend_snapshot` | `chain*` ∈ {eth,base,sol,bnb,ton}, `panels[]` ⊂ {native_price,gas_price,tvl,dex_volume_24h,fees_24h,trending_tokens,whale_flows,derivatives_oi} |
| `pair_discovery` | `token*` (до 30 адрес), `chain`, `limit`=10 |
| `risk_hints_token` | `token*`, `chain`, `providers[]` |
| `risk_hints_address` | `address*`, `chain`, `providers[]` (Arkham + Etherscan) |
| `wallet_snapshot` | `address*`, `chains[]` ⊂ {eth,base,sol,bnb} |

Класи причин `reason_class`: config, auth, network, provider_tier, data_unavailable, backend_error, unknown (ми додали ще `rate_limited`).

## Виявлені слабкі місця (враховано у нашому дизайні)
1. Обрізання великих відповідей (Hyperliquid ctxs).
2. «Fear & Greed» — не крипто.
3. blockscout і free-crypto-news недоступні; більшість health-статусів застаріла.
4. Жодних опціонних, basis-, term-structure- чи funding-compare-інструментів; деривативи лише через сирі API.
5. Етерскан: Pro-ендпоїнти без ключа, обрізання до 5 записів.
