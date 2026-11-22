# Рев'ю (режим C): стислі відповіді за замовчуванням, `market_flow` і `hyperliquid_funding_history`

Дата: 2026-11-18. Об'єкт: незакомічені зміни в клоні (пункт 379 картки `IDEA-20261102-standing-improvements`).

**Вердикт: approve.** Блокерів немає, три зауваження без блокування.

## Що перевірено самостійно
- `make lint`: чисто.
- Тести: `test_compact_defaults`, `test_market_flow`, `test_review_leftovers_nonfinite`, `test_rows_of_dropped`, `test_hl_coin_case`, `test_server`, `test_review_leftovers_2`, `test_dashboard_flows`, `test_assistant_snapshot`, `test_assistant_brief_tg`: 261 passed, 1 skipped. Додатково `-k "envelope or outage or wrong_type or parity or docs or readme or schema or description"`: 365 passed, 2 skipped. Повного набору не запускав (це робить координатор).
- Інваріанти CLAUDE.md: опис обох інструментів англійською, з одиницями (`rate per hour as a decimal`, `ts_ms`) і з попередженням про розмір; параметр `include_series` у `Annotated[bool, Field(description=...)]`; READ_ONLY лишився; нових шляхів, що кидають виняток, немає (змінено лише збирання `data`). Новий параметр не додає інструмент, тож `EXPECTED_TOOLS` і README-таблиця не потребують правок за чеклістом.
- Споживачі (Grep по `src/`, `scripts/`, `tests/`, `.claude/`): `collector/assistant_snapshot.py` передає `include_series` явно (`flows` = False, `flow_btc` / `flow_eth` = True), тож панель і бриф не змінилися; `tests/test_dashboard_flows.py` теж передає `True`. Скрипти поле `entries` не читають (колектор бере `funding_entries` напряму з провайдера). `test_rows_of_dropped` читає лише `summary` і `venues_latest`, вони є й у стислій відповіді. Втрачених потрібних полів не знайдено.
- Тест розміру не тавтологічний: порівнює справжній JSON-конверт (`json.dumps`) з порогом 8000; для `market_flow` перевіряє також, що повна відповідь більша за поріг, що довжина ряду дорівнює 7*24 і що зведення в обох режимах однакові; для funding перевіряє відсутність ключа `entries`, збіг `count` з довжиною `entries` і форму елемента.
- Слабшання у `test_review_leftovers_nonfinite.py` частково виправдане: відкидання нескінченного значення досі перевіряється через `stats.count == clean - 1`, а `_non_finite(env)` досі обходить усю відповідь. Дивись зауваження 1.

## Зауваження (не блокують)
1. **Середня.** У `test_hyperliquid_funding_history_drops_a_non_finite_rate` перевірка `len(entries) == count` зникла, і тепер жоден тест не доводить, що `entries` у відповіді `include_series=true` не містить NaN/inf. Фікс: окремий невеликий тест (або виклик через `_clean_and_broken` з аргументами, що включають `include_series=True`), який повторює мутацію й перевіряє `_non_finite(env) == []` та `len(entries) == count`. `ARGS` із `test_tools_envelope_shape` для цього міняти не треба.
2. **Низька.** Запас тесту розміру для `market_flow` малий: на фейках 7437 з 8000 (≈ 7 %); живцем 6946. Зростання зведень на кількасот символів зламає тест. Це прийнятно (цель тесту — зупиняти ріст), але розробнику варто знати: правити слід зведення, а не піднімати поріг.
3. **Низька.** Рядок `market_flow` у `README.md` не згадує, що ряд за параметром `include_series`. Опис інструмента це говорить, тож для моделей проблеми немає; для власника, що читає README, — доповнити одним реченням. Картка 379 лишилась `[ ]`: закриє координатор після рев'ю.

## Зміна поведінки, про яку варто пам'ятати
`include_series` у `market_flow` тепер `false` за замовчуванням (було `true`). Зовнішній клієнт, який викликав інструмент без аргументів і читав `data.series`, отримає відповідь без ряду. У репозиторії таких викликів немає (перевірено Grep), у `docs/changes/` це названо явно.
