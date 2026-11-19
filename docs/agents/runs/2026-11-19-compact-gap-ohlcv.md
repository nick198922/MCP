**Крок:** постійна картка `standing-improvements`, новий пункт 390 (поповнення з BACKLOG «великі відповіді за замовчуванням»). Картки власника зараз заблоковані: playbook (черга порожня), agents-mcp-access (Q-20261114-2), x-traders (гілка btc-charlie чекає), structural-edges 8f (дані до 2028), book-walls (чекає сервер/дату), sieve на паузі.

**Результат:**
• `polymarket_options_gap`: 17.8 → 7.5 тис. символів за замовчуванням (top 10, `flags`, повний вивід — `include_details=true`).
• `market_ohlcv`: 13.2 → 1.4 тис. (10 свічок + зведення, повний ряд — `include_series=true`); знімок асистента просить повний ряд.
• `make lint` і `make test` (10 500 passed, 3 skipped) — власний прогін.

**Критик:** C (sonnet) — approve; зауваження про тест знімка й запас розміру виправлено одним раундом.

**Далі:** `owner`. Запас типової відповіді `polymarket_options_gap` ≈ 450 символів; наступний у списку — `cycle_regime`.
