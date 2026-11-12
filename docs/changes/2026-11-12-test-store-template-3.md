# 2026-11-12 · Тести: спільний шаблон мігрованого сховища ще для трьох файлів (пункт 332)

Пункт 332 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md), продовження [пункту 322](2026-11-11-test-store-template-2.md). Змінено лише `tests/`; `src/`, `execution/`, `collector/` не чіпав.

- **Зміна.** `tests/test_demo_trader.py` більше не має власної фікстури-шаблону `_migrated_db_template` з пункту 321 і копіювання через `shutil`. `world` тепер бере `migrated_db` з `tests/conftest.py`: файл бази тепер `tradedesk.sqlite`, а не `db.sqlite`, на це ім'я жоден тест не посилається. У `test_rules_pretrade_tg.py` і `test_journal_trade_tg.py` фікстура `journal_db` раніше мігрувала свіжу базу для кожного тесту, тепер вона повертає `migrated_db` (той самий шлях `tmp_path/tradedesk.sqlite`). Прибрано імпорти, що стали зайвими: `shutil` у `test_demo_trader.py`, `asyncio` і `Store` у `test_rules_pretrade_tg.py`. Жоден асерт не змінено. Кількість тестів та сама: 63, 31, 24.
- **Замір** (кожен файл окремо, один процес `-n 0`, setup підсумовано по тестах; сервер ділили інші процеси):

  | файл | тестів | setup, с | увесь файл, с |
  |---|---|---|---|
  | `test_demo_trader.py` | 63 | 1.37 → 1.08 | 7.6 → 8.2 / 7.9 |
  | `test_rules_pretrade_tg.py` | 31 | 7.58 → 0.30 | 11.6 → 1.4 |
  | `test_journal_trade_tg.py` | 24 | 5.18 → 0.35 | 6.2 → 1.5 |

  `test_demo_trader.py` уже мав шаблон, тож тут прибрано лише дубль коду. Різниця в загальному часі (два заміри «після») лежить у межах шуму.
- **Перевірка.** Чотири файли разом із `-n 4` (з `test_demo_option_leg.py`, який імпортує `FakeVenue` з `test_demo_trader.py`): 149 passed. `make lint` чисто.
- **Наступні кандидати** для `migrated_db` з заміру пункту 322: `test_tool_failure_extra_8.py` (setup 6.2 с), `test_journal_cancel_tg.py`.
