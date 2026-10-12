# Pre-trade checklist — `{{SYMBOL}}` `{{SIDE}}` `{{SIZE}}`

Пройди повний чеклист ПЕРЕД відкриттям позиції. На кожному кроці або
пройди (✓), або явно пропусти з причиною. Не пропускай "про всяк випадок".

## 1. Thesis one-liner
Один рядок: **чому** цей трейд, і **що станеться** якщо thesis обере інший напрямок.

## 2. Ринковий контекст
- `market_regime` на 1h / 4h / 1d — чи стратегія підходить під регіме?
- `sentiment_fear_greed(days=30)` — чи ми в екстремумі?
- `global_market()` — BTC.D, ETH.D, total MC change_24h.

## 3. Деривативна картина
- `perps_basis_compare(base)` — крос-венюз perp premium і Deribit basis.
- `perps_funding_compare(base)` — поточний funding; якщо > 20 % annualised,
  додай warning до thesis.
- `perps_open_interest(base)` — хто тримає позиції.
- `deribit_oi_by_strike` + `deribit_gamma_exposure` — поблизу яких страйків
  ми торгуємо?

## 4. Volatility
- `deribit_dvol(currency, hours_back=168)` — DVOL latest + 7d change.
- `deribit_atm_term(currency)` — shape (contango/backwardation).
- `deribit_skew_term(currency, target_delta=0.25)` — куди платять за хедж.

## 5. Календар
- `calendar_expiries(horizon_days=14)` — експірації, funding payouts,
  макро-події у вікні.
- Якщо макро-подія в межах 48 годин — ЯВНО вкажи, і подивись
  `event_study` на цю `event_kind`.

## 6. Payoff + ризик
- `strategy_payoff` (або один із шаблонів: straddle / strangle / RR /
  iron_condor / cash_and_carry / funding_carry) — breakevens,
  max profit/loss, net δ/γ/vega/θ, MTM, PoP.
- `strategy_scenario_matrix` з горизонтами [D/3, D/2, D] і grid
  [-10, -5, -2, 0, 2, 5, 10]%.
- `positions_risk` — як цей трейд змінить net greeks поточного портфеля.
- Max loss USD повинен бути ≤ максимального R, який ти собі дозволяєш
  на один трейд.

## 7. Mechanics
- `market_orderbook(exchange, symbol, notional_usd=<planned>)` — slippage
  на вхід.
- `market_trades(exchange, symbol, limit=200)` — aggressor imbalance
  останніх 200 угод (чи є проти-flow).
- Якщо спред > 10 bps — додай warning.

## 8. Exit plan
- TP1 / TP2 / runner — конкретні ціни + % від entry.
- Stop — у ціні, у % і у R.
- Time-stop (якщо є).

## 9. Alert / rules
- Чи треба створити правило через `rules_add`?
  (наприклад: `funding("binanceusdm","BTC") > 0.50 and {{SIDE}}=="long"`).
- Якщо так — додай зараз, щоб ти отримав повідомлення автоматично.

## 10. Запис у журнал
Коли все готово — виклич `journal_add` з усіма параметрами, тегами,
thesis. Отримаєш `id`, за яким можна буде закрити (`journal_close`).

---

Якщо хоч один пункт позначено як **⚠** або **✗** — не відкривай позицію
без усвідомленого downside-trade-off'у у thesis.
