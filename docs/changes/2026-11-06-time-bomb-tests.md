# 2026-11-06 · Тести, що залежать від справжньої дати

Пункт 134 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md). Змінено лише `tests/test_stuck_demo.py`. `src/`, `scripts/`, `execution/` і колектори не чіпали, асерти не послаблено.

- **Як шукали.** Grep у `tests/` (`datetime.now`, `time.time()`, `date.today`, `utcnow`, експірації `27NOV26` / `25DEC26` / …, `Runtime(` без `rt.now`) дав сотні збігів. Майже всі вони мають фіксований `NOW` або порівнюють відносний час. Тому вирішальною була емпірична перевірка: весь набір (`-m "not slow"`, xdist) проганяли з годинником, зсунутим на іншу дату. Тимчасовий плагін pytest лежав поза репозиторієм (`~/.cache/tradedesk-agents/tmp/timeshift/`) і запускався з `uv run --with time-machine`, тож залежності проєкту не змінено. `time_machine.travel(..., tick=True)` на рівні C підміняє `datetime.now` / `utcnow` / `today`, `time.time` / `time_ns`, `gmtime` / `localtime` / `strftime`, зокрема у воркерах xdist (перевірено окремим тестом-зондом). `time.monotonic` і asyncio лишаються справжніми.
- **Прогони** (5167 тестів, 3 пропущено):

  | Годинник | Упало | Нові відносно базового |
  |---|---|---|
  | справжній (06.11) | 0 | — |
  | 2026-11-27 09:00 UTC (після місячної експірації) | 2 | 2 артефакти (див. нижче) |
  | 2026-11-30 12:00 UTC | 3 | `test_stuck_demo` + 2 артефакти |
  | 2026-12-31 12:00 UTC | 3 | `test_stuck_demo` + 2 артефакти |
  | 2027-03-01 12:00 UTC (після виправлення) | 2 | 2 артефакти |

- **Знайдено й виправлено:**

  | Тест | Від чого залежав | Як виправлено |
  |---|---|---|
  | `test_stuck_demo.py::test_script_names_stuck_trades_read_only` | `scripts/collector_health.py::main` бере `int(time.time())`, а рядки БД мають вік відносно `NOW` = 06.11 06:00 UTC. Угода `NEW` (1 доба на `NOW`) стає «завислою» після 7 діб утримання + 1 доби запасу. З **13.11.2026 06:00 UTC** завислих стає 3, а не 2, і асерт `"✗ Завислі демо-угоди: 2 ("` падає. Межу перевірено: о 05:30 тест проходить, о 06:30 падає. | Перед `script.main` годинник скрипта підміняється на `NOW` (`monkeypatch.setattr(script, "time", SimpleNamespace(time=lambda: float(NOW)))`), як у виправленні `test_my_options`. Асерт той самий. Тепер тест проходить і на справжньому годиннику, і з годинником 13.11 06:30 та 01.03.2027. |

- **Артефакти підміни, не «бомби»** (не виправлялись). Обидва тести самі пишуть файл і тут же порівнюють його `st_mtime` з `time.time()`. Підміна зсуває `time.time`, але не час файлової системи, тому файл здається старим. На справжньому годиннику обидва числа збігаються за будь-якої дати.
  - `test_dashboard.py::test_read_usage_prefers_the_agents_cache_and_throttles_own_calls`: `scripts/dashboard.py::read_usage`, `age = now - USAGE_CACHE.stat().st_mtime`.
  - `test_agents_transcript.py::test_denied_report_counts_across_runs_and_subagents`: `scripts/agents_transcript.py::denied_report`, `path.stat().st_mtime >= time.time() - hours * 3600`.
- **Межі перевірки.** Не зсувались дочірні процеси (`subprocess.run` у `test_forward_scripts`, `test_pr_watch` тощо). Вони перевіряють коди виходу й повідомлення, а не дати. Не зсувались і час файлів та SQLite: `'now'` / `CURRENT_TIMESTAMP` у SQL в `src/` і `scripts/` немає. Тест `slow` (`test_s3_known_result_in_sample`) не проганявся. Дат перевірено п'ять, тож «бомба», що падає лише у вузькому вікні між ними, могла б лишитися непоміченою. Ідеться про інші тести, ніж `test_stuck_demo`: для нього межу 13.11 перевірено окремо.
- **Залежність у коді продукту** (не змінювалась):
  - `scripts/collector_health.py::main` не має параметра часу (`now = int(time.time())`, рядок 201), на відміну від `brief_line(now=…)`. Тест мусить підміняти модуль `time` скрипта. Якщо колись знадобиться відтворюваний звіт на певну дату, варто додати `--now`.
