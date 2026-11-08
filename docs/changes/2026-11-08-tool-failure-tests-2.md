# 2026-11-08 — тести «збій сховища / зіпсовані рядки → відповідь, не виняток», частина 2 (пункт 279)

Інваріанти 1 і 5 `CLAUDE.md`, BACKLOG 6.53.

- Окремі тести для 5 інструментів сховища: `funding_zscore`, `skew_percentile`, `liquidations_summary`, `event_study`, `ohlcv_history` (`tests/test_tool_failure_extra_2.py`, 24 тести). Перевіряють збій запиту SELECT (`sqlite3.OperationalError` → `missing: store`, `ok=false`) і рядки, які інший записувач лишив із рядком, blob або inf у числовій колонці (SQLite таке зберігає).
- **Знайдено баги:** на такому рядку `funding_zscore`, `skew_percentile` і `liquidations_summary` кидали `TypeError` у сумах, а `event_study` кидав `ValueError` / `OverflowError` на часі події й `TypeError` на барі. Тепер такі рядки пропускаються з попередженням (`N stored rows ... skipped`); якщо в `liquidations_summary` зіпсовані всі рядки вікна, відповідь `ok=false`, а не «ліквідацій нема». inf у `funding_zscore` / `skew_percentile` раніше давав `ok=true` з NaN у статистиці.
- Новий помічник `store_num` у `tools/_util.py`: скінченне число зі сховища або `None`.
- `latest_ts` у `funding_zscore` / `skew_percentile` тепер береться з того самого рядка, що й останнє значення (раніше з останнього рядка, навіть якщо `rr25` там NULL).
- Лишається 30 інструментів без окремого тесту (BACKLOG 6.53). Відкрите: `iv_rank` кидає `TypeError` на рядку в `atm_iv` (перевірено 08.11, не виправлено), `ohlcv_history` віддає зіпсовану клітинку як є.
- Числа на коректних даних не змінились; `execution/` не чіпали.
