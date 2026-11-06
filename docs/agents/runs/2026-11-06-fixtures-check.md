**Крок:** p, постійна картка вдосконалень (відкритих пунктів лишалось менше трьох → додано 146 і 147).
**Результат:**
• Пункт 146: `scripts/fixtures_check.py` + `tests/test_fixtures_check.py` (6 тестів), лише читання.
• 119 фікстур, 115 з датою в імені, без жодної позначки було 3 → описані в новому `tests/fixtures/README.md`; лишок 0.
• Для `fear_greed_sample.json` з коміту не видно, реальний це знімок чи складений; `forexfactory_calendar_sample.json` зібрано вручну.
• `make lint` і `make test`: 5215 passed, 3 skipped.
**Критик:** C (sonnet) — approve, [рев'ю](../../reviews/2026-11-06-fixtures-check.md); дрібниця: тест запуску на репозиторії не перевіряє код виходу.
**Далі:** `owner`. Відкриті в картці: 147 (інвентар TODO/FIXME), 8a (чекає сервера). Незлиті гілки: `agent/2026-11-04-library-seller-exhaustion-card` (чекає власника).
