# Здоров'я колекторів: тест-охоронець джоб (05.11)

Пункт 96 [standing-improvements](../ideas/IDEA-20261102-standing-improvements.md). Джерело: «Пропуски даних» і технічний борг. Продовжує пункти 80 і 95: Polymarket мовчав три дні, бо `collector_health` не знав джоби.

- **Що додано.** Тест `tests/test_collector_health.py::test_every_collector_job_has_a_spec_or_an_exemption`. Він бере всі перемикачі `TRADEDESK_*_ENABLED` з `config.py`, будує справжній `build_default_scheduler` з усіма ними ввімкненими і по черзі вимикає кожен. Так видно, яка джоба від якого перемикача залежить. Тест валиться, якщо джоба не має `Spec` у `analytics/collector_health.SPECS`, причини в `NOT_CHECKED` чи в `EXEMPT` тесту. Ще він валиться, якщо `Spec.flag` не вмикає свою джобу, якщо `Spec` чи виключення вказує на джобу, якої вже немає, якщо перемикач не реєструє жодної джоби (крім `NOT_A_JOB`) або якщо котрась `PeriodicJob(name=…)` у `scheduler.py` недосяжна.
- **Прогалини.** З 25 джоб 19 мають `Spec`, 2 стоять у `NOT_CHECKED` (`assistant_paper`, `plan_resolve`), а 4 не мали нічого. Нових `Spec` немає, бо жодна з цих чотирьох не пише періодичного рядка в таблицю, який модель `Spec` могла б рахувати. Усі чотири внесено в `EXEMPT` з причиною:
  - `retention`: лише видаляє старі рядки;
  - `alerts`: пише лише на подію;
  - `assistant_snapshot`: пише JSON-файл, а не таблицю (застарілий файл показує панель);
  - `ohlcv_hist`: вимкнений, сталих серій немає.
  Подробиці — у BACKLOG 6.13 (в). `TRADEDESK_ALERTS_HEALTH_ENABLED` записано в `NOT_A_JOB`, бо це підперемикач джоби `alerts`.
- **Що перевірено.** Без `EXEMPT` тест валився саме на цих 4 джобах (`alerts`, `assistant_snapshot`, `ohlcv_hist`, `retention`). Прапорці всіх 19 `Spec` збігаються з перемикачами, що вмикають їхні джоби.
- **Перевірка:** `make lint`; `uv run pytest -q tests/test_collector_health.py tests/test_dashboard_health.py` (24 passed). Колектори, реєстрації, `analytics/collector_health.py`, форварди й `execution/` не змінювались.
