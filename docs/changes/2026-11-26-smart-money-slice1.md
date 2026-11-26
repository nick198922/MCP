# 2026-11-26: «розумні гроші», перший зріз форварду

- Картка [smart-money-persistence](../ideas/IDEA-20261015-smart-money-persistence.md): розділ «Зріз 1 форварду — уточнення виконання (до перегляду результатів)», as-of 2026-11-25T00:00Z. Правила й ворота реєстрації 17.10 не послаблено.
- Код: `analytics/smart_money_forward.py`, `scripts/verify_smart_money_slice1.py` (`--stage coverage|full`), `tests/test_smart_money_forward.py`.
- [Рев'ю реєстрації](../reviews/2026-11-26-smart-money-slice1-registration.md): три раунди критика B (чотири блокери в H3, потім R1 про вирішальні дати) — усе виправлено до будь-якого наслідку, approve; реєстрацію закомічено окремо.
- Повний прогін і [звіт](../verification/2026-11-26-smart-money-forward-slice1.md): сигналів нема. H3 `not_candidate` (копіювання нових позицій топ-гаманців Polymarket, помічених до запланованого `endDate`; −0.0096 [−0.0146; −0.0045]), H2 `inconclusive` за воротами потужності, H1 / H1w — лише опис.
- [Рев'ю результатів](../reviews/2026-11-26-smart-money-slice1-results.md): раунд 1 — два текстові блокери (позиції, помічені під час матчу, не перевірялись; нулі лідерборду Hyperliquid частково штучні), раунд 2 — approve.
- База знань (`data-sources.md`, README) і `docs/BACKLOG.md` оновлені; наступні зрізи — 16.12 і 15.01.
