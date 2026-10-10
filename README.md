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

## Інструменти (21)

| Група | Інструмент | Що дає |
|---|---|---|
| Система | `system_health` | стан провайдерів; `probe=true` робить живий пінг |
| Ринок | `market_ticker`, `market_tickers`, `market_ohlcv`, `market_orderbook`, `market_markets`, `market_trades` | котирування (одиночне й batch), свічки з реалізованою вола, стакан зі slippage для заданого розміру, пошук ринків, останні угоди з aggressor-flow (buy/sell, VWAP, CVD) |
| Перпи | `perps_funding_compare` | funding по Binance/Bybit/OKX/Hyperliquid/Deribit, приведений до річних |
| | `perps_open_interest` | OI в USD по майданчиках і частки |
| | `hyperliquid_overview` | скринінг усього Hyperliquid за OI, funding, обсягом |
| Ф'ючерси | `deribit_futures_curve` | basis і term structure (прості та лог. річні) |
| Опціони | `deribit_vol_surface` | ATM IV, RR/Fly 25Δ і 10Δ, очікуваний рух, P/C по експірації |
| | `deribit_option_chain` | ланцюжок з greeks (Black-76, збігається з Deribit) |
| | `deribit_max_pain`, `deribit_gamma_exposure` | max pain, GEX із рівнем flip |
| | `deribit_expected_move` | ціна ATM-страддла в USD, breakeven-и й 1σ-діапазон за експіраціями |
| | `deribit_oi_by_strike` | OI-стіни по страйках: кол/пут/USD-ноцінал, топ-рівні підтримки/опору |
| | `prob_from_options` | ризик-нейтральна ймовірність above/below/touch з урахуванням skew |
| Квант | `odds_devig`, `option_calc`, `touch_probability` | маржа й справедливі ймовірності, калькулятор опціону, barrier-ймовірність |

Усі відповіді мають однаковий конверт `{ok, data, evidence, missing, warnings, as_of}`: кожне число має джерело й час, а збій одного провайдера не валить виклик. Деталі в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

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
