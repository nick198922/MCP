**Крок:** e/p — постійна картка `IDEA-20261102-standing-improvements`, новий пункт 287 (тести збою інструментів, BACKLOG 6.53).

**Результат:**
• `tests/test_tool_failure_extra_5.py`: 66 тестів для `events_add`, `events_list`, `events_seed_load`, `journal_add`, `journal_close`, `journal_stats`.
• Знайдено й виправлено реальні дефекти (`tools/journal.py`, `tools/events.py`): `OverflowError` на великих `ts`, збереження inf/NaN, падіння на зіпсованих рядках сховища і на не-UTF-8 blob, крах `events_seed_load` на кривому форматі seed.
• Зміна поведінки: мілісекундні `opened_ts`/`closed_ts` у журналі відхиляються; порожній seed → `ok=false`.
• `make lint` чисто, `make test`: 7716 passed, 3 skipped (мій прогін).

**Критик:** C (sonnet) — approve, блокерів нема; `docs/reviews/2026-11-08-tool-failure-tests-5.md`. Дрібниці: `journal_list` на не-UTF-8 не перевірено, скасування зіпсованого запису не покрите тестом.

**Далі:** `owner` — готово до рев'ю. Питань до власника нема.
