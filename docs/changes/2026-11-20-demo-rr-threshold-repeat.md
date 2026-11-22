# 2026-11-20 — повтор перевірки порогу демо-guard R:R < 1.5: прогін і вердикт

BACKLOG 0.5, демо-трейдер (а), підпункт «повторити не раніше ~20.11».

- Звіт `docs/verification/2026-11-20-demo-rr-threshold-repeat.md`: реєстрація (комміт окремо) + повний прогін. Вердикт
  ◯ за зареєстрованим правилом: H1 +0.067 R [−0.046; +0.181] (618 планів, 31 день), H2 −0.018 R, MDE 0.156 R; нові
  дані після 29.10 +0.110 R [+0.003; +0.217]. «Ефекту ≥ 0.2 R немає» не твердиться (на нових даних верхня межа +0.217).
- `analytics/demo_rr_threshold.py`: `week_key` (тижневий індекс UTC), `wild_cluster` (ваги Вебба, WCR p / WCU CI),
  `decision` (правило реєстрації).
- `scripts/verify_demo_rr_threshold.py`: `--smoke`, `--new-after-ms`, розділи «Robustness», «New data only»,
  «Pre-registered decision».
- Тести `tests/test_demo_rr_threshold.py`; код `execution/` не змінювався, лише читання JSON демо-звірки.
- Рецензія критика B: `docs/reviews/2026-11-20-demo-rr-threshold-repeat.md` (зауваження у звіті виправлено).
