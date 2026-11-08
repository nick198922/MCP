**Крок:** p — постійна картка standing-improvements (інші картки заблоковані часом/власником); додано пункт 135 «попередження pytest».

**Результат:**
• Попереджень від нашого коду лише два види; виправлено `asyncio_default_fixture_loop_scope` у `pyproject.toml` (7 → 0, поведінка тестів та сама).
• Лишилось одне `fork()`-попередження slow-тесту сита (не в `make test`) — BACKLOG 6.27.
• Залежності, незакриті клієнти, «never awaited» — 0.
• Мій прогін: `make lint` ✓, `make test` 5167 passed, 3 skipped.

**Критик:** C (sonnet) — approve, [рев'ю](../../reviews/2026-11-06-pytest-warnings.md).

**Далі:** `owner`. Питань немає.
