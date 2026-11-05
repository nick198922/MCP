# 2026-11-05 · Охоронець колонок `SELECT *` у звітах контрольної точки

Пункт 97 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md), BACKLOG 6.15 (продовження [охоронця схеми SQL](2026-11-05-report-sql-schema-guard.md)).

- У `tests/test_report_sql_schema.py` замість SKIP чотирьох `SELECT *` тепер справжня перевірка. Для кожної таблиці (`demo_trade`, `cp_position`, `pm_gap_outcome`, `option_leg_snapshot`) у `STAR_READERS` перелічено функції, що читають її рядки за ключем: `analytics/checkpoint.py`, `analytics/carry_v2.a_v2_shadow`, `analytics/pm_gap_report.py`, `analytics/condor_forward.py` і `load_carry` у `scripts/checkpoint_report.py`. Обхід AST збирає там кожен літеральний `row["col"]` / `row.get("col")`, а також `legs[k]["col"]`. Кожен такий ключ має бути колонкою таблиці в схемі з міграцій (`Store.init`, схема 38).
- Де ім'я змінної вживається й для інших словників (`p` у `carry_forward`, `r` у `checkpoint.condor_forward`), ключі збираються лише в циклах і генераторах над рядками таблиці. Якщо функцію перейменують або в ній не знайдеться жодного ключа, тест падає й просить оновити перелік.
- Знайдено 10 / 8 / 8 / 20 колонок (`demo_trade` / `cp_position` / `pm_gap_outcome` / `option_leg_snapshot`). Усі вони є в схемі, тож розбіжностей немає і звіти й аналітика не змінювались.
- SKIP із причиною лишився один: ключі всередині JSON `cp_position.state` / `trade` (`spot_e`, `fees`, `pnl_usd` тощо). Їх пише рушій carry, а не схема.
- Межа охоронця: місця читання перелічено вручну. Якщо рядки передадуть далі під іншим ім'ям, тест цього не побачить. Перевірено, що він ловить відсутню колонку: на таблиці без `ask_size` тест падає з посиланням на `condor_forward.py:241`.
