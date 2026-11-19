# Рев'ю коду: компактні відповіді cycle_regime і polymarket_search (пункт 405)

Режим C, незакомічені зміни робочого дерева. Вердикт: **approve** (блокерів немає).

## Що перевірено сам
- `make lint`: чисто.
- `pytest tests/test_compact_defaults.py tests/test_cycle_regime.py tests/test_assistant_snapshot.py tests/test_server.py`: 139 passed.
- Grep усіх викликачів `cycle_regime` / `polymarket_search` (src, scripts, tests, prompts, docs/knowledge).
- Фікстура: ключів/секретів немає. Лише публічні дані Gamma (id, slug, question, ціни, `clobTokenIds`); `_source` вказує дату й джерело.
- `history_summary`: прочитав код і тест. Підрахунок станів, змін по визначених днях і поточної серії правильний. Усі однакові стани дають run = len, `current_run_since` = перший день. Порожня історія дає None. Дні без стану йдуть як `undecided` і в `changes` не рахуються (це описано в docstring і в тесті).

## Інваріанти
1. Інструмент не кидає виняток: `out.pop("history")` безпечний, бо `analytics.cycle_regime` завжди повертає `history` (рядок 514). Збій провайдера покрито тестами `*_compact_still_reports_a_provider_failure` для обох інструментів.
2. Описи англійською, нові параметри `include_details` мають `Annotated[..., Field(description=...)]`. Одиниці не змінено. Застереження «context, not a signal» і «NOT backtested» лишились у компактній відповіді (тест це перевіряє).
3. Мережі поза провайдерами немає, аналітика чиста (`history_summary` без I/O, є юніт-тест).
4. Відповідь не обрізається мовчки: у `warnings` є рядок «compact answer: …; include_details=true for all», у `polymarket_search` є поля `count` і `shown`.

## Споживачі старого повного виводу
- `collector/assistant_snapshot.py`: переведено на `include_details: True`, бо бриф рахує зміни за всі 30 днів. Це правильно. Тест snapshot проходить.
- `collector/cycle_regime_log.py`, `cycle_regime_eval`, `verify_*` викликають `analytics.cr.cycle_regime` напряму, а не MCP-інструмент, тому їх зміна не зачіпає.
- `scripts/live_smoke.py` викликає `polymarket_search` і перевіряє лише конверт. Не ламається.
- `src/tradedesk/prompts/pre_trade_checklist.md:46` використовує `polymarket_search` для ціни. Потрібні лише ціни, які в компактній відповіді є. `token_id` для `polymarket_history` береться через `polymarket_market` або `include_details=true`, і це написано в описі.
- Тестові виклики оновлено: `test_server.py::test_polymarket_search` і `test_cycle_regime.py` використовують `include_details=True`.

## Зауваження (не блокують)
1. Низька: рядок `cycle_regime` у `README.md` (62) не згадує `include_details` і компактне значення за замовчуванням. `docs/PARITY.md` оновлено. Fix: одне речення в README.
2. Низька: `count` у `polymarket_search` лишився кількістю знайдених ринків, а `markets` містить до 10. Це задокументовано (`shown`), але споживач, що вважає `len(markets) == count`, помилиться. Споживачів у репозиторії немає.
3. Інформаційна: в компактному `polymarket_search` без `slug` ринок не можна відкрити за посиланням. Є `id` для `polymarket_market`, тож функціональної втрати немає.
