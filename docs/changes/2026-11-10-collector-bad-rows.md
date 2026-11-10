# 2026-11-10 — зіпсовані рядки провайдера в задачах колектора (пункт 306)

Інваріант 1 `CLAUDE.md`. Кожну задачу `collector/`, що перетворює відповідь провайдера на рядки БД, прогнано з фейковим провайдером, який псує одне поле одного рядка: `None`, текст, NaN, ±inf, 1e308, 400-цифрове число, час 10**20. Перевірка: задача не кидає виняток, зіпсоване значення не доходить до БД (NaN / inf / ціле поза SQLite INTEGER), а решта рядків записана так само, як без зіпсованого. Без виправлень 100 зі 198 перших тестів падали.

- Перевірено 11 задач: `market_data` (`parse_bar` / `bars_4h`, `funding_entries`), `tradfi_spread`, `coingecko_trending`, `hl_oi_cap`, `flow_snapshot`, `option_xvenue`, `option_legs`, `coinalyze_history`, `radar_archive`, `cycle_regime_log`, розділ радара `assistant_snapshot`.
- `market_data`. `parse_bar` пропускав inf / NaN-high і кидав `OverflowError` на 400 цифрах і на часі 1e308. Тепер час має бути unix ms до 9999 року, а high / low / close у (0, 1e12]. `funding_entries` віддавав ставку 1e308 і час 10**20 у задачу `carry_paper_feed`. Тепер ставка понад ±1 відкидається. Інструменти `carry_paper_*` викликають його без межі (`max_rate=None`), щоб їхнє попередження про відкинуті ставки лишилось. Код `carry_paper_feed` / `carry_paper_engine` не змінено, але вхід фіда тепер без ставок понад ±1 і часу поза діапазоном: такі рядки відкидаються, як раніше NaN, з попередженням у лозі (`funding_entries: N of M funding row(s) dropped`). `got["complete"]` при цьому лишається True, тож двигун бачить пропущену виплату. Константа `MAX_FUNDING_RATE` тепер у `tools/_util.py` (реекспорт із `tools/perps.py`), щоб `collector/market_data.py` не імпортував модуль інструментів.
- Що кидало виняток і зривало весь запуск:
  - `tradfi_spread`: `int(inf)`, а час 10**20 давав `OverflowError` у SQLite.
  - `coingecko_trending`: `float()` від 400 цифр, а ранг 10**20 не вміщувався в SQLite.
  - `option_xvenue`: час котирування й `time` Bybit. Тепер спільні `ms()` / `num()` у `analytics/option_xvenue.py` (ціна до 1e12).
  - `option_legs`: `int(inf)` у часі котирування, 400 цифр у `_nonneg`.
  - `coinalyze_history`: `_num` на 400 цифрах. Тепер NaN / inf / |x| > 1e20 відкидаються.
  - `radar_archive`: `int(inf)` у часі принту. Тепер зіпсований принт або принт поза вікном пропускається з попередженням у лозі. Курсор пагінації — час останнього придатного принта сторінки в межах вікна (не `max()` по сторінці): інакше один правдоподібний, але хибний час (наступна доба) посеред сторінки перескакував решту вікна, і день архівувався з частини принтів. Повна сторінка, де всі придатні принти вже після вікна (біржа ігнорує `end_timestamp`), завершує пагінацію, як і раніше.
  - Розділ радара `assistant_snapshot` повністю зникав через `OverflowError` на DVOL / закриттях.
- Що записувалось у БД неправильно:
  - `hl_oi_cap` записував inf в `oracle` / `oi_usd`, а `providers/hyperliquid._f` кидав на 400 цифрах.
  - `flow_snapshot` писав лише 6 з 14 рядків, бо одне зіпсоване поле зривало весь знімок. Тепер нечислове long/short значення вважається пропуском і повторюється протягом години, а рівні книги понад 1e12 і значення понад 1e20 відкидаються.
- `cycle_regime_log` уже був коректний, тож тест там лише фіксує поведінку.
- Лічильник: окремого лічильника відкинутих рядків у `system_health → collector[]` немає, бо `JobMetrics` рахує лише запуски, а додати його можна лише зміною планувальника, що поза межами задачі. Тепер відкинутий рядок чи поле видно в лозі з кількістю (`radar_archive`, `hl_oi_cap`, `coingecko_trending`, `tradfi_spread`, `coinalyze_history`, `funding_entries`) або в `missing` (`flow_snapshot`, `option_xvenue`, `option_legs`). На чистих зразках бірж ці попередження мовчать (окремий тест).
- Не покрито:
  - Заморожені `carry_paper_feed` / `carry_paper_engine`, `assistant_paper` і `pm_gap` тут не перевірялись; перші два лише опосередковано через `bars_4h` / `funding_entries`. Знахідка в `pm_gap.scan_gaps`: `float(series[-1][4])` кидає на тексті / `None` в останньому рядку DVOL і зриває весь скан, а NaN проходить. Не виправлено, бо модуль заморожений.
  - Задачі `scheduler.py` (funding, basis, atm_term, ohlcv_hist тощо), `forecast_log` і `trade_plan` до списку пункту не входили.
- Живцем 10.11 (з цього сервера, тимчасова БД):

  | Задача | Результат |
  |---|---|
  | `hl_oi_cap` | 179 рядків |
  | `coingecko_trending` | 15 |
  | `tradfi_spread` | 204 |
  | `flow_snapshot` | 14 |
  | `cycle_regime_log` | 7 |
  | `option_xvenue` | 28 |
  | `option_legs` | 20 |
  | `radar_archive` | BTC і ETH |
  | розділ радара | `ok`, 365 закриттів |
  | `bars_4h` | 180 барів |
  | `funding_entries` | 90 з 90 |

  У таблицях жодного NaN / inf, у лозі жодного попередження, тобто на справжніх даних нічого не змінилось.
- Тести: `tests/test_collector_bad_rows.py` (200: 198 первісних, тест курсора `radar_archive` і тест «чисті зразки без попереджень»).
