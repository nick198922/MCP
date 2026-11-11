# 2026-11-11 · Тести: спільний шаблон мігрованого сховища ще для трьох файлів (пункт 322)

Пункт 322 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md), продовження [пункту 321](2026-11-11-demo-trader-db-template.md). Змінено лише `tests/`; `src/`, `execution/`, `collector/` не чіпав.

- **Замір.** 42 файли, що самі мігрують свіже сховище (без `test_demo_trader.py`, `test_store.py`, `test_pm_gap.py`), прогнано пачками в один процес (`-n 0`). Для 40 з них час setup підсумовано по файлах (плагін-підсумовувач поза репозиторієм). `test_forward_scripts.py` і `test_carry_paper_engine.py` переглянуто лише через `--durations=40`: у них повільний call, а не setup. Найбільший setup: `test_tool_failure_extra_13.py` 24.6 с, `test_tool_failure_extra_11.py` 15.8 с, `test_journal_close_tg.py` 8.4 с. Далі `test_rules_pretrade_tg.py` 7.6 с, `test_journal_trade_tg.py` 6.2 с і `test_tool_failure_extra_8.py` 6.2 с; ці файли не чіпав. У більшості інших файлів сховище мігрує всередині тесту, а не у фікстурі, тому їхній setup менший за 1 с.
- **Зміна.** У `tests/conftest.py` дві фікстури. `migrated_store_template` (scope session) мігрує файл-шаблон раз на процес, у кожного воркера xdist свій. `migrated_db` копіює цей шаблон у `tmp_path/tradedesk.sqlite` для кожного тесту. Шаблон закривається через `aclose()`, тож WAL уже злитий у головний файл. Фікстура `journal_db` у `test_journal_close_tg.py` тепер просто повертає копію. `rt` у `test_tool_failure_extra_11.py` / `_13.py` тепер працює з копією шаблону, а не з `":memory:"`: базу в пам'яті скопіювати неможливо, а міграція такої бази коштує ~0.2 с. `Store.init()` і далі викликається в кожному тесті. Жоден асерт не змінено. Міграції в цих файлах не перевіряються.
- **Замір файлів** (кожен файл окремо, один процес, до → після; сервер ділили інші процеси):

  | файл | тестів | setup, с | увесь файл, с |
  |---|---|---|---|
  | `test_tool_failure_extra_13.py` | 120 | 27.3 → 2.1 | 45.0 → 19.5 |
  | `test_tool_failure_extra_11.py` | 86 | 19.1 → 1.8 | 28.9 → 13.5 |
  | `test_journal_close_tg.py` | 25 | 8.3 → 0.65 | 11.1 → 2.3 |

  Час самих тестів (call) з файлом замість `:memory:` не зріс понад шум: 16.6 → 16.2 с і 9.0 → 10.1 с. Три файли разом з `-n 4`: 231 passed. `make lint` чисто.
- **Не зроблено.** `test_demo_trader.py` має власну фікстуру-шаблон із пункту 321. На спільну її не переводив, щоб не розширювати зміну. Наступні кандидати для `migrated_db`: `test_rules_pretrade_tg.py`, `test_journal_trade_tg.py`, `test_tool_failure_extra_8.py`, `test_journal_cancel_tg.py`.
