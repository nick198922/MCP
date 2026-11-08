# Рев'ю: plan_add з expires_ts у мілісекундах (BACKLOG 6.18)

Вердикт: **approve**

Перевірено самостійно: `git diff` по `src/tradedesk/tools/trade_plan.py` і `tests/test_trade_plan.py`; `make lint` (чисто); `uv run pytest -q tests/test_trade_plan.py` (18 passed); `docs/changes/2026-11-05-plan-add-ms-expires.md`.

1. (ок) Інваріант 1: `_iso` ловить `ValueError`, `OverflowError`, `OSError` і повертає `str(ts)`. `plan_add` тепер дає конверт `ok=false` з `invalid_input` замість винятку.
2. (ок) Інваріант 5: новий інструмент не додається, тож вимоги до README, PARITY, EXPECTED_TOOLS не діють. Запис у `docs/changes/` є.
3. (ок) Тести змістовні й упали б на старому коді. Інтеграційний тест перевіряє мілісекунди, 1.8e12 і 10**30: причину `invalid_input`, число в тексті помилки, відсутність запису в сховищі, а також нормальний шлях. Юніт-тест `_iso` покриває `None`, секунди та від'ємне велике значення.
4. (низька, не блокує) Сире число повертається рядком, тому в `_view` для вже збереженого плану поле `expires` може бути нечитабельним. Для плану, створеного через `plan_add`, це недосяжно, бо валідація відхиляє такі значення.
5. Зміни поза межами завдання в перевірених файлах відсутні. У `git status` є зміни `docs/BACKLOG.md` та `docs/ideas/IDEA-20261102-standing-improvements.md`; їх я не перевіряв.
