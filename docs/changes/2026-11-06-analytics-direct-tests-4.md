# 2026-11-06 — прямі тести ще для 10 функцій `analytics/`

Пункт 126 [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), [BACKLOG 6.9](../BACKLOG.md).

- **Перевірка перед роботою:** `Grep` за `\bім'я\b` у `tests/` не знайшов жодної згадки 10 функцій.
- **Покрито 10** (новий `tests/test_analytics_direct_4.py`, 25 тестів, еталони пораховано вручну):
  - `flow_model`: `lag_diff` (NaN у голові, лаг ≥ довжини, порожній вхід), `standardize` (моменти з train, нульова дисперсія → sd = 1), `predict_linear` (вільний член — останній вага), `r2_oos` (1, 0, від'ємні значення, NaN при сталому y), `accuracy` (поріг 0.5 включно);
  - `funding_settlement.scale_factor` (`scaled` = години / 8, інакше 1);
  - `funding_crowding.ols_beta` (нахил не залежить від вільного члена, стала x → 0, різна довжина → `ValueError`);
  - `xs_factors.two_sided_p` (симетрія, p(0) = 1, p(1.96) ≈ 0.05);
  - `strangle_manage.tau_years` (дні / 365, після експірації 0);
  - `instruments.parse_expiry_token` (08:00 UTC, регістр, 29 лютого, некоректні токени → `None`).
- Помилок не знайдено, код `src/` не змінено.
- Лічильник у BACKLOG 6.9: ≈ 83 (93 − 10), заново не перераховано.
