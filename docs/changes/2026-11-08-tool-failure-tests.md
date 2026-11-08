# 2026-11-08 — тести «збій провайдера → відповідь, не виняток» (пункт 258)

Інваріанти 1 і 5 `CLAUDE.md`. Разовий скрипт-аудит (не в git) зіставив 109 інструментів `EXPECTED_TOOLS` з тестами в `tests/` і повторно прогнав загальний охоронець збоїв, щоб побачити, які інструменти там узагалі не доходять до провайдера.

- Журнал, carry-папір (`carry_paper_*`), Polymarket (`polymarket_*`, зокрема `polymarket_options_gap`), Coinalyze, `plan_review`, `assistant_paper_*`, `forecast_log_status` окремі тести збою вже мали.
- Окремого тесту не мали 5 інструментів із провайдером, їх додано в `tests/test_tool_failure_extra.py` (7 тестів):
  - `market_regime` і `range_detect` з `source='live'`: загальні охоронці кличуть їх лише з `source='store'`, тож живий шлях ccxt ще не зустрічав біржі, що лежить;
  - `market_trades` (okx 429, `rate_limited`), `strategy_backtest_momentum` (біржа лежить);
  - `calendar_expiries`: Deribit лежить → `missing` для кожної валюти, розклад funding і макроподії повертаються; без макроподій `ok=false`.
- **Знайдено баг:** `market_regime(source='live')` кидав `TypeError` назовні, коли біржа віддала бар із `None` у цінах (`float(None)`). Тепер він бере `_live_bars` (як `range_detect`): битий рядок відкидається з попередженням `N malformed bars ... dropped`. Тест — `test_market_regime_live_drops_a_bar_with_none_prices_instead_of_raising`.
- Решта (35 інструментів без провайдера: лише сховище або обчислення) — BACKLOG 6.53.
- Числа форвардів не змінились; `execution/` не чіпали.
