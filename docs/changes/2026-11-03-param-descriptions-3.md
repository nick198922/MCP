# 2026-11-03 — описи параметрів, останні 59 (інваріант 5)

Пункт 28 [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), BACKLOG 6.6 (закрито).

- **Описано 59 параметрів у 26 інструментах:**
  - історія (`tools/history.py`, 25): `iv_rank`, `funding_zscore`, `skew_percentile`, `ohlcv_history`, `range_detect`, `range_setup`, `range_setup_scan`, `market_regime`;
  - квант (`tools/quant.py`, 9): `odds_devig`, `option_calc`, `touch_probability`;
  - події (6): `events_add`, `events_list`, `event_study`; журнал (5): `journal_add`, `journal_list`, `journal_close`, `journal_stats`; позиції (4): `positions_add`, `positions_list`; правила (4): `rules_add`, `rules_list`;
  - `polymarket_search`, `polymarket_history` (3), `top_coins` (2), `liquidations_summary` (1).
- **Спільні параметри — псевдоніми `Annotated`** у `tools/history.py`: `LOOKBACK_DAYS`, `MIN_POINTS`, `RANGE_LOOKBACK`, `RANGE_MIN_BARS`, `RANGE_MAX_WIDTH`, `SWING_LOOKBACK`, `SWEEP_WICK_RATIO`, `STOP_MULT`, `STOP_MIN_OFFSET`, `MIN_RR` (межі ті самі). Через них у `range_setup` уточнено два наявні описи: `stop_min_offset_pct` (відступ за кінцем гніту, у відсотках) і `min_rr`.
- **Одиниці й застереження:** `option_calc.iv` — річна IV десятковим дробом (0.60 = 60 %), `forward` — форвард експірації, не спот (r = 0), `days` → T = дні/365; `touch_probability.spot` трактується як форвард; `skew_percentile.delta_target` — RR у десятковій IV; `lookback_bars` у live — один запит ccxt, біржа обрізає (часто 1000–1500 барів); `ohlcv_history.limit` рахує від найстарішого бару; `journal_*`/`positions_*` — `size` у контрактах/монетах, не USD.
- **Поведінка та сама:** схеми всіх 108 інструментів до (HEAD) і після збігаються, якщо прибрати поля `description`; описів параметрів було 464, стало 523 (+59); описи самих інструментів не змінено.
- **Allowlist:** було 59 параметрів у 26 інструментах, тепер **порожній**; новий тест `test_param_description_allowlist_stays_empty` не дає його поповнити.
- **Помічено, не виправлено (поза межами завдання):** `range_detect`, `range_setup` і `market_regime` з `source="store"` читають `ohlcv_range(since_ts=0, limit=2N)` (сортування за зростанням `ts`) і беруть `rows[-N:]`, тобто аналізують **найстаріші** 2N барів, а не останні. Відтворено на тимчасовій БД: 1000 барів, `limit=200` → вікно з барів 100–199. Тому в описі `lookback_bars` про «останні N» сказано лише для live. Записано в BACKLOG («Ідеї без пріоритету»).
