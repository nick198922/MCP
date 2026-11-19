# 2026-11-19 — тести perps_open_interest: збій Deribit, рядок BTC без магічного числа (пункт 404b)

- `tests/test_perps_open_interest.py::test_d`: рядок BTC у фейку Hyperliquid шукається за назвою, а не за `openInterest == "1500"`. Поля `coin` у контекстах фейку немає (як і в справжньому `metaAndAssetCtxs`): назва береться з `universe` за тим самим індексом; якщо BTC зникне з фікстури, тест впаде з повідомленням, а не з `StopIteration`.
- Новий `test_e_deribit_down` (BTC і ETH): Deribit-фейк відповідає 503 → `ok=true`, запис `deribit` у `missing`, binanceusdm / bybit / hyperliquid на місці, `total_oi_usd` дорівнює сумі решти, частки дають 1, попередження `partial:` називає deribit.
- Код `src/` не змінювався. Перевірка: `uv run pytest -q tests/test_perps_open_interest.py` — 10 passed; `make lint` чисто.
