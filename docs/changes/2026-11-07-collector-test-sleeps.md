# 2026-11-07 · Тести колектора без реальних пауз опитування

Пункт 211 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md), залишок BACKLOG 6.26 (після [пункту 132](2026-11-06-test-speed-2.md)). Змінено лише `tests/`; `src/` і `execution/` не чіпано, асерти ті самі.

- **Що знайдено.** Тики планувальника в реальному часі тести не чекають: період 60 с ніколи не настає, бо `Scheduler.stop()` скасовує задачу, а тест з `period_seconds=0` крутиться без паузи. Реальні паузи — лише цикли опитування після `sched.start()`: `asyncio.sleep(0.05)` у `test_scheduler_runs_once_and_stops` і `test_scheduler_captures_failure`, `sleep(0.01)` у `test_scheduler_keeps_last_failure_after_a_success`, `sleep(0.02)` у `test_logfmt.py::test_scheduler_failure_passes_job_and_error`. Інших тестів, що запускають `Scheduler`, немає.
- **Що змінено.** Новий хелпер `wait_until(pred)` у `tests/conftest.py`: віддає керування циклу подій (`asyncio.sleep(0)`), доки умова не виконається, з межею 5 с (`asyncio.timeout`; раніше цикл після 50 спроб мовчки виходив, і падав лише наступний асерт). Чотири цикли опитування замінено на нього. Лінтер просить `asyncio.Event` (ASYNC110), але `Scheduler` події не має, а `src/` не змінюємо, тож на рядку `noqa: ASYNC110` з причиною.
- **До / після** (`pytest -q -n 0 --durations`, по два прогони; «до» — ті самі тести з HEAD в окремому файлі):

  | Тест | До | Після |
  |---|---|---|
  | `test_scheduler_runs_once_and_stops` | 0.13 с | 0.09–0.10 с |
  | `test_scheduler_captures_failure` | 0.13 с | 0.07–0.10 с |
  | `test_scheduler_keeps_last_failure_after_a_success` | 0.72–0.74 с | 0.69–0.73 с |
  | `test_logfmt::test_scheduler_failure_passes_job_and_error` | 0.02 с | < 0.005 с |

  Обидва файли разом: 2.19 → 2.06 с. Виграш малий (≈ 0.1 с), бо паузи були короткі; решту часу забирають `Store.init` (≈ 0.07–0.1 с на базу) і `build_server` + MCP-клієнт (≈ 0.7 с у тесті `system_health`), а це не очікування.
- **Лишилось:** `Store.init` на тест — окреме рішення (як і раніше). `tests/test_store.py` має навмисну `sleep(0.05)`: вона дає шанс запису, що має бути заблокований, і без реального часу перевірка втратила б сенс; не чіпано.
