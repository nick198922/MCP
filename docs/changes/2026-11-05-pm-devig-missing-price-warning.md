# 2026-11-05 — Polymarket: тести попередження про відсутню ціну

Пункт 90 [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), [рев'ю](../reviews/2026-11-05-pm-devig-index-shift.md), зауваження 5.

- **Код не змінено.** Попередження вже додано пунктом 78 ([зміни](2026-11-05-review-nits-0405.md), коміт `a020e4da`): `tools/polymarket.py` (`_partial_devig`, `PARTIAL_DEVIG`) пише в `warnings` «some outcomes have no numeric price…», коли в частини результатів `price is None`, а в інших `price_devigged` пораховано. `polymarket_market` дає одне попередження, `polymarket_search` — одне з id таких ринків. Опис інструментів не змінено.
- **Тести** (`tests/test_server.py`): спільний тест `test_polymarket_partial_prices_warn` розбито на два:
  - `test_polymarket_all_prices_no_partial_warning`: на звичайній фікстурі, де всі ціни є, попередження немає в обох інструментах;
  - `test_polymarket_missing_price_warns`, три випадки: нечислова ціна `"abc"`, JSON `null` (раніше не перевірявся) і список цін, коротший за список результатів. У кожному рівно один результат має `price` і `price_devigged` `None`, решта дає 0.5 / 0.5, а попередження є в обох інструментах (у `polymarket_search` разом з id `777`).
- Перевірка: `make lint`, `uv run pytest -q tests/test_server.py -k polymarket` (8 passed).
