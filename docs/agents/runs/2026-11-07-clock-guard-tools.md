**Крок:** p — постійна картка `standing-improvements` (власник, Порядок 1); єдиний відкритий пункт 8a чекає даних сервера, тож додано пункт 219 зі списку «Як поповнювати».

**Результат:**
• Охоронець годинника в `tests/test_architecture_imports.py` тепер діє й на `tools/`, `providers/` (раніше лише `analytics/`): `time.time()`, `datetime.now()`, `utcnow()`, включно з псевдонімами імпортів.
• Порушень у коді 0, `src/` не змінено; `time.monotonic` / `perf_counter` дозволені.
• Не ловить `date.today()`, `time_ns()` (їх у коді немає; зазначено у змінах).
• `make lint` ок, `make test`: 6969 passed, 3 skipped.

**Критик:** approve (sonnet, режим C), мутація тесту ловиться — [рев'ю](../../reviews/2026-11-07-clock-guard-tools.md).

**Далі:** owner. Відкритих питань нових немає; на картці лишився лише 8a (сервер).
