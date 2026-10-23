# Рев'ю коду: історія OI / funding / лонг-шорт з Coinalyze (2026-10-23)

Режим C. Предмет: незакомічений diff (`providers/coinalyze.py`, `tools/coinalyze.py`, `tests/test_server.py`, README, PARITY, HANDOFF, BACKLOG) і нові файли `src/tradedesk/analytics/coinalyze_series.py`, `tests/test_coinalyze_history.py`, `docs/changes/2026-10-23-coinalyze-history-tools.md`.

## Що перевірено самостійно

- `make lint`: чисто (ruff check і format).
- `uv run pytest -q tests/test_coinalyze_history.py tests/test_server.py tests/test_coinalyze.py`: 99 passed. Інструменти є в `EXPECTED_TOOLS`, `liquidations_history` проходить свої старі тести.
- Фікстури, на які спираються тести (`tests/fixtures/doubletop/coinalyze__*_2026-10-18.json`, `ours__binanceusdm_funding_history_BTCUSDT_2026-10-18.json`, `ours__binance_globalLongShortAccountRatio_BTCUSDT_1d_2026-10-18.json`), вже є в git.
- Одиниці перевірив окремим скриптом на фікстурах:
  - funding: `c/100` дорівнює останній виплаті Binance за добу UTC, `h/100` дорівнює найбільшій виплаті, і так усі 4 дні. Твердження «Coinalyze шле відсотки, ділимо на 100» підтверджене (0.01 → 1e-4, базова ставка Binance).
  - лонг-шорт: `l/100`, `s/100` і `r` збігаються з `longAccount`, `shortAccount`, `longShortRatio` Binance на тих самих мітках часу.
  - OI: `open` Coinalyze на мітці T дорівнює `sumOpenInterest` Binance на T з точністю до 7 BTC, а `close` бару T приблизно дорівнює Binance на T+1д (розбіжність близько 0.24 % у двох останніх днях). Отже, «мітка Coinalyze = початок інтервалу, Binance ставить кінець» правдоподібне. Значення в зразку в монетах (~93k BTC), не в USD.
- Мережа йде тільки через `Http.get_json` у провайдері. Ключ передається лише в заголовку `api_key`, у URL, params, evidence і в результаті його немає (це перевіряє тест `test_oi_history_happy_and_key_not_leaked`). У логах httpx URL без ключа.
- `analytics/coinalyze_series.py` чистий: без I/O, без годинника.
- Описи англійською, з одиницями й застереженнями (лише Binance, не річна ставка, рахунки, а не нотіонал, «not a signal»), `READ_ONLY`, `Annotated[..., Field]`.
- Є тести на щасливий шлях, на 503, 401, відсутній ключ (без запиту), порожню відповідь, неправильну форму відповіді й неправильний символ (без запиту).
- Поведінка `liquidations_history` та сама: ті самі параметри запиту, вирівнювання `start`, текст попереджень. Змінилося одне: попередження про обрізку hourly тепер з'являється й тоді, коли провайдер упав (`ok=false`). Це нешкідливо.
- Документація: README, PARITY, BACKLOG, HANDOFF і `docs/changes/` оновлено. Зайвого обсягу в зміні немає, коду ордерів немає.

## Зауваження

1. **Блокер: порушено інваріант 1, інструмент кидає виняток.** У `ohlc_series` і `long_short_series` виклик `_iso(t)` (`datetime.fromtimestamp`) стоїть поза `try`, а `int(h["t"])` не ловить `OverflowError`. Перевірив пробою через MCP-клієнт із `MockTransport`. Рядок `{"t": 10**14, ...}` (або `t` = ±Infinity: `json.loads` приймає `Infinity`) ламає всі три нові інструменти: `UnexpectedToolError`, `isError=true`, конверта `ok/missing` немає. Дані із зовнішнього API недовірені, тож «неправильний рядок» має відкидатися, як це обіцяє docstring («malformed rows dropped»).
   Виправлення: перенести `_iso(t)` усередину `try` і ловити `(KeyError, TypeError, ValueError, OverflowError, OSError)`. Краще відкидати й `t` поза розумними межами (наприклад, від 2009 року до `now` + 1 крок). Додати в `test_series_edge_cases` рядки з `t=10**14`, `t=-10**13` і `t=float("inf")` для обох функцій, а також тест через інструмент, який перевіряє `ok=false` і `reason=data_unavailable`.
   Той самий шаблон уже давно живе в `analytics/liquidations_agg.summarise` (рядок 54, `fromtimestamp` поза `try`). Варто виправити й там у цьому ж кроці, бо `liquidations_history` вразливий так само.

2. **Середнє (не блокер): `open` і `low` денного funding не завжди є виплатами цієї доби.** На зразку за 2026-10-16 (t=1792108800): `o = l = 0.001828 %`, а це остання виплата попередньої доби. Перша виплата самої доби 5.429e-5, найменша 4.313e-5. Тест перевіряє тільки `close` і `high`, а опис мовчить про `open`/`low`.
   Виправлення: дописати в опис і `warnings`, що `open`/`low` можуть містити останню ставку попереднього бару і що для виплат слід брати `close` (остання) та `high` (найбільша). Ще варто прямо написати, що середнє денних `close` — це не середня ставка за добу і не сума виплат, щоб ніхто не рахував з нього carry.

3. **Середнє (не блокер): hourly funding не перевірено.** При виплаті раз на 8 год більшість годинних барів виплати не містить. Невідомо, що тоді шле Coinalyze: повторює останню ставку, віддає прогнозну чи пропускає бар. Від цього залежать і `gaps`, і `mean`/`zscore`. Пропоную до живої перевірки власником додати для `interval="hourly"` у `coinalyze_funding_history` попередження «hourly bar semantics (settled vs. predicted, forward-filled) not verified», а в пункт BACKLOG (в) додати «перевірити hourly funding».

4. **Низьке: щасливий тест OI маркує дані в монетах як USD.** `test_oi_history_happy_and_key_not_leaked` віддає зразок у BTC (~93k) і перевіряє `unit == "USD"`. Для mock це формально правильно, але тест закріплює неперевірену річ: чи справді параметр `convert_to_usd` працює на `/open-interest-history`. Виправлення: у docstring тесту або фікстури написати, що зразок узято без конверсії (монети), а після живого виклику власника зберегти USD-зразок і перевіряти його порядок величини (≈ 7.6e9 для BTC, як `sumOpenInterestValue` у `ours__binance_openInterestHist_BTCUSDT_1d_2026-10-18.json`).

5. **Низьке: момент, коли відома точка лонг-шорт, не задокументовано.** Мітки Coinalyze LS збігаються з мітками Binance один в один, а мітки OI зсунуті на день. Отже, рядок LS з міткою T, найімовірніше, є знімком на момент T, а не на кінець бару. Для дослідників це важливо (lookahead). Виправлення: одне речення в описі `coinalyze_long_short_history` («row t = Binance snapshot time; known at t»), або позначка, що це не перевірено.

## Вердикт

**request changes.** Блокер один, зауваження 1: нові інструменти кидають виняток на неправильній мітці часу від API (інваріант 1). Після виправлення і тестів на краї можна схвалювати. Зауваження 2–5 бажано закрити в тому ж кроці, бо це правки описів і тестів.

## Re-review (2026-10-23)

### Що перевірено самостійно

- `make lint`: чисто.
- `uv run pytest -q tests/test_coinalyze_history.py tests/test_coinalyze.py tests/test_server.py`: 138 passed.
- Блокер (зауваження 1) виправлено. `parse_ts` у `analytics/liquidations_agg.py` приймає мітки лише з `[2009-01-01, 2100-01-01)` і кидає тільки винятки з `ROW_ERRORS` = `(KeyError, TypeError, ValueError, OverflowError, OSError)`. `ohlc_series`, `long_short_series` і `summarise` викликають його всередині `try ... except ROW_ERRORS`. `_iso`/`fromtimestamp` поза `try` більше ніде немає. Нечислові й нескінченні значення OHLC/LS відкидає `_num`.
- Нова проба `~/.cache/tradedesk-agents/review_probe/probe_ts2.py`: усі чотири інструменти (`coinalyze_oi_history`, `coinalyze_funding_history`, `coinalyze_long_short_history`, `liquidations_history`) через MCP-клієнт з `MockTransport`. Випадки: `t` = 1e14, −1e13, `Infinity`, `NaN`, `1e400`; `history` = 5 або dict; рядки `history`, що є рядком, числом або `null`; значення `Infinity`; payload-dict. Жодного `isError`, у всіх випадках є конверт: `ok=false` + `data_unavailable`, а для payload-dict `backend_error`. Виняток: `liquidations_history` з `l=Infinity`, див. пункт R2.
- Нові тести справді впали б, якби захист прибрали. `BAD_TS` (8 значень, зокрема межі `0` і `4_102_444_800`) параметризує `test_series_drop_bad_timestamps` для обох функцій, і поганий рядок там теж поруч із добрим. `test_bad_timestamp_rows_return_envelope` перевіряє `ok=false`/`data_unavailable` через інструмент, а `test_summarise_drops_bad_timestamps` і `test_liquidations_history_bad_timestamp_returns_envelope` роблять те саме для `liquidations_history`.
- Зауваження 2–5 закрито. В описі й `warnings` funding тепер сказано, що `open/low` можуть містити ставку попереднього бару і що середнє `close` не є базою для carry (тест `open/low may carry`). Для hourly funding є попередження «hourly bar semantics are not verified», а тест перевіряє, що на daily його нема. Docstring OI-тесту пояснює, що зразок у монетах, а в BACKLOG (в) з'явилися перевірка hourly funding і USD-зразок OI. В описі лонг-шорт є «known at t».
- Старий тест `test_summarise_edge_cases`: заміна `t=0` на `t0=2024-01-01` зберігає його мету (частка `None` при нульовому total, `gaps == 1`).

### Нові зауваження

R1. **Низьке: старий тест мовчки втратив перевірку.** `tests/test_coinalyze.py:61` `summarise([{"t": "x"}, {"t": 1, "l": -1, "s": 0}], "daily") is None`. Тепер рядок з `t=1` відкидає `parse_ts` (до 2009 року), і до перевірки `lg < 0` він не доходить. Якщо видалити `if lg < 0 or sh < 0: continue` у `summarise`, тест усе одно пройде. Інших тестів на від'ємні ліквідації немає. Виправлення в один рядок: `{"t": 1_704_067_200, "l": -1, "s": 0}`.

R2. **Низьке, стара поведінка, не блокер: `summarise` пропускає нескінченні значення.** `liquidations_history` з `l=Infinity` повертає `ok=true` з рядком `long: null, total: null`. Виняток не виникає, тож інваріант 1 не порушено, але рядок не відкинуто, як у нових серіях. Виправлення: у `summarise` брати `l`/`s` через `math.isfinite` (як `_num`) і додати `float("inf")` у тест. Можна зробити окремим кроком.

### Фінальний вердикт

**approve.** Блокер усунуто, і це підтвердили проба та тести. R1 бажано виправити в тому ж коміті, бо це один рядок тесту. R2 можна відкласти.
