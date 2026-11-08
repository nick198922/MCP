**Крок:** p — постійна картка `IDEA-20261102-standing-improvements`; її єдиний відкритий пункт 8a заблоковано (потрібні дані сервера), тож додано пункт 231 (лишок пункту 224).

**Результат:**
• Правило `clock-call` у `tests/test_architecture_imports.py` ловить також `datetime.today()` і `datetime.datetime.today()` (з псевдонімами імпорту) у `tools/` і `providers/`.
• Порушень у `src/`: 0, `ALLOWED` без змін; +2 тести (11 passed).
• `make lint && make test`: 7030 passed, 3 skipped.

**Критик:** C (sonnet) — approve, [рев'ю](../../reviews/2026-11-07-clock-guard-datetoday.md).

**Далі:** `owner`. Питань нових немає; відкрите лише Q-20261106-1.
