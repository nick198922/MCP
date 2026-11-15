# Рев'ю коду: злиття `deribit_atm_term` у `deribit_skew_term(view="atm")`

Дата: 2026-11-15. Режим C. Об'єкт: незакомічена робоча копія (картка `docs/ideas/IDEA-20261114-mcp-cleanup.md`, пункт 2; `docs/changes/2026-11-15-mcp-cleanup-atm-term.md`).

## Вердикт: approve

Блокерів немає.

## Що перевірено самостійно
- `make lint`: чисто.
- `uv run pytest -q -n auto tests/test_server.py tests/test_provider_failures.py tests/test_option_ideas.py`: 170 passed.
- Тіло `_atm_term` порівняно з `deribit_atm_term` на `origin/main`: логіка, ключі рядка (`expiry, days, forward, atm_iv, strikes`), `shape`, запис у `missing` (`atm_term`) і значення за замовчуванням (`min_days` 0.5, `max_days` 400) збігаються рядок у рядок. Числа для `view="atm"` не змінені. Тест додатково фіксує еталонні значення старого інструмента на тих самих фікстурах.
- Режим за замовчуванням (`view="skew"`) не змінився: гілка `if view == "atm"` стоїть після `_chain` і перед старим кодом. Тест перевіряє, що в `skew` є `rr_shape` і немає `shape`.
- Інваріант 1: збій провайдера дає `_chain -> None -> b.build(None, ok=False)` для обох видів. Порожнє вікно дає `ok=false` із записом у `missing`. Обидва випадки є в тестах (`test_provider_failures`, `..._empty_window`).
- Інваріант 5: опис англійською, з одиницями (десяткові дроби, USD, дні) і примітками про `target_delta`; `Literal["skew","atm"]` з `Field(description=...)`; `READ_ONLY` лишився; `EXPECTED_TOOLS` оновлено (прибрано `deribit_atm_term`); README, PARITY, CHANGELOG-нотатка в `docs/changes/` є.
- Усі виклики оновлено: `git grep deribit_atm_term` не лишає жодного живого виклику. Залишки: історичні записи (CHANGELOG, `docs/changes/` за минулі дні), згадки «колишній ...» в README, PARITY, playbook, коментарях і docstring, питання Q-20261114-1. `scripts/option_ideas_tg.py`, `collector/assistant_snapshot.py`, `scripts/live_smoke.py` і три промпти переведено на `deribit_skew_term` з `view="atm"`. Тест `test_market_for_reads_the_mcp_tools` перевіряє аргументи виклику (`view="atm"`).
- Нових I/O, ордерів і ключів немає, обсяг змін не виходить за межі злиття.

## Зауваження (не блокують)
1. Низька. У `assistant_snapshot.py` ключі `term` і `skew` тепер викликають той самий інструмент з різними `view`. Стан обох викликів залежить від одного й того ж ланцюжка Deribit, що не змінює поведінку, бо `_chain` кешується в `Http`. Дії не потрібні, лише знати це.
2. Низька. Рядки у двох docstring аналітики (`calendar_p10.py` ~ рядок 53, `option_ideas.py`) трохи довші за звичну довжину. Lint проходить, тож змінювати не потрібно.
3. Низька. Лічильник «104 інструменти» стоїть у PARITY і в нотатці. Перерахувати його з `EXPECTED_TOOLS` координатор може під час повного прогону.
