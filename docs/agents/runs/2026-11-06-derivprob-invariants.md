**Крок:** p, постійна картка standing-improvements: додано і виконано пункт 155 (інваріанти `derivatives.py` / `probability.py`).

**Результат:**
• `tests/test_derivatives_probability_invariants.py`: 26 тестів, 4 вироджені випадки в `DEGENERATE`.
• Знайдено й виправлено 2 помилки в `derivatives.py`: `polarity_flips` рахував зайві зміни знака через нулі; `z_score_latest` для сталого ряду давав ≈ ±1 замість None.
• `make lint` і `make test` (5294 passed) пройшли.

**Критик:** C (sonnet), approve ([рев'ю](../../reviews/2026-11-06-derivprob-invariants.md)).

**Далі:** owner. Відкрите питання Q-20261106-1 (поріг для правила 48 год) лишається без відповіді.
