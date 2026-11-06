# Попередження про часткову відповідь у `perps_*` (пункт 174, BACKLOG 6.47)

- `perps_basis_compare`, `perps_funding_compare`, `perps_funding_history`, `perps_open_interest`: коли хоча б одна біржа дала дані, а `missing` непорожній, у `warnings` додається одне повідомлення `partial: <провайдери> unavailable, see missing; the cross-venue comparison is incomplete` (імена з `missing[].provider` без префікса `ccxt:`, без повторів, у порядку появи). Помічник `_warn_partial` у `src/tradedesk/tools/perps.py` використовує наявний `Builder.warn`.
- Числа, `evidence`, `missing` і `ok` не змінено; коли даних нема (`ok=false`), попередження не додається.
- `tests/test_tools_envelope_shape.py`: `KNOWN` тепер порожній (чотири записи прибрано), тест проходить без винятків.
- Тести: перевірка тексту попередження в `test_funding_compare_normalises_intervals_and_degrades`, `test_funding_history_cross_venue` (`tests/test_server.py`), `test_perps_open_interest_partial_failure`, `test_perps_basis_compare_partial_failure` (`tests/test_provider_failures.py`); відповідь без збоїв (`exchanges=["binanceusdm","bybit"]`) попередження не має. Тести на повний збій провайдерів лишились без змін.
- Розширення покриття фейками (колишня друга частина 6.47) винесено в BACKLOG 6.48.
- Перевірка: `make lint`; `uv run pytest -q tests/test_tools_envelope_shape.py tests/test_tools_never_raise.py tests/test_provider_failures.py tests/test_server.py tests/test_hl_coin_case.py` — 128 passed.
