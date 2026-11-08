# 2026-11-08 — реєстр інструментів звіряється в обидва боки (пункт 286)

Інваріант 5 `CLAUDE.md`, рев'ю `docs/reviews/2026-11-08-docs-tool-coverage.md` п. 3.

- `test_tool_surface` (`tests/test_server.py`) раніше перевіряв лише `EXPECTED_TOOLS <= names`: інструмент, зареєстрований на `build_server()`, але відсутній у `EXPECTED_TOOLS`, проходив непоміченим, а з ним і перевірка документації з пункту 283 (`tests/test_docs_tool_coverage.py` іде від `EXPECTED_TOOLS`).
- Тепер тест вимагає `names == EXPECTED_TOOLS`; у повідомленні падіння — списки «зареєстровано, але нема в `EXPECTED_TOOLS`» і «є в `EXPECTED_TOOLS`, але не зареєстровано».
- Розбіжностей не знайшлось: усі зареєстровані інструменти вже є в `EXPECTED_TOOLS`, тож README, `docs/PARITY.md` і `src/` не змінювались. Інваріант 5 тепер замкнений: новий інструмент без запису в `EXPECTED_TOOLS` валить цей тест, а з записом без документації — тест пункту 283.
