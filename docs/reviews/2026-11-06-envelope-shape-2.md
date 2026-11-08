# Рев'ю пункту 176: ширше покриття тесту форми Envelope (режим C)

Вердикт: **approve**

Перевірено самостійно: `git diff` (тести, BACKLOG, картка), `make lint` (чисто), `uv run pytest -q tests/test_tools_envelope_shape.py` (4 passed).

## Перевірки

1. Лише тести й документи: змінено `tests/test_tools_envelope_shape.py`, `docs/BACKLOG.md`, картку, новий `docs/changes/2026-11-06-envelope-shape-2.md`. `src/` і `tests/conftest.py` не змінено.
2. Фейки без секретів: усі відповіді з `tests/fixtures/` або з існуючих тестових модулів (`test_pendle`, `test_onchain`, `test_contract_risk`, `test_cycle_regime`, `test_stablecoin_supply`). Ключів і токенів немає. Імпорт з інших тестових модулів не додає тестів до збору (імена не `test_*`).
3. Немає мережі: `_Transports.get` віддає 404-транспорт для будь-якого провайдера без фейку, `httpx.AsyncHTTPTransport.handle_async_request` підмінено на `AssertionError`. Нові фейки (`_pendle`, `_defillama`, `_dexscreener`) на невідомий шлях відповідають 404.
4. Не порожній тест:
   - `EXTRA_OK` (15 інструментів) перевіряється на неперетин з `not_ok`, тож повернення в `ok=false` ламає тест. Поріг `ok >= 95` (факт 100) дає запас 5.
   - `NO_NETWORK` порівнюється рівністю (`report["no_network"] == NO_NETWORK`). Шість БД-інструментів додано, і це чесно: вони працюють з локальним store, а лічильник `calls` рахує лише HTTP/ccxt. Якщо якийсь із них почне ходити в мережу, або `contract_risk`/`pendle_*` ні, рівність впаде.
   - Самоперевірка `test_the_check_catches_a_tool_without_evidence` лишилась і проходить на новому `_runtime` (без засіву).
   - `called == listed - len(SKIP)` зберігає честність лічильників (109/109).
5. Інваріанти CLAUDE.md: код інструментів не чіпали, нових інструментів і провайдерів немає, мережі в тесті немає, одиниць не торкались.

## Зауваження (низька важливість, не блокують)

1. Низька. Фейк GoPlus віддає `SHIB_BODY` на будь-який запит, а `stablecoins` синтетичний (мітка в картці про це є). Тест перевіряє форму відповіді, а не числа, тож це прийнятно. Тримати назви зразків у коментарі біля `_extra_fakes`, якщо хтось додасть тест на значення.
2. Низька. Документ змін каже «8 passed» для пари `envelope_shape` + `never_raise`; я прогнав лише перший файл (4 passed), за завданням.
3. Низька. У картці 176 галочка `[ ]` ще не змінена. Це за координатором після затвердження рев'ю.
4. Інформаційно. Лишились 9 інструментів з `ok=false` (Coinalyze ×4, `polymarket_options_gap`, `event_study`, `positions_risk`, `carry_paper_close`, `range_setup`). У BACKLOG 6.48 це названо, пункт правильно лишено відкритим.
