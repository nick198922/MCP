# Рев'ю: прискорення тестів (пункт 9 standing-improvements), 2026-11-02

Режим C. Перевірено робоче дерево (незакомічене), `docs/changes/2026-11-02-test-speed.md`.

## Вердикт: approve

Блокерів немає.

## Що перевірив сам

1. **Нічого з src/, execution/, scripts/ не змінено**: `git diff --stat -- src execution scripts` порожній. Змінені лише `tests/` і docs.
2. **Видалених тестів і послаблених assert немає.** Усі видалені (`-`) рядки у diff: перепис `Settings(...)`/`Runtime(...)` (додано `http_retry_backoff=0.0`, форматування) та три рядки в `test_setup_signals.py`, перенесені на кеш. У `test_short_s14_comment_names_the_impulse_low` умова і assert ті самі (`i >= 150`, `side == "short"`, обидва `in`/`not in`). Видалених `def test_` немає.
3. **Стаби в `conftest.make_transports`** (`hyperliquid:stats`, `polymarket:data` -> 404 `not mocked`): ними користуються лише колекторний traders-job у рантаймах з `collector_enabled`. `tests/test_traders.py` ставить власні транспорти й додатково власний `TokenBucket`; `test_copy_vaults`, `test_trader_persistence`, `test_pm_threshold` проходять (94 passed). Раніше ці тести ходили в справжню мережу, тож стаб лише прибирає недетермінізм і не маскує перевірену поведінку.
4. **`PRAGMA synchronous=OFF`** додано тільки в тестах на тимчасових файлах міграцій (`tmp_path`), не в `src/`. **`TokenBucket(1000, 1000)`** підставлено на `http._limiter` конкретного провайдера тесту, `monkeypatch` для `graph_mod.RATE_PER_SECOND/BURST` відкочується автоматично. Тести, що перевіряють сам rate-limit/backoff (`test_core.py`), не чіпали.
5. **Кеші між тестами**: `functools.cache` у `test_setup_signals._live_s14` ключується seed, повертає кортежі; споживачі лише читають (множини/списки будуються заново). Це чисті функції від `_bars(700, seed)`, тож стан між тестами не тече. У `test_pm_gap._call` сервер кешується на конкретному `rt` (`rt.__dict__["_test_server"]`), `rt` створюється на тест; як і в проді, lifespan викликається на кожне з'єднання.
6. `make lint` проходить. Запущено `test_setup_signals`, `test_pm_gap`, `test_traders`, `test_collector` (107 passed), ще 5 файлів (94 passed).

## Зауваження (не блокують)

1. (низька) `rt._test_server` з `# type: ignore` у `test_pm_gap.py` — дрібний хак; зручніше було б тримати словник у фікстурі. Не змінює покриття.
2. (низька) `http_retry_backoff=0.0` розмазано копіпастою по ~50 файлах; зручніше один фабричний хелпер у conftest, щоб нові тести не повертали повільний backoff. Пропозиція на майбутнє.
3. Заявлене 308 -> 216 с і 4115 passed повний прогін не перевіряв (координатор запускає повний suite).
