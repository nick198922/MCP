# Рев'ю: пункт 197, зіпсоване значення в холодному кеші, усі види для ccxt, не лише перше входження (режим C)

Предмет: незакомічені зміни `src/tradedesk/providers/exchanges.py`, `src/tradedesk/tools/perps.py`, `src/tradedesk/tools/market.py`, `tests/test_tools_wrong_type_fields.py`, `docs/BACKLOG.md` (6.41), `docs/HANDOFF.md`, `docs/ideas/IDEA-20261102-standing-improvements.md` (пункт 197), `docs/changes/2026-11-07-wrong-type-cached.md`.

## Вердикт: approve

Блокерів нема. Інваріанти 1–4 у змінених місцях дотримано. На коректних відповідях ccxt числа не змінюються. Нові тести падають на мутантах. Чотири невиправлені мутації записано чесно, і моя перевірка це підтверджує. Знахідки 1–3 варто взяти наступним кроком.

## Що я перевірив сам

- `make lint`: ruff check і format, чисто.
- `uv run pytest -q -n auto tests/test_tools_wrong_type_fields.py`: 180 passed, 22.8 с. Збігається із записом у змінах.
- Тести змінених модулів (`test_server`, `test_pagination`, `test_provider_failures`, `test_tools_never_raise`, `test_tools_malformed_200`, `test_tools_envelope_shape`, `test_tools_provider_outage`, `test_funding_spread`, `test_funding_spread_hl`, `test_carry_paper`, `test_dashboard`): 289 passed.
- Мутанти. Робив копію `src/` і `tests/` у `/tmp`, файл повертав до `HEAD` і запускав тест на копії.
  - `tools/market.py` до `HEAD`: падають `market_ohlcv`, `market_ticker`, `market_tickers`.
  - `tools/perps.py` до `HEAD`: падають `funding_carry_scan`, `funding_spread_scan`, `perps_funding_history`, `perps_open_interest`.
  - `providers/exchanges.py` до `HEAD`: падають `funding_carry_scan`, `funding_spread_scan`, `perps_funding_history`, `cycle_regime`, `market_flow`.
  - Разом 9 інструментів, плюс 2 з `KNOWN`, тобто 11. Це збігається з «74 порушення в 11 інструментах» до виправлень. Кожне з дев'яти виправлень закрите тестом.
  - Холодний кеш: прибрав `AttributeError` з `except` у `HyperliquidProvider._name_table`. Падають 5 параметрів `test_no_tool_raises_on_a_bad_value_in_a_cold_cache` (`hyperliquid_candles`, `_book`, `_funding_history`, `_trades`, `perps_funding_history`). Отже, новий тест справді ламає кешовану мапу монет і бачить виняток, що вилітає назовні.
  - `KNOWN = {}`: `carry_paper_open` дає 3 порушення, `carry_paper_status` 1, разом 4. Усі чотири: `ValueError: could not convert string to float: 'abc'` у `collector/market_data.py:53` (`funding_entries`). Запис у змінах, у BACKLOG 6.41 і в `KNOWN` правдивий.
- Числа на коректному вході, перевірено читанням диффу.
  - `_ts`: на ціле чи float повертає те саме `int(x)`, що й раніше. Відкидає лише `None`, `bool` (раніше `int(True)` давало 1) і нечислове.
  - `parsed_rows` повторює ті самі лямбди, що були в list comprehension.
  - `json_num` пропускає int і float без змін. Відкидає числові рядки в `fundingRate`, `markPrice`, `bid`/`ask`, OI і `last`, але ccxt нормалізує ці поля через `safe_number`, тож на реальних відповідях різниці нема.
  - Кап (рядок біржі) і далі парситься `float` через `parsed_rows`.
  - Мережевого коду не додано: усе через `Exchanges._call` і `Http`.
- Живу перевірку з нотатки («перевірено наживо 07.11 через `build_server()`») я не відтворював. Це твердження автора.

## Знахідки

1. **Середня. Інваріант 1 у `carry_paper_open` і `carry_paper_status` досі порушено.** `fundingRate` `"abc"` / `[]` у будь-якому рядку історії валить інструмент через `funding_entries`. Записано чесно, і межа «колектори не чіпаємо» зрозуміла: функція спільна з процесом колектора. Але виняток вилітає саме з MCP-інструменту, а правка на два рядки. Виправлення: у `collector/market_data.funding_entries` замінити comprehension на `parsed_rows(...)`, з тим самим фільтром і тією самою лямбдою (на коректних рядках результат той самий), прибрати обидва записи з `KNOWN`, закрити рядок у 6.41. Бажано взяти першим наступним пунктом.

2. **Низька. `KNOWN` ховає нові порушення в цих двох інструментах.** Для них тест перевіряє лише, що `failures` не порожній (`test_no_tool_raises_on_a_wrong_type_field`, рядки 303–304). Якщо в `carry_paper_open` з'явиться друге, не пов'язане порушення, тест лишиться зеленим. Виправлення: для `KNOWN` перевіряти, що кожен ключ `failures` відповідає задокументованому полю, наприклад `all(".fundingRate -> " in k for k in report["failures"])`. Або зберігати в `KNOWN` регулярний вираз ключа.

3. **Низька, існувала й до зміни. `market_ohlcv` кидає `IndexError`, коли жодне закриття не є числом.** `closes = [...]`, а далі одразу `closes[0]` і `closes[-1]` (`tools/market.py:73–75`). Достатньо, щоб усі `c[4]` були `None`, `0` або нечисловими. Тест цього не ловить, бо ламає одне входження. Виправлення: `if not closes: b.miss("ohlcv", f"ccxt:{exchange}", "data_unavailable", "no numeric close"); return b.build(None, ok=False)`.

4. **Низька, поведінка, не інваріант 1. Одна зіпсована назва в `universe` вимикає всю мапу монет.** `_name_table` будує таблицю одним comprehension: виняток на одному рядку, і на холодному старті мапи нема зовсім. Тоді всі монети отримують `unavailable` і назву у верхньому регістрі, а `kPEPE` перетворюється на хибне `KPEPE`. Так триває до повторної спроби через 60 с, а якщо поле зіпсоване стабільно, то назавжди. Нотатка це описує, але як прийнятну поведінку. Виправлення: пропускати лише погані рядки (`parsed_rows(universe, lambda u: (u["name"].lower(), u["name"]))`, `AttributeError` додати до перехоплюваних або перевіряти `isinstance(u.get("name"), str)`). Тест холодного кешу тоді може перевіряти, що `BTC` дає `exact`.

5. **Низька, записано у «Відкритому».** Ряд ccxt-фандингу з поганим `timestamp` мовчки пропускається, а `complete` лишається `true`. У Hyperliquid у такому разі `complete=false` і `dropped_rows`. Для узгодженості варто рахувати `dropped_rows` і в `funding_rate_history_range`. Блокером це не є: раніше на такому ряду виняток вилітав з інструменту.

6. **Інформаційна.** Самоперевірка `test_the_cold_check_reaches_both_caches` рахує лише кількість мутацій (`>= 2`) і не показує, що холодний тест ловить виняток. Мій мутант із пункту «Що я перевірив сам» показує, що ловить. Можна додати пробу на зразок `zz_schema_trusting_probe`, яка робить `float()` над полем `load_markets`, щоб це було закріплено в коді.

## Обсяг

Зайвого нема. Змінено лише місця, які знайшов тест, плюс гілку Hyperliquid у `funding_spread_scan` на випередження, з поясненням. Нових інструментів нема, тож чекліст інваріанту 5 не застосовується. Запис у `docs/changes/` є, BACKLOG 6.41 і картку пункту 197 оновлено.
