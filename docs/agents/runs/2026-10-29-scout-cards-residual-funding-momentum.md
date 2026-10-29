# Скаут: картки residual-momentum і funding-momentum (29.10)

**Крок:** d — workable-карток (proposed/accepted) було менше двох (решта — forward/rejected/заблоковані), тож скаут написав 2 картки, критик (mode A) їх відтріажив. Кроки p/a0/a не мали розблокованої роботи; сито (s) пропущено — одна з останніх трьох партій уже `sieve-batch`.

**Результат:**
- `residual-momentum-short-leg` — **accepted** з умовами: ворота G1–G3 і кореляція з M28 < 0.85 до перегляду доходів; торговий висновок не з IC (урок MAX30); один прогін. MDE(IC) ≈ 0.024 проти порога 0.03 — запас малий; ймовірність 3–4 %.
- `funding-momentum-cross-section` — **rejected**: зміна funding механічно слідує за ціною, рівень funding уже дав IC ≈ 0, двобічна H1, ймовірність ≈ 3 %.

**Вердикт критика:** тріаж завершено, рев'ю `docs/reviews/2026-10-29-scout-cards-residual-funding-momentum-triage.md`.
**Доставка:** `owner`. `make lint` і `make test` пройшли (3484 passed, 2 skipped).
**Далі:** наступний крок кванта — `residual-momentum-short-leg`. Нових питань до власника немає.
