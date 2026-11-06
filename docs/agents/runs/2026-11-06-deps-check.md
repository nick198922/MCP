**Крок:** p/e — постійна картка `standing-improvements`, новий пункт 156 (звірка залежностей; відкритих лишався лише 8a, що чекає сервера).

**Результат:**
• `scripts/deps_check.py` + `tests/test_deps_check.py` (6 тестів): імпорти `src/` і `scripts/` проти `pyproject.toml`.
• `src/tradedesk`: 8 сторонніх модулів; неоголошеним був `uvicorn` (HTTP-режим, `server.py:86`) — додано `uvicorn>=0.54` у `pyproject.toml` і 2 рядки в `uv.lock` (вручну).
• `scripts/`: 7 неоголошених ледачих імпортів (медіа-скрипти) — списком у змінах, BACKLOG 6.38.
• `make lint` чисто, `make test`: 5301 passed, 3 skipped.

**Критик:** C (sonnet) — approve, [рев'ю](../../reviews/2026-11-06-deps-check.md). Попросив підтвердити ручну правку lock командою `uv lock --check`.

**Далі:** `owner`. Власнику: виконати `uv lock --check`; рішення про extra `media` для скриптів — за ним. Нових питань немає.
