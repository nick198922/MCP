**Крок:** p — постійна картка `IDEA-20261102-standing-improvements`, пункт 198 (друга тема поспіль: зіпсовані поля в даних).

**Результат:**
• `funding_entries` іде через `parsed_rows`: зіпсований `fundingRate` більше не валить `carry_paper_open` / `carry_paper_status`; числа на нормальних даних не змінені.
• `market_ohlcv`: `ok=false` + `missing` замість `IndexError`, коли жодне закриття не число.
• `KNOWN` у `tests/test_tools_wrong_type_fields.py` порожній; ключі порушень звіряються за шаблоном; нові тести в `test_carry_paper.py`.
• `make lint` чисто, `make test`: 6839 passed, 3 skipped (мій запуск).

**Критик:** approve (sonnet, mode C), блокерів нема; [рев'ю](../../reviews/2026-11-07-funding-entries-wrong-type.md). Не блокує: `inf`/`nan` у `fundingRate` не відкидаються; мовчазне відкидання рядків без `complete=false`.

**Далі:** `owner`. Питань нових нема; відкрите лише Q-20261106-1.
