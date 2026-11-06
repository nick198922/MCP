# Рев'ю: поля не того типу в правильній відповіді (пункт 184)

Режим C (код). Предмет: незакомічені зміни робочого дерева `tradedesk-agents` поверх `f0a898f2`: `tests/test_tools_wrong_type_fields.py`, `tools/_util.py` (`parsed_rows`, `json_num`), `analytics/{options,backtest}.py`, `providers/hyperliquid.py`, `tools/{options,perps,market,markup,history}.py`, `docs/changes/2026-11-06-wrong-type-fields.md`, рядки в BACKLOG/HANDOFF/картці.

**Вердикт: `request changes`.** Один блокер (1), решта не блокує.

## Що перевірено самостійно

- `make lint`: чисто.
- `tests/test_tools_wrong_type_fields.py`: 115 passed.
- Тести модулів, яких торкнулися зміни (`test_analytics`, `test_vrp`, `test_backtest`, `test_pagination`, `test_ranges`, `test_pm_gap`, `test_forecast_log`, `test_pre_event_vol`, `test_tools_never_raise`, `test_tools_malformed_200`, `test_tools_envelope_shape`, `test_tools_provider_outage`, `test_provider_failures`, `test_server`, `test_dashboard`, `test_forecast_weekly_tg`): 395 passed. Числа для правильних відповідей ці тести не зрушили.
- Сила тесту (мутаційна перевірка): скриптом поза репозиторієм я по черзі повертав кожен із 8 змінених файлів коду до `HEAD` у копії `src/` і запускав новий тест. **Кожне повернення ловиться**: `backtest.py` 1 падіння, `analytics/options.py` 11, `hyperliquid.py` 2, `history.py` 1, `market.py` 1, `markup.py` 1, `tools/options.py` 3, `perps.py` 8. Самоперевірка `test_the_check_catches_a_tool_that_trusts_the_schema` є і має сенс: зонд без перевірки типу падає. Умова `hits == mutations` гарантує, що кожна обрана мутація справді застосована.
- Інваріант 1: виконано для всіх 62 інструментів з мутаціями, `KNOWN` порожній.
- Інваріант 3: `analytics/` лишився чистим, без I/O (`_json_num` локальний, `tools/` не імпортується).
- Інваріант 4: одиниці не змінено (`mark_iv / 100`, DVOL `/ 100`, `funding_8h` на 8 год).
- Числові рядки Deribit тепер вважаються відсутніми: усі виклики `build_chain` (`tools/options.py:77`, `forecast_log.py:86`, `assistant_paper.py:127`, `collector/scheduler.py:120`, `collector/pm_gap.py:153`) отримують живий JSON Deribit, де ці поля приходять числами. Нічого з numpy/pandas у `build_chain` не потрапляє (`np.int64` інакше став би `None`). Для правильних відповідей поведінка та сама.
- Відкинуті рядки: `signals_momentum` і `run_backtest` нормалізують через ту саму `_normalise_bars`, а інших викликачів у `src/` і `scripts/` немає, тож вирівнювання позицій збережено.
- `execution/` не змінено. Нових інструментів немає, тож чекліст README/PARITY не застосовується. Файл `docs/changes/` є.

## Зауваження

1. **Блокер (середня серйозність): Hyperliquid `funding_history_range` мовчки обрізає вікно і лишає `complete=True`.** `providers/hyperliquid.py:242-246`: рішення гортати далі ухвалюється за `len(batch) < 500`, де `batch` уже без рядків із поганим `time`. Повна сторінка з 500 рядків, де один рядок зіпсований, дає 499 і завершує прохід. Я відтворив це пробним тестом із `MockTransport`: 1200 рядків, у рядку 10 `time: "abc"`. Результат `complete=True, rows=499, pages=1`: втрачено 58 % вікна, і жодного попередження. `_warn_short_coverage` перевіряє лише початок вікна, а не кінець, тож `perps_funding_history` / `hyperliquid_funding_history` віддадуть урізаний фандинг як повний. До зміни такий рядок валив інструмент. Тепер це тихі хибні дані, а для грошового рішення це гірше. Автор це визнає в `docs/changes` («`complete` лишається `true`»), але виправлення займає кілька рядків, а саме цей рядок зміна вже переписує. Виправлення:
   - гортати за сирим розміром сторінки: `page = [x for x in (f.data or []) if isinstance(x, dict)]`; `if len(page) < 500: break`;
   - `nxt` брати з `max` валідних `t`. Якщо сторінка повна, а валідного `t` немає, ставити `complete = False; break`;
   - якщо на повній сторінці щось відкинуто, теж ставити `complete = False` (або окремий лічильник у `evidence.args`), щоб інструмент написав попередження;
   - у `_ms` відкидати `bool` (`int(True) == 1` зараз дає рядок 1970 року);
   - додати регресійний тест у `tests/test_pagination.py` за зразком `test_hyperliquid_funding_range_walks_500_row_pages` з одним зіпсованим `time` на першій сторінці: очікувати або 1199 рядків і `complete=True`, або `complete=False`, але ніколи 499 рядків із `complete=True`.

2. **Низька: тихе відкидання барів у `strategy_backtest_momentum`.** Відкинутий внутрішній бар зливає два бари в одну дохідність. Інструмент перевіряє `len(bars)` до нормалізації і про відкинуте не повідомляє. Виправлення: у `tools/backtest.py` порівнювати кількість нормалізованих рядків із сирими й робити `b.warn(f"{k} bars with non-numeric fields dropped")`, коли `k > 0`.

3. **Низька: `deribit_vrp` мовчки зупиняє гортання DVOL на `continuation` не того типу** (`tools/options.py`, `nxt = parsed_rows([cont] …)`). Ряд IV урізається без попередження. Виправлення: `b.warn(...)`, коли `cont` є, а `nxt` порожній.

4. **Низька: `range_setup_scan` відкидає весь бар, коли `volume` дорівнює `None`** (`tools/history.py`, `float(x[5])`). Для біржі, де ccxt віддає `None` в об'ємі на кожному барі, інструмент тепер відповідає `insufficient_bars`, хоча `chart_markup` у такому разі ставить 0 (`float(x[5] or 0.0)`). Виправлення: `float(x[5] or 0.0) if len(x) >= 6 else 0.0`, як у `markup._bars`.

5. **Низька: `OverflowError` не перехоплюється.** `httpx` розбирає JSON через `json.loads`, а той приймає `Infinity`, і тоді `int(inf)` у `parsed_rows` або в `_ms` кидає `OverflowError`, якого немає в переліку винятків. Від справжніх бірж такого чекати не варто, але виправлення дешеве: додати `OverflowError` до кортежів винятків у `parsed_rows`, `_ms` і `_normalise_bars`.

6. **Низька, підтримуваність:** `analytics/options._json_num` дублює `tools/_util.json_num`. Краще тримати одну функцію в `analytics/` (наприклад, в `analytics/options.py` або в окремому модулі утиліт) і імпортувати її в `tools/_util.py`, щоб правила не розійшлися.

## Умова approve

Виправити пункт 1 і додати до нього регресійний тест. Пункти 2–6 бажано зробити, але вони не блокують. Виправлені пункти занотувати в `docs/changes/2026-11-06-wrong-type-fields.md` і прибрати відповідне речення з «Не покрито».

## Повторне рев'ю

Предмет: той самий незакомічений стан після раунду виправлень (`git diff` поверх `f0a898f2`).

**Вердикт: `approve`.** Блокер 1 виправлено правильно, регресійний тест справді ловить помилку. Пункти 2–6 зроблено. Нижче три дрібниці, вони не блокують.

### Що перевірено самостійно

- `make lint`: чисто.
- Тести: `test_pagination`, `test_tools_wrong_type_fields`, `test_backtest`, `test_vrp`, `test_analytics`, `test_ranges`, `test_markup`, `test_tools_never_raise`, `test_tools_malformed_200`, `test_tools_envelope_shape`, `test_tools_provider_outage`, `test_server`, `test_hl_coin_case`, `test_funding_spread_hl`, `test_carry_paper_engine`. Разом 364 passed.
- **Пункт 1 (блокер), виправлено.** `providers/hyperliquid.py`: рішення гортати далі тепер ухвалюється за сирим розміром сторінки `len(page) < 500`. `nxt` береться з `max` валідних `t`. Повна сторінка без валідного часу дає `complete=False` і зупиняє гортання. Будь-який відкинутий рядок ставить `complete=False`, лічильник лежить у `evidence.args["dropped_rows"]`. `_ms` відкидає `bool` і ловить `OverflowError`. Сила тестів перевірена мутаціями в копії дерева поза репозиторієм:
  - повернення файла до `HEAD`: обидва нові тести падають;
  - відновлення помилки першого раунду (`len(batch) < 500`): тест падає, бо отримує 498 рядків замість 1198;
  - прибрано `if dropped: complete = False`: тест падає (`True is False`);
  - прибрано перевірку `bool` у `_ms`: тест падає, бо отримує 1199 рядків і `True` серед часів.
- Мій пробний сценарій: зіпсований рядок стоїть **останнім** на повній сторінці (рядок 499), а рядок 700 має час-рядок із цифрами. Результат: 1199 рядків, 3 сторінки, `complete=False`. Числовий рядок часу, як і до зміни, приймається, тож поведінка для правильних відповідей та сама. Наступна сторінка починається з позиції зіпсованого рядка, і той рахується в `dropped_rows` двічі. Це косметика: на `complete` не впливає.
- Споживачі `complete` (`perps.py` 344/364/500/1195, `collector/carry_paper_feed.py`): `False` дає попередження або `covered_from/to=None`. Це консервативно, нічого не ламає. Тексти попереджень у `perps_funding_history` і `hyperliquid_funding_history` тепер називають нову причину.
- **Пункт 2, зроблено.** `tools/backtest.py` попереджає, скільки барів відкинуто.
- **Пункт 3, зроблено.** У `deribit_vrp` `continuation` не того типу (зокрема `bool`) дає `b.warn(...)` і зупиняє гортання. Для числа й числового рядка поведінка як раніше.
- **Пункт 4, зроблено.** У `range_setup_scan` `volume=None` тепер дає 0.0, як у `chart_markup`.
- **Пункт 5, зроблено.** `OverflowError` додано в `parsed_rows`, `_ms` і `_normalise_bars`.
- **Пункт 6, зроблено.** Лишилась одна функція `analytics/options.json_num`, `tools/_util` її реекспортує. `_json_num` більше ніде немає.
- `docs/changes/2026-11-06-wrong-type-fields.md` має розділ «Після рев'ю». Речення про `complete=true` прибрано з «Не покрито».

### Залишкові зауваження (низькі, не блокують)

1. **Залишок пункту 5 у двох нових місцях.** Розбір `ts` перенесено всередину `try`, але там ловляться лише `(TypeError, ValueError)` (`tools/options.py`, `deribit_largest_trades`) і `(KeyError, TypeError, ValueError)` (`tools/perps.py`, `hyperliquid_trades`). Якщо в `timestamp`/`time` прийде `Infinity`, `int(inf)` кине `OverflowError` назовні, а загального перехоплювача в `server.py` немає. Від реальних бірж такого чекати не варто. Виправлення: додати `OverflowError` до обох кортежів.
2. **Пункти 2–4 не закріплені тестами.** `test_tools_wrong_type_fields` перевіряє лише, що інструмент не кидає виняток і повертає булевий `ok`. Він не перевіряє ні попереджень, ні того, що бар з `volume=None` лишається. Виправлення: три малі тести. (а) `strategy_backtest_momentum` з одним баром `close=None` має дати попередження `malformed bars`. (б) `deribit_vrp` з `continuation: "abc"` має дати попередження `malformed continuation`. (в) `range_setup_scan` з `volume=None` на всіх барах не має давати `insufficient_bars`.
3. **`strategy_backtest_momentum`.** Перевірка мінімуму барів (`max(lookback + 5, 50)`) іде до нормалізації, тож після відкидання барів може лишитися менше за мінімум. Попередження про це вже є. Крім того, `tools/` імпортує приватну `_normalise_bars`. Варто перейменувати її на публічну або перенести перевірку довжини після нормалізації.
