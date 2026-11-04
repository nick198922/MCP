# 2026-11-04 — виснаження продавців BTC як вето шортів: реєстрація й етап 0

- Пункт картки [IDEA-20261030-demo-setups-depth](../ideas/IDEA-20261030-demo-setups-depth.md) «виснаження продавців BTC (Seller Exhaustion Constant) забороняє шорти» (джерело [LIB-20261104-seller-exhaustion](../library/LIB-20261104-seller-exhaustion.md)).
- Нове: `src/tradedesk/analytics/seller_exhaustion.py` (чисті функції SEC і масок вето), тести `tests/test_seller_exhaustion.py`, раннер `scripts/verify_seller_exhaustion.py` (гарнес `verify_regime_side.py` без змін; дані Bitview з `~/private/data/bitview/`, без мережі).
- Реєстрація — розділ «Попередня реєстрація — виснаження продавців забороняє шорти» в картці; етап 0: MDE₈₀ 0.306R > 0.3 → вердикт ◯ за реєстрацією, прогін `report` — описовий, наступним кроком після коміту реєстрації.
- Демо, `execution/`, колектор не змінено.
