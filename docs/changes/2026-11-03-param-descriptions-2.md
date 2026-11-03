# 2026-11-03 — описи параметрів options / perps / market (інваріант 5)

Пункт 27 [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), BACKLOG 6.6.

- **Описано 46 параметрів у 22 інструментах:**
  - options (`tools/options.py`, 15): `deribit_vol_surface`, `deribit_option_chain`, `deribit_expected_move`, `deribit_oi_by_strike`, `deribit_atm_term`, `deribit_skew_term`, `deribit_largest_trades`, `prob_from_options`;
  - perps (`tools/perps.py`, 23): `perps_funding_compare`, `perps_funding_history`, `perps_long_short_ratio`, `perps_oi_history`, `hyperliquid_overview`, `hyperliquid_candles`, `hyperliquid_book`, `hyperliquid_trades`, `hyperliquid_funding_history`, `deribit_futures_curve`;
  - market (`tools/market.py`, 8): `market_ohlcv`, `market_orderbook`, `market_markets`, `market_trades`.
- **Спільні параметри — псевдоніми `Annotated` на рівні модуля** (як `CUR`/`EXPIRY`): `MIN_DAYS`/`MAX_DAYS` (options), `HL_COIN`, `PERIOD`, `SERIES_LIMIT`, `INCLUDE_SERIES`, `INCLUDE_HL` (perps).
- **Одиниці й застереження в текстах:** `width_pct` — межа |ln(K/F)| у відсотках (20 ≈ ±20 % від форварда); OI у контрактах (BTC/ETH); `prob_from_options.kind` — ризик-нейтральні ймовірності, не прогноз; `hyperliquid_overview.sort_by` — funding простий річний десятковим дробом (0.10 = 10 %), зміна за 24 год у відсотках; `include_series` — ставка funding за інтервал десятковим дробом; `days_back` — OKX тримає ~3 місяці; `limit`/`depth` у market — один запит, біржа може повернути менше; `HL_COIN` — регістр не важливий, builder-перпи як `dex:NAME`.
- **Поведінка та сама:** схеми всіх 108 інструментів до (HEAD) і після збігаються, якщо прибрати поля `description` (скрипт через `build_server()`; нових описів рівно 46).
- **Allowlist:** було 105 параметрів у 48 інструментах, лишилось **59 у 26**.
- **Помічено, не виправлено (поза межами завдання):** `deribit_largest_trades` з `kind="future"` рахує `notional_usd = amount × price`, а за документацією Deribit API для інверсних ф'ючерсів і перпа `amount` уже в USD, тож номінал, імовірно, завищено приблизно в ціну BTC/ETH разів (на живих даних не перевіряв, фікстури з ф'ючерсними угодами немає). Опис параметра формулу не обіцяє; виправлення — окремим пунктом.
