# Рев'ю: описи параметрів, третя група (пункт 28 постійної картки)

Вердикт: **approve**. Блокерів немає.

Перевірено особисто:

1. `make lint` чистий. `uv run pytest -q tests/test_tool_metadata.py tests/test_server.py` дає 72 passed.
2. Поведінка не змінилась. Я порівняв кожен `Field` у `git diff`: змінився лише `description`, а `ge/gt/le/lt/min_length/max_length` і значення за замовчуванням ті самі. Спільні типи в `history.py` збігаються з вихідними межами в усіх трьох місцях, де їх використано (`range_detect`, `range_setup`, `range_setup_scan`):
   - `LOOKBACK_DAYS` 1–365;
   - `MIN_POINTS` 2–10000;
   - `RANGE_LOOKBACK` 20–10000;
   - `RANGE_MIN_BARS` 5–1000;
   - `RANGE_MAX_WIDTH` (0, 50];
   - `SWING_LOOKBACK` 1–50;
   - `SWEEP_WICK_RATIO` 0–1;
   - `STOP_MULT` 0–10;
   - `STOP_MIN_OFFSET` 0–5;
   - `MIN_RR` 1–20.
   `iv_rank` лишив вбудований `min_points` без змін.
3. Точність описів звірена з кодом:
   - `journal_close`: `cancelled` ігнорує `exit` (журнал, рядки 113–126).
   - `liquidations_summary`: `top_n * 2` для рядків по символах (рядок 107).
   - `events_add`: `label` входить у ключ дедуплікації `(ts, kind, label)`.
   - Одиниці: IV у десятковому вигляді (`0.60 = 60 %`), `T = days/365`, `r = 0`, відсотки названі явно.
   - Опис `power` у `odds_devig` узгоджений зі змістом методу.
   - Англійська скрізь, нічого з `execution/` не змінено.
4. `PARAM_DESCRIPTION_ALLOWLIST` порожній, доданий тест `test_param_description_allowlist_stays_empty`. Тест `test_every_parameter_has_a_description` тепер охоплює всі інструменти, тому будь-який новий параметр без опису його провалить.

Зауваження (не блокери):

- Низька важливість: у `touch_probability` опис `barrier` каже «direction is inferred». Це правда для відомої логіки інструмента, але я окремого тесту на це не бачив. Варто лише тримати опис синхронним, якщо логіку зміниться.
- Низька важливість: у `RANGE_LOOKBACK` фраза «venues cap a request, often at 1000-1500 bars» є загальним фактом про біржі, а не гарантією. Для опису цього достатньо.
