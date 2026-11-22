# Рев'ю: аудит описів трьох інструментів (19.11)

Режим C. Вердикт: **approve**.

## Що перевірено самостійно
1. `git diff -- src`: змінено лише рядки `description` у `tools/history.py`, `tools/perps.py`, `tools/positions.py`. Сигнатур і логіки не змінено. Описи англійською.
2. `make lint`: чисто. `pytest tests/test_tool_desc_audit.py tests/test_server.py tests/test_tool_metadata.py`: 95 passed.
3. `perps_open_interest`: код бере `oi.get("openInterestAmount")` як є, без перерахунку. Оцінка `oi_base * last` і `warn` збігаються з описом. Твердження про okx (контракти) спирається на живий виклик із change-файлу. Із коду це повністю не підтверджується (ccxt `parse_open_interest` для okx не перевіряв). Формулювання обережне й безпечне.
4. `ohlcv_history`: у ccxt `okx.parse_ohlcv` для swap береться індекс 6, тобто базовий обсяг, а не контракти. Це збігається з описом. Для binance і bybit не перевіряв, але вони дають базовий обсяг за звичайною схемою.
5. `positions_list`:
   - `instrument` це `instrument = ?`, тобто точний збіг. Так.
   - `tag` це `tags LIKE '%"tag"%'`, тобто цілий тег. LIKE у SQLite нечутливий до регістру лише для ASCII. Так.
   - Сортування `ORDER BY opened_ts DESC`. Так.
   - `scenario_pnl` = `d*dSpot + 0.5*g*dSpot²`, тож delta у монетах, gamma = зміна delta на $1. Нове формулювання правильне, старе («coin per $^2») було хибним.

## Зауваження
1. (низька) Docstring `analytics/portfolio.py` (рядок 10), опис `positions_add` і опис `market_ohlcv` досі містять стару або нечітку одиницю. Це вже названо в change-файлі як поза обсягом. Варто закрити окремим пунктом, бо тепер інструменти суперечать один одному.
2. (середня, не блокує) Підозра на баг логіки в `positions_risk`: `d_vol_points` = ±0.05 за замовчуванням при описі «±5 vol-points». Окремий пункт, як запропоновано.
3. (низька) Твердження про okx для OI залежить від живого виклику, тест на нього не додано. Для опису тексту це прийнятно.
