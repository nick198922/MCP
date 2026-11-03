# Рев'ю: scripts-dedup, дев'яте коло (пункт 40 standing-improvements)

Вердикт: **approve**. Блокерів немає.

## Що перевірено особисто

1. Тіла прибраних копій (за `git diff`) такі самі, як у `scripts/common_helpers.py`:
   - `ymd(day)` у 7 скриптах: `datetime.fromtimestamp(day * 86400, UTC).strftime("%Y-%m-%d")`.
   - `day_str` у `verify_course_s1_spot_alts`: те саме з `tz=UTC`.
   - `ymd_day` у `common_helpers.py` викликає `datetime.fromtimestamp(day * 86400, timezone.utc).strftime(...)`. `UTC` і `timezone.utc` — той самий об'єкт.
   - `rd` у `verify_free_supply_history` і `read_json` у `verify_tradfi_offhours_reopen`: `json.loads(p.read_text())`, як у спільному `read_json`.
2. У 10 змінених файлах не лишилося викликів `ymd(`, `day_str(`, `rd(` чи локального `read_json(`. Знайдені `fc.rd` і `s14.rd` — це атрибути інших модулів, які не чіпали.
3. Імена ззовні не зламані. Тести й `scripts/` посилаються на ці модулі лише так:
   - `tests/test_pair_spread.py` бере `log_returns`, `admitted`, `FORM_DAYS`;
   - тести `verify_free_supply_history` беруть `cmn`, `our_snapshots`;
   - `vlf`, `s1.day_of`, `s1.rd` / `s1.wr` лишились без змін.
   - Видалені `ymd`, `day_str`, `rd`, `read_json` ніде не імпортуються. `s1.day_str` не використовує ніхто.
4. Лічильники в `docs/changes/2026-11-03-scripts-dedup-9.md` збігаються з тим, що я порахував у файлах: 82 виклики `cmn.ymd_day` (8 скриптів), 22 виклики `cmn.read_json` (2 скрипти), разом 104. Файлів 10, видалених копій 10.
5. Вивід не змінюється: замінені лише виклики та визначення. Зміни форматування — перенос довгих рядків `print` / f-string, текст рядків той самий.
6. Заборонені зони не зачеплені. `git status` показує лише 10 `scripts/verify_*.py`, `docs/BACKLOG.md`, `docs/HANDOFF.md`, картку standing-improvements і новий файл у `docs/changes/`. Ні `execution/`, ні колекторів, ні `scripts/server/`, ні `agents_*`, ні заморожених форвардів.
7. `make lint`: ruff check і format чисті.
8. `uv run pytest -q tests/test_pair_spread.py tests/test_toptrader_divergence.py tests/test_common_helpers_callers.py tests/test_common_helpers.py`: 73 passed.

## Зауваження (не блокують)

1. Низька. Власних перевірок ідентичності на 116 зразках з опису я не повторював. Я звірив тіла за diff і `common_helpers.py`, а ці зразки лишаю на слові розробника. Тіла збігаються дослівно, тож ризик мінімальний.
2. Низька. `import common_helpers as cmn` у `verify_free_supply_history` уже був. Решта 9 скриптів додали імпорт у блок stdlib/third-party, ruff його прийняв. Запуск скриптів над реальними кешами я не робив: вони потребують великих даних.
3. Документи (BACKLOG 6.4, HANDOFF, картка) оновлені, перелік «лишилось» узгоджений із файлом змін.
