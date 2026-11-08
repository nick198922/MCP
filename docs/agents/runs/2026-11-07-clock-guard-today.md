**Крок:** p — постійна картка `standing-improvements` (усі інші картки власника заблоковані); додано пункт 224.

**Результат:**
• Правило `clock-call` у `tests/test_architecture_imports.py` ловить також `date.today()`, `datetime.date.today()`, `time.time_ns()` (з псевдонімами).
• Порушень у `src/` 0, виправлено 0; тестів 7 → 9.
• `make lint` і `make test` пройшли (6987 passed, 3 skipped).

**Критик:** C (sonnet) — approve; зауваження: `datetime.today()` ще не покрито (кандидат на наступний пункт), [рев'ю](../../reviews/2026-11-07-clock-guard-today.md).

**Далі:** owner. Нових питань нема.
