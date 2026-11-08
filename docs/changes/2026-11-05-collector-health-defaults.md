# 2026-11-05 · Здоров'я колекторів бачить типові джоби

Пункт 80 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md), BACKLOG 6.13 (б).

- `analytics/collector_health.Spec` має поле `default_on`: джоба, яку колектор запускає без рядка у `deploy/features.env`, вважається ввімкненою, поки її перемикач там не має значення, відмінного від «1» (як читає `config.Settings`). `/etc/tradedesk-mcp.env` скрипт не читає.
- Нові серії: `atm_term` і `basis` (BTC/ETH; година з ≥ 1 знімком, джоба кожні 300 с), `funding` (за біржею: binanceusdm, bybit, okx, hyperliquid, deribit; година), `supply` (день), `traders` (`trader_snapshot` за `venue`: hyperliquid, polymarket, polymarket_crypto; день), `trader_positions` (hyperliquid, polymarket; година з ≥ 1 зміною позиції, бо це журнал змін).
- Живцем 05.11 01:27 UTC (`scripts/collector_health.py`, БД `mode=ro`): 38 серій за 2.3 с. Нові серії ≥ 99 %, крім Polymarket: `traders:polymarket` і `traders:polymarket_crypto` 66 %, `trader_positions:polymarket` 69 %, останній запис 02.11. Тепер ця дірка потрапляє в ранковий бриф і на панель «Система».
- Тести: типові джоби без перемикача, дірка в одному `venue` (знімки й позиції), порожня БД; групи `funding` звіряються з `PERP_EXCHANGES`. Тести панелі тепер явно вимикають типові джоби, щоб перевіряти лише `pm_gap`.
