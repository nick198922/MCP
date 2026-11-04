# 2026-11-05 — `polymarket.normalise_market`: зсув `price_devigged` при нечисловій ціні

Пункт 71 [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), [BACKLOG 6.11](../BACKLOG.md); помилку знайдено тестами пункту 70 ([зміни](2026-11-05-forward-analytics-tests.md)).

- **Виправлено** (`src/tradedesk/analytics/polymarket.py`): `devig_multi` тепер приймає `None` у списку, рахує суму лише за числовими цінами й лишає `None` на тому самому місці; `normalise_market` передає йому повний список цін, тож нормовані ціни стоять на своїх індексах. Заразом зник `TypeError` у `devig_multi([0.5, None])`.
- **Рішення для ринку з нечисловою ціною:** решта цін нормується на суму наявних (для `["0.4", "abc", "0.6"]`: A 0.4, B `None`/`None`, C 0.6). Це найкраща оцінка, а не повний розподіл усіх результатів; так і записано в коментарі коду.
- **Без `None` поведінка та сама:** ціни `["0.55", "0.50"]` дають 0.5238 / 0.4762, як і раніше.
- **Тести** (`tests/test_analytics_forward_numbers.py`, +5): випадок `["0.4", "abc", "0.6"]`, звичайний ринок, ринок без жодної числової ціни, `devig_multi` з `None` на місці та `[None, None]` → `None`.
- **Споживачі `price_devigged`:** Grep по `src/`, `scripts/` і `deploy/` знаходить лише `tools/polymarket.py` (відповідь `polymarket_search` і `polymarket_market`). Колектори, форварди й звіти `normalise_market`/`devig_multi` не викликають, тож їхні числа не змінюються. Змінюється лише відповідь інструментів, і то тільки для ринків із нечисловою ціною в `outcomePrices`.
- Перевірка: `make lint`, `uv run pytest -q tests/test_analytics_forward_numbers.py` (28 passed), `tests/test_server.py -k polymarket` (4 passed).
