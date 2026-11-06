# Рев'ю: одиниці в описах інструментів, пункт 185 (режим C)

Вердикт: **approve**

Перевірив сам: `git diff` (src, scripts, tests, docs/changes), `make lint` (чисто), `uv run pytest -q tests/test_tool_desc_audit.py tests/test_server.py` (87 passed). Кожне твердження про одиниці звірив із кодом інструмента.

## Зміни в src
У `market.py`, `perps.py`, `events.py`, `options.py`, `quant.py` змінено лише рядки `description=`. Логіки, сигнатур і схем параметрів немає в diff. Анотації `READ_ONLY` на місці.

## Звірка тверджень з кодом
1. `market_ohlcv` (`market.py:72-80`): `return_pct = (last/first - 1) * 100`, тобто відсотки. `realized_vol_annualized` це десятковий дріб із множником `sqrt(365*86400/tf_sec)`, виводиться лише при `len(closes) >= 10`. `ts_ms` це епоха в мс. Усе збігається. Твердження «volume passed through via ccxt» нічого не вигадує.
2. `hyperliquid_candles` (`perps.py:1052-1069`): ті самі формули. `t` береться як `int`, `v` як `float` без перерахунку. «USDC for standard perps» правильно, бо формулювання обмежене словом «standard». Збігається.
3. `event_study` (`analytics/event_study.py:76-95`): `close_vs_t0 = c_after/c0 - 1`, `max_up` і `max_dd` рахуються відносно `c0` і дають десяткові дроби. `volatility = stdev(log-returns) * sqrt(n_rets)`, не річна, тобто опис «per-bar stdev times sqrt(bars in the window), NOT annualised» точний. `direction` це 0/1, `hit_rate` це середнє. Збігається.
4. `prob_from_options` (`options.py:505-524`): `probability` це дріб 0-1, `forward`, `level` і `one_sigma_range` у USD, `one_sigma_range = F*exp(±atm_iv*sqrt(T))`, `days = T*365`, `iv_at_level` і `atm_iv` це десяткові дроби. Збігається з `docs/ARCHITECTURE.md` (T = дні/365, Deribit-експірація 08:00 UTC).
5. `touch_probability` (`quant.py:75-91`): `analytic` і `monte_carlo` це дроби, `one_sigma_move_pct = iv*sqrt(T)*100`, `T = days/365`. Збігається.

Хибних тверджень не знайшов.

## Скрипт і тести
- `REQUIRED_UNITS` це явна мапа інструмент → список обов'язкових слів. Вона закриває прогалину токен-правила: назви без токена одиниці не перевірялися. Перевірка йде по всіх описах, а не лише по зареєстрованих, і `test_registered_tools_load_and_pass` ганяє її на справжньому реєстрі.
- Тест `test_required_units_for_tools_without_a_unit_token` перевіряє, що ключі мапи рівні множині `full`, що кожне слово перевіряється окремо (видалення одного дає рівно одну знахідку) і що негативні випадки не проходять («fraction of paths» не рахується як «fraction 0-1», USDT не рахується як USD). Інструмент поза мапою не штрафується. Тест впав би, якби логіку зламали.
- Старі тести (`test_normal_tool_has_no_findings`, `test_missing_risk_neutral_or_heuristic_caveat`) коректно доповнені словами одиниць, тож вони досі ізолюють лише перевірку застереження.

## Зауваження (не блокують)
1. [низька] Регулярки `\bdecimals?\b` і `\bpercent\b` лише підтверджують, що слово є в описі, а не що воно стосується потрібного поля. Це обмеження вже зазначене в docstring («only sees that a unit is named»), тож змін не потрібно.
2. [низька] Опис `event_study` каже «bars in the window», а вікно для `volatility` починається з `i_lo`, тобто включає «before»-частину. Для `max_up` і `max_dd` вікно починається з `i0`. Формулювання не хибне, але його можна уточнити, якщо буде нагода.
3. [info] Запис у `docs/changes/2026-11-06-tool-desc-units-2.md` є (ще untracked). Його треба додати в коміт разом з рештою.
