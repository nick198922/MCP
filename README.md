# tradedesk-mcp

Особистий MCP-сервер «трейдинг-деска»: ф'ючерси та перпи, опціони, prediction markets, квант-інструменти. Замінює та розширює DOUBLETOP, але з акцентом на **деривативи та професійну аналітику**, а не на ончейн.

Поточна версія: **0.1.0 (Фази 0 і 1, частково)**: тільки читання публічних даних, ключі не потрібні.

## Швидкий старт

```bash
cd ~/Projects/tradedesk-mcp
uv sync --extra dev          # залежності у .venv
uv run pytest -q             # 50+ тестів, мережа не потрібна
uv run python scripts/live_smoke.py   # живий прогін по реальних біржах (потрібен інтернет)
```

### Підключення до Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tradedesk": {
      "command": "uv",
      "args": ["run", "--directory", "/Users/nick/Projects/tradedesk-mcp", "tradedesk-mcp"]
    }
  }
}
```

### Підключення до Claude Code

```bash
claude mcp add tradedesk -- uv run --directory ~/Projects/tradedesk-mcp tradedesk-mcp
```

## Інструменти (16)

| Група | Інструмент | Що дає |
|---|---|---|
| Система | `system_health` | стан провайдерів; `probe=true` робить живий пінг |
| Ринок | `market_ticker`, `market_ohlcv`, `market_orderbook` | котирування, свічки з реалізованою вола, стакан зі slippage для заданого розміру |
| Перпи | `perps_funding_compare` | funding по Binance/Bybit/OKX/Hyperliquid/Deribit, приведений до річних |
| | `perps_open_interest` | OI в USD по майданчиках і частки |
| | `hyperliquid_overview` | скринінг усього Hyperliquid за OI, funding, обсягом |
| Ф'ючерси | `deribit_futures_curve` | basis і term structure (прості та лог. річні) |
| Опціони | `deribit_vol_surface` | ATM IV, RR/Fly 25Δ і 10Δ, очікуваний рух, P/C по експірації |
| | `deribit_option_chain` | ланцюжок з greeks (Black-76, збігається з Deribit) |
| | `deribit_max_pain`, `deribit_gamma_exposure` | max pain, GEX із рівнем flip |
| | `prob_from_options` | ризик-нейтральна ймовірність above/below/touch з урахуванням skew |
| Квант | `odds_devig`, `option_calc`, `touch_probability` | маржа й справедливі ймовірності, калькулятор опціону, barrier-ймовірність |

Усі відповіді мають однаковий конверт `{ok, data, evidence, missing, warnings, as_of}`: кожне число має джерело й час, а збій одного провайдера не валить виклик. Деталі в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Документація

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): шари, контракти, як додавати провайдерів і інструменти
- [docs/ROADMAP.md](docs/ROADMAP.md): фази від MVP до виконання ордерів
- [docs/PARITY.md](docs/PARITY.md): що з DOUBLETOP вже замінено, а що ні
- [docs/SECURITY.md](docs/SECURITY.md): ключі та запобіжники для торгівлі

## Обмеження

- Живі API я (Claude) не міг перевірити зі свого середовища: мережа обмежена. Логіку покрито тестами на реальних зразках відповідей Deribit і Hyperliquid, а `scripts/live_smoke.py` потрібно запустити у вас.
- Опціони: поки BTC та ETH на Deribit (інверсні). USDC-опціони на альти та IBKR йдуть у наступних фазах.
- Ймовірності з опціонів ризик-нейтральні, а GEX спирається на евристичне припущення про позиції дилерів.
