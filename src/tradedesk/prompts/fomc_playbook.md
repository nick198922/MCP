# FOMC playbook — трейд-план на подію

Склади **структурований план** на найближче або нещодавнє рішення FOMC для активу
`{{SYMBOL}}` (default: BTC/USDT:USDT на `{{VENUE}}`). Шаблон слідує DoubleTop
FOMC-write-up patterns: event-study → ladder → scenario matrix → exit plan →
risks. Не пиши прогноз; пиши дескриптивну статистику і готові рівні.

## Контекст

- Актив: `{{SYMBOL}}` на `{{VENUE}}`, таймфрейм за замовчуванням `{{TIMEFRAME}}`.
- Дата / час FOMC події (UTC): `{{EVENT_DATE}}` `{{EVENT_TIME_UTC}}`.
- Decision price (P0) — закриття бару, що безпосередньо передує event timestamp.
- Reaction window: 60 хв до події + 240 хв після (можна переглянути).

## Структура плану

### 1. Position summary
- Пропозиція: інструмент, side, size, entry premium, breakeven, stop
  (`close above P0 + 0.5 %` на 15-хв закритті).

### 2. Event-study на минулих FOMC-днях
Виклич `event_study`:
- `event_kind='fomc'`, `measure='direction'`, `before_minutes=60`, `after_minutes=240`.
- Повтори з `measure='close_vs_t0'`, `max_up`, `max_dd`, `volatility`.
- Для кожного measure зафіксуй `n_sample`, `hit_rate` / `median` / `p25` / `p75`,
  `small_sample_flag`.
- **Якщо n < 20** — вкажи це явно у висновку і не роби сильних тверджень.

### 3. Level calculator
Якщо користувач дав `P0` і `L` (recent low після події), виклич
`fib_retracement(p0, l_point, min_drop_pct=2)` і наведи ladder 23.6 / 38.2 /
50 / 61.8 / 78.6 % з цінами і % від P0.

### 4. Scenario P&L по горизонтах
Виклич `strategy_scenario_matrix` для запропонованих ніг:
- `horizons`: `[{days: 0.25, iv: current_dvol}, {days: 1, iv: current_dvol},
  {days: expiry_days, iv: current_dvol}]`.
- `price_grid_pct`: `[-10, -5, -2, 0, 2, 5, 10]`.
- Виведи таблицею: horizon × price → MTM, і net δ/γ/vega/θ.

Для touch-probabilities окремо виклич `touch_probability` на межах P0 ± σ·√T.

### 5. Second-entry comparison
Якщо користувач вагається між 2+ контрактами, прожени кожен через
`option_calc` і зістав: price, theta/day, implied delta, break-even,
retest P&L при переналаштуванні на L − 2.5 %.

### 6. Exit ladder
- TP1 — retest `L` (ретрейс back to reaction low).
- TP2 — `L − 2.5 %`.
- Runner — `L − 3.25 %`.
- Time stop — на закритті бару `{{TIME_STOP}}` (наприклад, Fri 21:00 UTC).
- Hard stop — `close above P0 + 0.5 %` на 15-хв закритті.

### 7. Statistics (регіме-conditional)
Виклич `market_regime` для `{{SYMBOL}}` на 1h/4h/1d. Якщо regime розходиться
між TF — явно це познач і скажи, чи стратегія працювала в такому режимі
(посилайся на подивитись hit-rate з п.2 з фільтром `impact='high'`).

### 8. Rejected ideas
Якщо LLM розглядав 2+ альтернативи — коротко зафіксуй, що відкинуто і чому
(переважно більший theta/дрібна convexity).

### 9. Risks and caveats
- `n_sample` і small-sample flag (DoubleTop pattern).
- Regime change post-{{REGIME_CHANGE_DATE}} якщо релевантно.
- Vol-surface disclaimer: scenario matrix рахується з flat IV per horizon.
- Risk-neutral PoP ≠ real-world probability.

## Інструменти tradedesk-mcp для цього плану

- `events_list` (kind='fomc') — підтвердити дату події.
- `event_study` — усі 5 measures.
- `calendar_expiries` — найближчі експірації Deribit і макро-події в
  horizon-вікні.
- `deribit_vol_surface`, `deribit_atm_term`, `deribit_dvol` — поточна IV
  surface + історія DVOL.
- `strategy_payoff`, `strategy_scenario_matrix` — многоногий payoff +
  grid по горизонтах.
- `fib_retracement` — level calculator.
- `touch_probability` — ймовірність досягти L за T годин.
- `market_regime` — up/down/chop context.
- `ohlcv_history`, `market_ohlcv` — bars.

На виході користувач має отримати документ, який можна покласти у журнал
(`journal_add`) і повернутися до нього після закриття позиції.
