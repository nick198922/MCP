**Крок:** p, постійна картка `standing-improvements`: усі картки з пріоритетом власника заблоковані (дані сервера, час), тож додано й виконано пункт 309 (залишок BACKLOG 6.53).

**Результат:**
• `_json_list` / `_maybe_json_list` тепер ловлять `ValueError` (ціле 4300+ цифр) і `RecursionError`; `polymarket._f` не кидає на 300+ цифрах і не пускає NaN / inf.
• `deribit_vrp`: рядки DVOL з NaN / inf / bool / сміттям відкидаються з попередженням, усі зіпсовані → `ok=false`.
• `true` / `false` у числових полях семи парсерів тепер сміття, а не 1 / 0.
• 110 тестів `tests/test_tool_failure_extra_16.py`; `make lint` і `make test` (9279 passed, 3 skipped) пройшли.

**Критик:** C (Sonnet), approve, блокерів нема ([рев'ю](../../reviews/2026-11-10-live-float-guard-4.md)).

**Далі:** `owner`. Додано пункт 310 (`deribit_dvol` з NaN, `negrisk._jlist`, `outcome_yes` з bool). Заблоковані пункти картки: 8a та дірки ліквідацій (потрібні журнал і БД сервера). Нових питань немає.
