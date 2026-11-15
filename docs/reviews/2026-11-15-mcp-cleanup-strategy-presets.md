# Рев'ю C: пресети стратегій у `strategy_payoff` (mcp-cleanup, пункт 1)

Дата: 2026-11-15. Режим C (код). Предмет: незакомічений робочий стан (`git diff` + `docs/changes/2026-11-15-mcp-cleanup-strategy-presets.md`), картка [`IDEA-20261114-mcp-cleanup`](../ideas/IDEA-20261114-mcp-cleanup.md), пункт 1.

## Вердикт: **approve**

Блокерів немає. Нижче чотири дрібні зауваження, їх можна виправити цим самим кроком або наступним.

## Що перевірив сам

- `make lint`: чисто (ruff check і format).
- `uv run pytest -q -n auto tests/test_strategy.py tests/test_server.py tests/test_tools_never_raise.py tests/test_tools_envelope_shape.py tests/test_calc_degenerate_inputs.py tests/test_tool_failure_extra_9.py tests/test_mcp_call.py`: 244 passed. Решту набору (`test_tool_desc_audit`, `test_docs_tool_coverage` тощо) не запускав: це зробить координатор.
- **Числа ті самі, що в старих інструментах (перевірено незалежно від тестів автора).** Скрипт `~/.cache/tradedesk-agents/critic_preset_equiv.py` завантажує `register` з `git show origin/main:src/tradedesk/tools/strategy.py`, піднімає старі `strategy_straddle / _strangle / _risk_reversal / _iron_condor` поруч із новим `strategy_payoff(preset=…)` і порівнює `ok`, `data`, `missing`, `warnings`. 13 випадків: типові значення, `side="short"`, `direction="bearish"`, `size`, `forward`, `kind="detailed"`, `grid` / `width_pct`, перехрещені страйки risk reversal, неправильний порядок страйків strangle і condor (`missing: strikes`), переповнення (`missing: strategy`), `size=inf` (`missing: input`). Розбіжностей **0 з 13**.
- Інваріант 1: `_preset_problem` гарантує, що всі обов'язкові параметри є до `_preset_legs` (без `KeyError`); `ValueError` кондора ловиться як раніше; NaN / inf проходять через `_answer` → `reject_nonfinite` так само, як у старих шаблонах. Нові відмови (`neither`, `both`, бракує параметра, чужий параметр, параметр пресета без `preset`) дають `ok=false` / `invalid_input`, тест `test_bad_preset_call_is_invalid_input_not_raised` це покриває. Невідомий пресет відхиляє схема (тест є).
- Інваріант 4: одиниці не змінились (премії USD за контракт із застереженням про BTC/ETH, IV десятковим дробом, T = days/365, r = 0); опис `strategy_payoff` тепер явно це каже.
- Інваріант 5: `EXPECTED_TOOLS` оновлено; `README.md` (105, рядок «Стратегії»), `docs/PARITY.md` (рядок шаблонів прибрано, `strategy_payoff` доповнено), запис у `docs/changes/`, HANDOFF і BACKLOG оновлені. Анотація `READ_ONLY` збережена.
- Описи: зміст усіх чотирьох старих описів (breakevens, max loss, skew у RR, ширина крила мінус кредит, порядок страйків) перенесено в опис `strategy_payoff` і в описи параметрів `_P_*`. Опис тепер 1888 символів, схема 6460 символів JSON: один інструмент замість п'яти, тож сумарно схема менша.
- Поведінка `legs` без змін: `kind` за замовчуванням `detailed` (`None` → детально), перевірки ніг ті самі. Порядок параметрів змінився (`spot, days, iv` перед `legs`), але викликів за позицією немає: у `src/`, `scripts/`, `.claude/` прямих викликів функції не знайдено.
- Згадки старих назв поза історичними файлами: лише `docs/QUESTIONS.md` (текст питання Q-20261114-1), `docs/BACKLOG.md:183` (журнал 6.53), картки `IDEA-20261114-mcp-cleanup` і `IDEA-20261114-options-playbook`, сам `strategy.py` (коментар і опис «replace the former…»). Скрипти й промпти, що викликали б старі імена, не знайдено.

## Зауваження

1. **Низька. Застаріла кількість у `docs/PARITY.md:76`:** «Наш сервер: 109 інструментів; за 30 днів напряму викликали 17». Виправлення: «105 інструментів (15.11, після злиття пресетів; на аудит 14.11 було 109)» або явно позначити рядок як знімок аудиту 14.11.
2. **Низька. `src/tradedesk/prompts/pre_trade_checklist.md:66`:** «`strategy_payoff` або шаблони» — модель може шукати окремі інструменти-шаблони, яких уже немає. Виправлення: «`strategy_payoff` (власні ноги або `preset`)».
3. **Низька. Тест рівності частково тавтологічний.** `test_preset_matches_the_former_template_tool` порівнює відповідь із поточним `_packed(build_*(…))`, тобто з тим самим кодом, а не зі старим інструментом; від регресії в `_packed` він не захищає (захищають еталони `test_preset_reference_numbers` і старі тести аналітики). Рівність зі старим кодом я підтвердив скриптом вище. Виправлення не обов'язкове; за бажання — закріпити в тесті кілька чисел (breakevens, `max_profit_usd`, `max_loss_usd`, `greeks.delta`) одного з випадків `PRESET_CASES` як літерали.
4. **Інформаційна. Картка `IDEA-20261114-mcp-cleanup.md`, пункт 1, досі `[ ]`**, а BACKLOG позначає його зробленим. Позначити пункт виконаним із посиланням на `docs/changes/2026-11-15-mcp-cleanup-strategy-presets.md` (координатор / автор картки). У `CLAUDE.md` «109 шт.» — правка власника, у записі змін це вже сказано.

## Чого не перевіряв

- Повний набір тестів (за дорученням — лише перелічені файли).
- Живий виклик через службу `127.0.0.1:8000`: зміна чисто обчислювальна, без мережі.
