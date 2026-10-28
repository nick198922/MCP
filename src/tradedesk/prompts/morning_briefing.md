# Morning briefing — крипто-дериватив-деск

Склади коротку (≤ 1 сторінки) ранкову зведення. Жодних прогнозів,
тільки факти + одна лінія контексту на кожному блоці.

## 1. Price / regime (3-4 рядки)
- `coin_snapshot('bitcoin')` і `coin_snapshot('ethereum')` — ціна,
  24h %, 7d %, ATH distance.
- `global_market()` — total MC change_24h, BTC.D, ETH.D.
- `market_regime` для BTC на 4h / 1d — одним словом (up/down/chop +
  strength).
- `cycle_regime('BTC', history_days=0)` — один рядок «Режим циклу» (поле
  `line_ua`) з позначкою «контекст, не сигнал».

## 2. Sentiment / funding
- `sentiment_fear_greed(days=7)` — latest + zone + change_7d.
- `perps_funding_compare('BTC')`, `perps_funding_compare('ETH')` —
  найвищий і найнижчий annualised, різниця (crypto-carry).

## 3. Волатильність і skew
- `deribit_dvol('BTC', hours_back=24)` + `deribit_dvol('ETH', hours_back=24)` —
  DVOL latest + change_24h.
- `deribit_atm_term('BTC')` — shape (contango/back).
- `deribit_skew_term('BTC', target_delta=0.25)` — чи зміщення у puts
  (downside skew) чи вирівняно.

## 4. Basis / crypto carry
- `perps_basis_compare('BTC')` — крос-венюз perp premium (bps) +
  найближчий dated-futures annualised.
- Один рядок: чи є cash-and-carry > 5 % річних.

## 5. Flow
- `deribit_largest_trades('BTC', count=200, only_blocks=True, top_n=3)` —
  3 найбільші block prints за ніч.
- `liquidations_summary(window_minutes=720, venue='binanceusdm')` —
  скільки ліквідацій, imbalance.

## 6. Календар на 24 години
- `calendar_expiries(horizon_days=1, funding_hours_ahead=24)` —
  найближчі експірації Deribit, funding payouts, макро-події.
- Якщо є HIGH-impact macro в межах 24h — у top-рядку зведення
  позначити ⚠.

## 7. Alerts і правила
- `alerts_recent(limit=10)` — що зачепило за ніч.
- `rules_list(enabled_only=true)` — скільки активних правил і яких.

## 8. Позиції й ризик
- `positions_list(status='open')` — скільки відкрито.
- `positions_risk()` — net δ/γ/vega/θ і стрес ±10 %.

## 9. Journal summary
- `journal_stats()` — win-rate, expectancy, avg R за усю історію
  (одним рядком).

---

Формат виведення: markdown, ≤ 15 рядків на всі секції + optional
"⚠ сьогодні звернути увагу" ТОП-рядок, якщо є HIGH-impact подія чи
funding > 30 % anneualised.
