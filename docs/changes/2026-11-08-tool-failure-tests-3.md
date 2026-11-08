# 2026-11-08 — тести «збій сховища / зіпсовані рядки → відповідь, не виняток», частина 3 (пункт 280)

Інваріанти 1 і 5 `CLAUDE.md`, BACKLOG 6.53.

- Закрито два залишки пункту 279: `iv_rank` кидав `TypeError` на рядку чи blob в `atm_iv` (inf давав `max_iv` Infinity). Тепер такий рядок пропускається з попередженням, `latest.ts` береться з того самого рядка, що й значення. `ohlcv_history` віддавав зіпсовану клітинку як є: тепер бар пропускається з попередженням, а якщо зіпсовані всі, відповідь `ok=false`.
- Окремі тести (`tests/test_tool_failure_extra_3.py`, 69 тестів): `alerts_recent`, `journal_stats`, `positions_risk`, `positions_list`, `plan_list`, а також `iv_rank` і `ohlcv_history`. Перевіряють збій SELECT і рядки з рядком, blob (валідний і невалідний UTF-8) або inf у числовій колонці. `touch_probability` замінено на `positions_list`, бо тести вироджених входів у нього вже є.
- **Знайдено баги:** `plan_list` кидав виняток на рядку в рівнях, часі чи `r_result` і на entry = stop або 0. `journal_stats` кидав `ValueError` на рядку в `pnl` / `r_multiple`. Blob з невалідним UTF-8 ламав JSON-відповідь `alerts_recent` і `ohlcv_history`. inf давав Infinity / NaN у `journal_stats` і `positions_risk`. Тепер такі рядки пропускаються з попередженням (`N stored … rows skipped`, з id). Якщо зіпсовані всі, відповідь `ok=false`, а не «нічого нема».
- `positions_risk`: inf / NaN у `days`, `reference_spot`, `scenarios` тепер дають `invalid_input`, а переповнення результату дає `ok=false`. Раніше зіпсований грек мовчки рахувався як 0.
- Новий помічник `drop_corrupt_rows` у `tools/_util.py`.
- Лишається 25 інструментів без окремого тесту. Відкрите: `plan_review` кидає `ValueError` на зіпсованому відкритому плані (перевірено 08.11, не виправлено). Невалідний UTF-8 в інших інструментах-списках не перевіряли.
- На коректних даних відповіді не змінились; `execution/` не чіпали.
