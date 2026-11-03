# Рев'ю (режим C): `source="store"` бере останні бари (пункт 29)

Дата: 2026-11-03. Об'єкт: незакомічений `git diff` (`store/db.py`, `tools/history.py`, три файли тестів, `docs/changes/2026-11-03-store-source-latest-bars.md`, BACKLOG, картка).

**Вердикт: approve.**

## Що перевірено самостійно
- `make lint`: чисто. `pytest tests/test_ohlcv_hist.py tests/test_ranges.py tests/test_regime.py`: 43 passed.
- Коректність `Store.ohlcv_range(newest=True)`: `ORDER BY ts DESC LIMIT N`, потім `reverse()` дає останні N у зростанні. Без `limit` і з `newest=True` порядок також зростаючий. Типове `newest=False` дає той самий SQL, що й раніше (`ORDER BY ts ASC`).
- Інші виклики `ohlcv_range` не зачеплені: `history.py:299` (`ohlcv_history`, без `newest`) і `events.py:218` (`since_ts`/`until_ts`, без `limit`). Інших споживачів у `src/` немає.
- Тести впали б на старому коді: старий код віддає бари 100-199 (перші 2N=200, останні 100 з них), а тести вимагають бари 900-999. Store-тест на старому коді дає `TypeError` (параметра `newest` немає). Тест `market_regime` на старому коді бачить частину фази падіння (`regime` не `up`, `first_ts` не збігається). Тест `range_*` через шпигуна на `classify_ranges` перевіряє саме `ts` переданих барів, а не лише число.
- Інваріанти CLAUDE.md: виняток назовні не йде (обгортки `try/except` на місці), мережі нема, `analytics/` не чіпали, `execution/` не чіпали. Описи параметрів англійською.

## Зауваження
1. (низька) `RANGE_LOOKBACK` спільний для `range_setup_scan`, який параметра `source` не має (лише live, `history.py:694`). Новий текст «store: the newest N rows of ohlcv_hist» там трохи вводить в оману. Виправлення: необов'язкове, або окремий опис для `scan`. Зміна `docs/changes` теж каже, що опис змінено і для `range_setup_scan`: це збігається з фактом, але для scan «store» не застосовується.
2. (інформаційна) `rows[-lookback_bars:]` після `limit=lookback_bars` тепер надлишковий, але нешкідливий.
3. (інформаційна) Дві дублюючі довгі рядки `ohlcv_range(...)` у `range_detect`/`range_setup` можна було б винести, але це стиль, вимог lint не порушує.

Блокуючих проблем немає.
