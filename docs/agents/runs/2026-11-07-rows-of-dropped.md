**Крок:** p, постійна картка `standing-improvements` (усі інші пункти карток власника заблоковані часом, даними демо чи рішенням власника); додано пункт 213.

**Результат:**
• `_rows_of` у `cycle` і `flow` тепер попереджають про `dropped_rows` (число й поле) окремим рядком; «paging stopped early» лишається лише для `truncated` / неповноти без відкинутих рядків.
• Спільний хелпер `warn_incomplete_range` у `tools/_util.py`; числа не змінено, змінюється лише текст `warnings`.
• 21 новий тест (`tests/test_rows_of_dropped.py`); `make lint && make test`: 6945 passed, 3 skipped.

**Критик:** C (sonnet) — approve, блокерів нема; два дрібні зауваження (немає тесту `complete=true` + `dropped_rows>0`; float ігнорується) — [рев'ю](../../reviews/2026-11-07-rows-of-dropped.md).

**Далі:** `owner`. Нових питань нема. Живу перевірку власнику виконувати не потрібно.
