# 2026-11-14 · Попередження тестового набору (пункт 374b)

Пункт 374b [постійної картки](../ideas/IDEA-20261102-standing-improvements.md) (координатор поставив номер 374, але він уже зайнятий закритим пунктом у рядку 34; `tests/test_idea_cards.py::test_standing_card_item_numbers_are_unique` вимагає дати пізнішому пункту суфікс, тож він став 374b). Код у `src/`, `scripts/`, `tests/` не змінювався; `execution/`, правила рушіїв і мережу не чіпав.

- **Замір:** `uv run pytest -q -n 0 -W default -r w` по всьому набору разом із `slow` (Python 3.12.3, pytest 9.1.1, httpx 0.28.1, numpy 2.5.3): 10009 passed, 3 skipped, 1 failed, 1189 с. Падіння — саме дубль номера 374 у картці (див. вище), не попередження.
- **Попереджень до: 0. Після: 0.** У підсумку pytest немає ні рядка «N warnings», ні секції «warnings summary»; у виводі немає `ResourceWarning`, `PytestUnraisableExceptionWarning`, «Exception ignored», «unclosed».
- **З нашого коду (`src/`, `scripts/`, `tests/`):** жодного, виправляти нічого.
- **Із залежностей:** жодного, записувати нічого.
- **Чому тихо:** у `pyproject.toml` немає `filterwarnings`, тож `-W default` нічого не перекриває; єдиний свідомий фільтр pytest-asyncio знято ще раніше явним `asyncio_default_fixture_loop_scope`. Локальні придушення в коді (усі вузькі, у `catch_warnings`): `analytics/flow_model.py` і `scripts/verify_sector_pair_spread.py` (`RuntimeWarning` numpy на всі-NaN вікна), `scripts/sql_schema_check.py` (весь блок перевірки схеми); вони за пунктом не змінювались.
- **Межа заміру:** попередження в дочірніх процесах (тести, що запускають `scripts/` через `subprocess`) і `ResourceWarning`, що виникають лише під час завершення інтерпретатора, pytest не рахує; цей замір їх не бачить.
- `make lint` чисто; тестів не змінено, тож окремого прогону змінених тестів нема; `tests/test_idea_cards.py` після перенумерування проходить.
