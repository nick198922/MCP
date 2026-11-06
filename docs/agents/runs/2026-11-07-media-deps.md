**Крок:** e/p — постійна картка `standing-improvements`: відкритих пунктів < 3, додано пункт 189 (BACKLOG 6.38, залежності медіа-скриптів).

**Результат:**
• Імпорти `bs4`, `faster_whisper`, `imageio_ffmpeg`, `PIL`, `youtube_transcript_api` у 6 медіа-скриптах обгорнуто: без пакета — зрозуміле повідомлення з командою `uv run --no-project --with …` замість `ImportError`.
• `deps_check.py` розпізнає охорону `find_spec`; неоголошених безумовних імпортів у `scripts/` 0 (було 6), у `src/` 0.
• Extra `media` не додано: `uv lock` заборонено, `pyproject.toml` і `uv.lock` без змін (за потреби власник запускає `uv lock`).
• Я доправив `tests/test_scripts_import.py`: тест імпорту не знав про новий `SystemExit` у `lesson_sheets`.
• `make lint` і `make test` пройшли: 6751 passed, 3 skipped.

**Критик:** C (sonnet) — approve, блокерів немає; 3 зауваження низької серйозності ([рев'ю](../../reviews/2026-11-07-media-deps.md)).

**Далі:** `owner`. Пункт 8a картки чекає даних сервера. Нових питань немає.
