# Рев'ю: описи параметрів options/perps/market (пункт 27 постійної картки)

Режим C. Вердикт: **approve** (з двома зауваженнями низької серйозності і одним окремим реальним багом поза обсягом).

## Що перевірено сам
- `git diff`: у `src/tradedesk/tools/{options,perps,market}.py` змінено лише анотації параметрів (`Annotated[..., Field(description=...)]`, нові аліаси `MIN_DAYS`, `MAX_DAYS`, `INCLUDE_HL`, `HL_COIN`, `PERIOD`, `SERIES_LIMIT`, `INCLUDE_SERIES`). Значення за замовчуванням, обмеження (`ge/gt/le`) і тіла функцій не змінені. Поведінка та сама.
- `make lint`: ruff check і format чисті.
- `uv run pytest -q tests/test_tool_metadata.py tests/test_server.py`: 71 passed.
- Allowlist у `tests/test_tool_metadata.py`: видалені рівно ті записи, чиї параметри тепер описані (46 параметрів у 22 інструментах). Решта записів не зачеплена. Тест на застарілі записи проходить, тож список узгоджений. Лічильник у коментарі (59 у 26) відповідає сказаному в changelog.
- Мова: усі описи англійською.
- Точність текстів зі звіркою з кодом:
  - `min_days/max_days`: код `min_days <= days <= max_days` (options.py:128, 346, 711, 762), тож «inclusive» вірно.
  - `width_pct`: код `abs(math.log(strike/F)) <= width_pct/100` (210, 415), тож опис «|ln(strike/forward)|» вірний.
  - `oi_by_strike.top_n`: три списки (434-436), вірно.
  - `hyperliquid_overview`: фільтр `min_oi_usd` до зрізу `top_n`, `ascending` через `reverse=not ascending`, `funding_annualized` simple (рядок 975), вірно.
  - `prob_from_options.kind`: наявне застереження про ризик-нейтральність (інваріант 8), вірно.
  - Одиниці: funding як десятковий дріб (0.10 = 10 %), ставка за інтервал як decimal, інваріант 4 дотримано.
  - OKX «about 3 months» узгоджується з докстрінгом на perps.py:89.

## Зауваження
1. (низька) `HL_COIN` стверджує, що підтримується builder-deployed perp у форматі `dex:NAME` (напр. `xyz:SP500`). У коді `lookup_coin`/`_hl_name` я не знайшов жодної обробки такого формату, а зразка в `tests/fixtures/` немає. Якщо це не підтверджено живою відповіддю HL, прибрати цю частину опису або позначити «unverified», бо опис інструмента спонукає модель передавати такі імена. Не блокує.
2. (низька) `market_orderbook.depth`: «Impact estimates use only these levels» правильне по суті, але я окремо не звіряв з `analytics/orderbook`. Не блокує.

## Позначений збирачем можливий баг: deribit_largest_trades, notional для futures
**Реальний, але поза обсягом цього diff (поведінку не змінено, виправляти тут не слід).**
- Код options.py:845-847: `notional_usd = amount*price*index if kind == "option" else amount*price`, коментар «For dated futures price is USD; use amount * price directly» трактує `amount` як кількість монет.
- Документація Deribit (зовнішня, у репозиторії її немає): для perpetual та inverse futures `amount` виражено в USD (контракт 10 USD для BTC, 1 USD для ETH), для опціонів у базовій монеті. Тоді USD-номінал ф'ючерсної угоди дорівнює `amount`, а `amount*price` завищує його приблизно в ціну BTC разів (~10^5). Це ламає `min_notional_usd`, сортування `top_n`, `buy/sell_notional_usd` і дисбаланс для `kind="future"`.
- Перевірка в межах репозиторію: підтвердження чи спростування в `docs/` або `tests/fixtures/` немає. Тести (`test_server.py:888-901`, `test_provider_failures.py:54`) покривають лише `kind="option"`, тож ф'ючерсна гілка не протестована взагалі. Висновок спирається на специфікацію Deribit, а не на зразок із репозиторію.
- Нова опис параметра `kind` («Deribit futures, including the perpetual») нічого не стверджує про одиниці, тож не поглиблює проблему.
- Рекомендація: окремий пункт у BACKLOG: взяти реальний зразок `get_last_trades_by_currency kind=future` (власник через curl), зберегти у `tests/fixtures/`, виправити формулу (`notional = amount` для inverse) і додати тест на futures-гілку. До виправлення додати застереження у `description` інструмента, що для `kind="future"` USD-номінал може бути завищеним.

## Документи
`docs/changes/2026-11-03-param-descriptions-2.md` присутній, оновлені `docs/BACKLOG.md` і картка постійних покращень. Нових інструментів немає, тож README/PARITY/EXPECTED_TOOLS не потрібні.
