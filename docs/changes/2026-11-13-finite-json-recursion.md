# 2026-11-13 · `finite_json` не кидає `RecursionError` (пункт 375)

Пункт 375 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md), зауваження 4 [рев'ю пункту 353](../reviews/2026-11-13-tags-close-journal.md). Змінено `src/tradedesk/tools/_util.py` і додано `tests/test_finite_json.py`; `execution/` не чіпав.

- `finite_json` тепер ловить і `RecursionError`: занадто глибоко вкладене значення дає `False`, як NaN / Infinity, а не виняток назовні (інваріант 1). Через нього безпечні `drop_corrupt_rows` і `clean_tags` для будь-яких клітинок store.
- Новий тест: вкладений список і словник на 5000 і 100 000 рівнів (з NaN на дні) дають `False` без винятку; NaN, Infinity, blob дають `False`; звичайний JSON (зокрема рядки "inf" і "NaN") дає `True`.
- Межа глибини енкодера залежить від версії: 5000 рівнів падають на Python 3.11, але кодуються на 3.12.3 і 3.13.16; 100 000 рівнів падають на всіх трьох (перевірено 13.11). На старому коді на 3.12.3 обидва випадки на 100 000 рівнів падали з `RecursionError`.
- `uv run pytest -q tests/test_finite_json.py tests/test_tool_failure_extra_19.py tests/test_tool_failure_extra_20.py`: 87 passed; `make lint` чисто.
