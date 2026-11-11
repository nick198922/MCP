**Крок:** p, постійна картка, пункт 310 (залишок 6.53 після 309).

**Результат:**
• `deribit_dvol` відкидає зіпсовані рядки (NaN, inf, bool, поза діапазоном) з попередженням; усі зіпсовані → `ok=false`; не-список у `data` більше не кидає.
• `negrisk._jlist` ловить `ValueError` / `RecursionError`; `pm_threshold.outcome_yes` не читає `[true, false]` як виплату YES.
• 53 нових тести (`tests/test_tool_failure_extra_17.py`); `make lint` і `make test`: 9333 пройшло.
• Знайдено, не виправлено: `negrisk._f` кидає `OverflowError` (лише скрипт `verify_negrisk_step0`), записано в BACKLOG 6.53.

**Критик:** approve (`docs/reviews/2026-11-10-live-float-guard-5.md`); зауваження не блокують: застарілий опис «OHLC», тест 17 залежить від допоміжних функцій тесту 16.

**Далі:** `owner`.
