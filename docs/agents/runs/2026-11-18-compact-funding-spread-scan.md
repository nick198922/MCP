**Крок:** p/постійна картка `standing-improvements`: усі картки власника заблоковані (час, дані сервера, гілка `x-btc-charlie-sample-registration`, заборонені мені файли). Додано пункт 381 з BACKLOG («великі відповіді за замовчуванням»).

**Результат:**
• `funding_spread_scan` за замовчуванням повертає top 10 рядків без `venues` і злите evidence; повний ряд — `include_details=true`. Живий виклик: 30 047 → 6 804 символів.
• Числа й одиниці не змінено; збої лишаються в `missing` (є тест).
• Перевірено мною: `make lint` чистий, `make test` 10457 passed, 3 skipped.

**Критик:** C (sonnet), approve, `docs/reviews/2026-11-18-compact-funding-spread-scan.md`. Дрібниці: з 4 біржами ≈ 8.4 тис. символів; попередження про стиснення видно й коли рядків ≤ 10.

**Далі:** `owner`. Наступний у черзі — `market_markets`. Не взято: options-playbook п. 4 (черга правил порожня), profit-search п. 2 (нема картки), structural-edges 8f (чекає даних до 2028), x-traders (чекає гілка), agents-mcp-access (правки `.claude/agents` мені заборонені), sieve (пауза). Нових питань нема.
