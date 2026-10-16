# tradedesk-mcp

Особистий MCP-сервер «трейдинг-деска»: ф'ючерси та перпи, опціони, prediction markets, квант-інструменти. Замінює та розширює DOUBLETOP, але з акцентом на **деривативи та професійну аналітику**, а не на ончейн.

Поточна версія: **0.1.0 (Фази 0 і 1, частково)**: тільки читання публічних даних, ключі не потрібні.

## Швидкий старт

```bash
git clone <URL-репозиторію> tradedesk-mcp && cd tradedesk-mcp
make install     # uv sync --extra dev
make test        # 50+ тестів, мережа не потрібна
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

## Інструменти (28)

| Група | Інструмент | Що дає |
|---|---|---|
| Система | `system_health` | стан провайдерів; `probe=true` робить живий пінг |
| Ринок | `market_ticker`, `market_tickers`, `market_ohlcv`, `market_orderbook`, `market_markets`, `market_trades` | котирування (одиночне й batch), свічки з реалізованою вола, стакан зі slippage для заданого розміру, пошук ринків, останні угоди з aggressor-flow (buy/sell, VWAP, CVD) |
| Розмітка графіка | `chart_markup` | рівні й зони курсу DoubleTop зі свічок біржі: EMA Trend 13/21/34, Daily (EMA 100/200 з 4h), Weekly (EMA 100/200 з 1d), VWAP з якорем і смугою 1σ, профіль об'єму (POC/VAH/VAL), свінги й структура (BOS лише закриттям, тінь — sweep), неторкнута ліквідність і рівні хаї/лої, FVG зі станом заповнення, OB з прапорцями валідності й брейкери, premium/discount і OTE, ATR/RSI, азійський рейндж і свип на відкритті Франкфурта/Лондона; BingX-форекс приймає `EURUSD` |
| Перпи | `perps_funding_compare` | funding по Binance/Bybit/OKX/Hyperliquid/Deribit, приведений до річних |
| | `perps_funding_history` | історія funding по всіх venues: carry 7d/30d, z-score, перцентиль, polarity flips; крос-венюз: найдорожчий carry, найбільший |z| |
| | `funding_carry_scan` | скан funding carry по біржі: поточний і trailing 7d/30d APR (сума фактичних виплат), статус відносно порогів входу/виходу (вхід за замовчуванням 20%), окупність комісій у днях, чи є спот; за замовчуванням лише крипто-перпи зі спотом. Перевірку на історії див. у `docs/verification/2026-10-12-funding-carry.md` |
| | `funding_spread_scan` | крос-біржовий спред funding для carry перп-перп (Binance/Bybit/OKX): на якій біржі шорт, на якій лонг, trailing і поточний спред, окупність витрат, розрив цін, діапазон за 7 днів, прапорці ризику (різні токени під тікером, funding на ліміті, розворот спреду, різні інтервали). Бектест: `docs/verification/2026-10-13-funding-carry-perp-perp.md` |
| | `perps_long_short_ratio` | роздрібна L/S по Binance і Bybit (account-based fractions); z-score, divergence між venues |
| | `perps_oi_history` | OI history (Binance в USD, Bybit в base) з latest/mean/change24h/z-score; крос-венюз: biggest gain/drop |
| | `perps_open_interest` | OI в USD по майданчиках і частки |
| | `perps_basis_compare` | крос-венюз perp premium (bps) + Deribit dated-futures basis, cash-and-carry snapshot |
| Hyperliquid | `hyperliquid_overview` | скринінг усього Hyperliquid за OI, funding, обсягом |
| | `hyperliquid_candles` | OHLCV + реалізована вола |
| | `hyperliquid_book` | L2-стакан (20 рівнів) + slippage під розмір |
| | `hyperliquid_trades` | останні угоди з aggressor-flow (B/A → buy/sell, VWAP, CVD) |
| | `hyperliquid_funding_history` | історія funding + mean 7d/30d, z-score, percentile, річний carry, polarity flips |
| Ф'ючерси | `deribit_futures_curve` | basis і term structure (прості та лог. річні) |
| Опціони | `deribit_vol_surface` | ATM IV, RR/Fly 25Δ і 10Δ, очікуваний рух, P/C по експірації |
| | `deribit_option_chain` | ланцюжок з greeks (Black-76, збігається з Deribit) |
| | `deribit_max_pain`, `deribit_gamma_exposure` | max pain, GEX із рівнем flip |
| | `deribit_expected_move` | ціна ATM-страддла в USD, breakeven-и й 1σ-діапазон за експіраціями |
| | `deribit_oi_by_strike` | OI-стіни по страйках: кол/пут/USD-ноцінал, топ-рівні підтримки/опору |
| | `prob_from_options` | ризик-нейтральна ймовірність above/below/touch з урахуванням skew |
| | `deribit_dvol` | DVOL (річний %) time series + latest / mean / stdev / min / max / change 24h / 7d |
| | `deribit_vrp` | премія за волатильність: DVOL проти реалізованої (7д/30д), перцентиль DVOL за історію і за рік, історична премія і результат хеджованого продажу ATM-стреддла, у т.ч. по квінтилях рівня DVOL. Перевірку на історії див. у `docs/verification/2026-10-13-vrp.md` |
| | `deribit_atm_term` | ATM-IV term structure (contango/backwardation) по експіраціях |
| | `deribit_skew_term` | term structure 25Δ-RR і 25Δ-fly (puts/calls richer, flattening/steepening) |
| | `deribit_largest_trades` | останні крупні трейди (options/futures), фільтр блок-угод, buy/sell imbalance |
| Квант | `odds_devig`, `option_calc`, `touch_probability`, `fib_retracement` | маржа й справедливі ймовірності, калькулятор опціону, barrier-ймовірність, fib-retracement ладдер (23.6/38.2/50/61.8/78.6%) |
| Настрій | `sentiment_fear_greed` | Crypto Fear & Greed (alternative.me): latest + zone, mean 7d/30d, зміни, частка днів у кожній зоні |
| Календар | `calendar_expiries` | експірації Deribit (daily/weekly/monthly/quarterly), найближчі funding-моменти, макро-події (FOMC/CPI/NFP) |
| Події (Фаза 1) | `events_add`, `events_list`, `events_seed_load`, `event_study` | локальна база макро-подій (FOMC/CPI/NFP); seed 2024-2026 запаковано у wheel; windowed-study поверх `ohlcv_hist` (hit rate, p25/50/75) |

MCP-промпти: `token_deep_analysis`, `fomc_playbook`, `pre_trade_checklist` (план угоди: контекст, розмір від рахунку й ризику, вердикт, запис у журнал), `journal_review` (щомісячний розбір журналу за тегами), `morning_briefing` (шаблони з підказками, які `tradedesk-mcp` інструменти викликати).
| Prediction markets | `polymarket_search`, `polymarket_market`, `polymarket_history` | Polymarket Gamma+CLOB: пошук, карта ринку з order-book мідами, історія цін yes/no |
| Макро/ринок | `coin_snapshot`, `global_market`, `top_coins` | CoinGecko: ціна/mcap/FDV/supply/ATH, глобальна домінація BTC/ETH/alts, топ-N монет |
| Історія (Фаза 2) | `iv_rank`, `funding_zscore`, `skew_percentile`, `alerts_recent`, `ohlcv_history`, `market_regime`, `range_detect`, `range_setup`, `range_setup_scan` | ATM-IV rank, funding z-score, 25Δ/10Δ RR percentile, журнал алертів, OHLCV, up/down/chop, рейнджі + sweeps + deviations + accumulation/distribution, SFP-сетап (entry/stop/TP/RR), сканер SFP-сетапів по списку тикерів |
| Журнал (Фаза 2) | `journal_add`, `journal_list`, `journal_close`, `journal_stats` | трекінг угод у локальній SQLite: PnL, R-multiple, теги, win-rate / expectancy / avg R |
| Паперовий carry перп-перп | `carry_paper_open`, `carry_paper_status`, `carry_paper_close` | паперові пари (лонг на одній біржі, шорт на іншій) у локальній SQLite, без ордерів: funding обох ніг, price P&L, комісії і проковзування як у бектесті, стоп 1/плече, сигнал виходу; закриті пари дзеркаляться в журнал з тегом `pp-paper` |
| Позиції (Фаза 2) | `positions_add`, `positions_list`, `positions_close`, `positions_risk` | ручний портфель + сценарії PnL (±10 % спот, ±5 vol-points, crash+vol-up): net δ/γ/vega/θ |
| Ліквідації (Фаза 2) | `liquidations_summary` | WebSocket збирач Binance `!forceOrder@arr` + агрегати (longs/shorts USD, imbalance, per-symbol, top) |
| Правила (Фаза 3) | `rules_add`, `rules_list`, `rules_test`, `rules_disable`, `rules_presets_load` | DSL правил alert'ів (безпечний AST + metric funcs); 8 готових пресетів у wheel; зберігає й читається alerts job'ом |
| Стратегії (Фаза 3) | `strategy_payoff`, `strategy_straddle`, `strategy_strangle`, `strategy_risk_reversal`, `strategy_iron_condor`, `strategy_cash_and_carry`, `strategy_funding_carry`, `strategy_scenario_matrix` | многоногий payoff + шаблони + multi-horizon MTM-grid (τ-aware Black-76): P&L крива, breakevens, max profit/loss, δ/γ/vega/θ, MTM, risk-neutral PoP, scenario matrix |
| Бектест (Фаза 3) | `strategy_backtest_momentum` | walk-forward momentum breakout на ccxt OHLCV; fee+slippage у bps; equity, trades, Sharpe/Sortino/maxDD/win-rate |

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
