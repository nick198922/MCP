# Рев'ю (режим C): positions_risk, одиниці vol (пункт 387)

Вердикт: **approve**

## Перевірено сам
- Висновок про одиниці правильний. `analytics/options.py:62` рахує `vega = F·pdf·√T / 100`, тобто USD на 1 пункт вола. `scenario_pnl` (`analytics/portfolio.py:110`) множить `v * d_vol_points` без масштабування. Тому `d_vol_points` має бути в процентних пунктах IV. Старі ±0.05 давали 0.05 пункту, а новий ±5.0 дає заявлені 5. Це був справжній баг.
- Інших місць зі старими 0.05 для `d_vol_points` у `src/` немає. Рядки з `0.05` у решті файлів стосуються інших полів.
- Інваріанти CLAUDE.md. Описи й `Field` англійською з одиницями. Логіка поза константою `DEFAULT_SCENARIOS` не змінена, тож «інструмент не кидає виняток» не зачеплено. Мережі й нових інструментів немає, тому `EXPECTED_TOOLS` лишається як був.
- `make lint` чистий. `pytest tests/test_positions.py tests/test_tool_desc_audit.py`: 18 passed.
- Тести змістовні. Один перевіряє PnL у дефолтних сценаріях як `size·vega·5` і падав би на старих значеннях. Другий перевіряє користувацький сценарій із delta, vega, theta, days і `reference_spot` проти ручного розрахунку.
- Зайвих змін немає. У diff лише README, PARITY, картка (пункт 387 закрито), `portfolio.py`, `market.py`, `positions.py`, тести та файл `docs/changes/…` (untracked, його треба додати в коміт).

## Зауваження (не блокують)
1. Низька: опис `market_ohlcv` стверджує, що ccxt `parse_ohlcv` бере колонку base-volume на binance, bybit і okx. Я це не перевіряв за кодом ccxt. Формулювання обережне («another venue may report contracts or quote volume»), тому шкоди немає.
2. Низька: зміна значень за замовчуванням змінює вивід `positions_risk` для наявних викликачів. Це виправлення, і в changelog це є.

## Блокери
Немає.
