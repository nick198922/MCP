# 2026-11-03 — `deribit_largest_trades`: номінал ф'ючерсних угод

Пункт 30 [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), BACKLOG «Ідеї без пріоритету» (закрито).

- **Виправлено:** з `kind="future"` інструмент рахував `notional_usd = amount × price`, а в інверсних ф'ючерсах і перпі Deribit (BTC, ETH) `amount` уже в USD. Номінал був завищений у ціну монети разів: угода на 5000 контрактів BTC-PERPETUAL ($50 000) показувалась як ≈ $3.9 млрд. Тепер для ф'ючерсів `notional_usd = amount`. Для опціонів формула та сама: `amount × price × index_price`.
- **Опис:** в описі інструмента й у docstring провайдера `last_trades_by_currency` вказано одиниці: для опціонів `amount` і `price` в монеті, для ф'ючерсів `amount` у USD, `price` в USD за монету.
- **Зразок і тести:** чотири реальні угоди BTC-ф'ючерсів (2 датовані, 2 перпа) — у `tests/fixtures/deribit_futures_trades_2026-11-03.json`. Фейковий транспорт Deribit (`tests/conftest.py`) віддає його для `kind=future`. Новий тест `test_deribit_largest_trades_future_amount_is_usd` перевіряє `notional_usd == amount`, buy 50 040 / sell 32 510 USD і фільтр `min_notional_usd`. До тесту збою провайдера додано випадок `kind="future"`.
- **Жива перевірка (03.11, з сервера):** у картці її доручали власнику (`curl`), але сервер бачить Deribit, тож перевірку зроблено звідси через провайдер проєкту. На 1000 останніх угодах BTC і ETH усі інструменти інверсні; `amount / contracts` = 10 USD для BTC і 1 USD для ETH, лінійних USDC-контрактів у видачі немає. Виклик через `build_server()`: BTC-ф'ючерси — 1000 угод на ≈ $4.6 млн (buy 2.25 млн / sell 2.35 млн), найбільша угода $32 510; опціони без змін (найбільша — $149 315). Повторювати `curl` власнику не потрібно.
- `execution/` не змінювався.
