# Покриття тесту форми Envelope: решта 9 інструментів (пункт 181, BACKLOG 6.48)

- `tests/test_tools_envelope_shape.py` тепер перевіряє форму успішної відповіді всіх 109 інструментів: `ok=true` 100 → 109, `ok=false` 9 → 0 (поріг у тесті 95 → 104). Нових провайдерів і зразків не знадобилось, `tests/conftest.py` і `src/` не змінено.
- Coinalyze ×4 (`coinalyze_oi_history`, `coinalyze_funding_history`, `coinalyze_long_short_history`, `liquidations_history`): тестовий ключ-заглушка `test-dummy-key` лише в `Settings` тесту і фейк транспорту на вже збережених зразках (`coinalyze__*_2026-10-18`, `coinalyze__liquidation_history_…_2026-10-17` через `test_coinalyze_history.py` / `test_coinalyze.py`).
- `polymarket_options_gap`: Gamma `/events/keyset` і CLOB `/books` віддають події й книги з `test_pm_gap.py` (решта шляхів — як і раніше, фейк `conftest`); аргументи `max_days=30`, `min_net_edge=-1` (події закінчуються 29.10, `NOW` тестів — 10.10).
- Засів БД: подія FOMC усередині 200 годинних свічок (`event_study`), відкрита опціонна позиція з греками (`positions_risk`), відкрита paper-пара binanceusdm/bybit (`carry_paper_close`, закривається за фейковими цінами, 8 `evidence`), 54 свічки 4h BTC зі sweep верхньої межі (`range_setup` з `source="store"`). `event_study`, `positions_risk`, `range_setup` додано до `NO_NETWORK` (42 → 45).
- Через засів змінено два аргументи: `journal_close` і `positions_close` беруть `id=2` (рядок 1 журналу пише `carry_paper_close` уже закритим, позиція 1 — засіяна для `positions_risk`).
- Порушень форми серед 9 нових інструментів не знайдено, `KNOWN` порожній. BACKLOG 6.48 закрито.
- Перевірка: `make lint`; `uv run pytest -q tests/test_tools_envelope_shape.py` — 4 passed.
