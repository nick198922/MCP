# 2026-11-19 · Поведінкові тести perps_open_interest

Пункт 404 постійної картки, рев'ю пункту 403, знахідка 1: досі опис був закріплений лише підрядками тексту. Код у `src/` не змінено, розбіжностей з описом не знайдено.

- Новий `tests/test_perps_open_interest.py` (8 тестів, без мережі: фейкові транспорти й FakeEx з `tests/conftest.py`). Що закріплено: (а) якщо є `openInterestValue`, `oi_usd` береться з нього, тікер не запитується і попередження немає; (б) без нього `oi_usd = openInterestAmount × last`, а біржа названа в `warnings`; якщо немає й `last`, `oi_usd` = null, біржа в `warnings` і не входить у total; (в) Deribit: `oi_base` = null, `oi_usd` береться з PERPETUAL, а не з датованого ф'ючерса; для бази поза BTC/ETH рядка немає; (г) Hyperliquid: монети, `oi_usd` = монети × mark; (д) якщо падає Hyperliquid (503) або одна ccxt-біржа, з'являється запис у `missing`, `ok` = true з рештою бірж, частки в сумі дають 1.
- Перевірка: коли формулу локально замінити на `oi_base + mark`, тест (б) падає; після повернення формули все знову проходить.
- `make lint` чисто; `tests/test_perps_open_interest.py` і `tests/test_tool_desc_audit.py` дають 19 passed.
