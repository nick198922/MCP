# 2026-11-06 · Попередження pytest у наборі

Пункт 135 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md). Змінено лише `pyproject.toml` (один рядок конфігурації pytest). `src/`, `scripts/`, `execution/`, колектори й тести не чіпали, асерти ті самі.

- **Як міряли.** Увесь набір, разом із `slow`, двічі прогнали командою `uv run pytest -q -n auto -W default -p no:cacheprovider` з `PYTHONWARNINGS=default`: до і після правки. stdout і stderr писав тимчасовий скрипт `scripts/_warn_run.py` (потім видалений) у `~/.cache/tradedesk-agents/tmp/`. Результат обох прогонів: 5168 пройшло, 3 пропущено, 0 упало.
- **Що знайшли.** Причин лише дві, обидві з нашої конфігурації або нашого тесту. Попереджень від залежностей (MCP SDK, ccxt, httpx) у тестах немає. Немає й `ResourceWarning` / `PytestUnraisableExceptionWarning` (незакриті клієнти, сокети, SQLite) та `RuntimeWarning` «coroutine was never awaited».

  | Категорія | Джерело | До | Після |
  |---|---|---|---|
  | `PytestDeprecationWarning` «`asyncio_default_fixture_loop_scope` is unset» (pytest-asyncio 1.4.0) | наша конфігурація: `[tool.pytest.ini_options]` у `pyproject.toml` | 7 (по одному на процес pytest: контролер + воркери xdist; ішло в stderr, тож у «warnings summary» його не було) | 0 |
  | `DeprecationWarning` «process is multi-threaded, use of fork() may lead to deadlocks» | наш тест `test_strategy_sieve.py::test_s3_known_result_in_sample` (`slow`) → `scripts/sieve.py` `Pool(4)` усередині воркера xdist | 1 | 1 (не виправлялось, див. нижче) |
  | Залежності | — | 0 | 0 |

- **Виправлено.** Додано `asyncio_default_fixture_loop_scope = "function"`. Поведінка не змінилась: усі 9 асинхронних фікстур у `tests/` мають область `function` (`scope=` є лише у двох синхронних фікстурах `test_report_sql_schema.py`), а саме таку петлю pytest-asyncio давав їм і без налаштування. Майбутні версії pytest-asyncio змінять типове значення, тепер воно зафіксоване.
- **Не виправлено: `fork()` у багатопотоковому процесі.** Попередження з'являється лише за трьох умов разом: тест `slow` запущено, є локальний кеш 1h (на сервері є, у CI нема, там тест пропускається через `needs_cache`) і набір іде під xdist, бо воркер execnet має потоки. `make test` (`-m "not slow"`) і `make test-slow` (без `-n`) його не показують. Сам скрипт `sieve.py` з командного рядка однопотоковий, тож попередження стосується лише тесту. Прибрати його можна двома способами. Перший: `multiprocessing.get_context("forkserver")` у `scripts/sieve.py`, але це зміна скрипта продукту, і `run_symbol` модуля, завантаженого тестом через `_runner()`, тоді мав би імпортуватися в дочірньому процесі за назвою. Другий: `workers=1` у тесті, але це ≈ 4× довше за нинішні ~146 с. Обидва варіанти за межами пункту, тому відкладено в BACKLOG 6.27.
- **Підсумок для `make test`:** попереджень до правки було 7 (у stderr), після правки 0.
