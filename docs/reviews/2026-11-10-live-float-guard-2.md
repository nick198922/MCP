# Рев'ю: live-float-guard-2 (пункт 305, захист від зіпсованих рядків провайдерів)

Режим C, незакомічений diff + `docs/changes/2026-11-10-live-float-guard-2.md` + `tests/test_tool_failure_extra_14.py`.

## Вердикт: approve

Блокерів немає. Нижче дрібні зауваження (не блокують).

## Що перевірено особисто
- `make lint`: чисто (ruff check + format).
- `tests/test_tool_failure_extra_14.py`: 242 passed (146 с).
- Тести зачеплених інструментів (`-k ohlcv/orderbook/market_trades/hyperliquid_candles/markup/backtest/carry_paper/funding_history/cycle/trades/...`): 780 passed, 1 skipped. Повний набір не запускав (за завданням).
- Прочитано весь diff по `_util.py`, `analytics/trades.py`, `analytics/cycle_regime.py`, `market.py`, `markup.py`, `history.py`, `perps.py`, `options.py`, `backtest.py`, `carry_paper.py`, `cycle.py`.

## Інваріанти
1. Інструменти не кидають: `bar_row` перевіряє тип/діапазон до арифметики, `float()` великих цілих ловить `OverflowError` (`hl_bar_row`, `trades._f`), `isfinite()` не викликається на 400-цифровому int (діапазонна перевірка ts). Ок.
3. `analytics/` лишилась чистою (лише `math`, константи; без I/O). Ок.
4. Одиниці не змінені. Ок.
5. Нових інструментів немає; описи `market_ohlcv` і `hyperliquid_candles` доповнено згадкою про відкинуті свічки. Запис у `docs/changes/` є.
- Поведінка для валідних даних: ліміти (ціна 0<p<=1e12, обсяг<=1e20, ts<9999 р., funding ±1 за інтервал, `MAX_CLOSE` 1e12) далеко за межами реальних значень (SHIB ~1e-5, BTC ~1e5, funding-капи бірж <=4 %). Легітимні дані не відкидаються. Наявні тести цих інструментів проходять без змін.
- Пропущені рядки: у всіх зачеплених місцях є `b.warn` з кількістю (ohlcv, orderbook, trades, markup, history, backtest, options, perps spread, HL candles/trades, cycle, carry_paper). Усі-погані: `ok=false` (`market_ohlcv`, `hyperliquid_candles`, orderbook «no valid level on one side», trades `count==0`, `malformed_bars` у скані з виключенням з `any_fetched`).

## Зауваження (мінімальна серйозність, не блокери)
1. Низька. `bar_row` вимагає ціну строго > 0 для open/high/low/close. Свічка з нульовою ціною (деякі біржі віддають 0 для неторгованих інтервалів на неліквідних парах) тепер відкидається з попередженням, раніше проходила. Для крипти прийнятно, але варто мати на увазі для нових інструментів, що працюватимуть з ціноподібними рядами, які можуть бути <= 0 (спреди, ставки): для них `bar_row` не підходить.
2. Низька. `hl_bar_row` вимагає поле `v`; раніше скан спредів (`perps` range_7d) потребував лише `h`/`l`. Реальні свічки HL завжди містять `v`, тож практичного впливу немає, але це тиха зміна контракту.
3. Низька. У `market_trades` `with_side` рахує будь-яку непорожню `side` (включно з невалідною на кшталт "foo"), тож попередження «price or amount is not a finite number» може бути неточним щодо причини. Лише формулювання.
4. Низька. Файл `tests/test_tool_failure_extra_14.py` виконується ~146 с на 242 тести без xdist; для нічного конвеєра краще скоротити найповільніші параметризації (пункт 304 про повільні тести вже в роботі).

## Тести
Параметризовані за NaN/inf/1e308/str/None/великі int, перевіряють відсутність винятку, `ok`, наявність warning і відсутність NaN/Infinity у відповіді; проходять і падали б при поверненні старого коду (у коментарях до коду описано, які саме виклики раніше кидали виняток).
