**Крок:** постійна картка (пункт 173): усі пункти карток власника заблоковані (дані сервера, час, власник), тому додано й виконано новий пункт про форму Envelope.

**Результат:**
• `tests/test_tools_envelope_shape.py`: 109 інструментів викликано, 85 з `ok=true` перевірено (поля Envelope, `evidence` з джерелом і часом, `missing`, warnings).
• 4 порушення: `perps_basis_compare`, `perps_funding_compare`, `perps_funding_history`, `perps_open_interest` — okx лише в `missing` без warning. Не виправлено, BACKLOG 6.47 (рішення: достатньо `missing` чи додати warning).
• 24 інструменти дали `ok=false` на фейках (немає фейку провайдера, ключ Coinalyze, порожня БД) і форму успіху не перевірено.
• `src/` не змінено. `make lint` і `make test`: 6237 passed, 3 skipped.

**Критик:** approve ([рев'ю](../../reviews/2026-11-06-envelope-shape.md)); виправлено лічильник 36 замість 35 у змінах.

**Далі:** `owner`. Відкрите: BACKLOG 6.47; Q-20261106-1 (поріг «48 год перерви») досі чекає відповіді.
