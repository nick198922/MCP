# 2026-10-24 — перевірка «спот проти перпа: хто зробив великий денний рух»

- `src/tradedesk/providers/binance_vision.py`: новий парсер `parse_klines_taker_zip`. Він повертає ще колонки 9–10 klines (`taker_buy_base`, `taker_buy_quote`) і методи `um_klines_taker_monthly` та `spot_klines_taker_monthly`. Формат `parse_klines_zip` (7 колонок) не змінено.
- Нове: `src/tradedesk/analytics/spot_perp_flow.py` — чисті функції:
  - імбаланс агресорів;
  - ковзні SD, z і β лише за попередні дні;
  - розширювані терцилі без lookahead;
  - кластерні SE і бутстреп комбінацій групових середніх;
  - перемішування в межах дня;
  - OLS із кластерною коваріацією.
- Тести: `tests/test_spot_perp_flow.py`. Серед них реальні зразки архіву: перп із заголовком, спот без нього і спот 2025 року з мікросекундами (нова фікстура `tests/fixtures/binance_vision_BTCUSDT-1d-2025-03_spot_taker_2026-10-24.zip`). Є також перевірка, що ознаки й мітки до t не змінюються від даних після t.
- Нове: `scripts/verify_spot_perp_flow.py`. Етапи `probe`, `meta`, `universe`, `fetch`, `stage0`, `full`. Кеш лежить у `~/.cache/tradedesk-agents/spot_perp_flow/`, вивід дублюється у файл через `--out`.
- Картка `docs/ideas/IDEA-20261024-spot-vs-perp-flow-divergence.md` отримала статус `rejected`, звіт — `docs/verification/2026-10-24-spot-vs-perp-flow-divergence.md`, у `docs/knowledge/README.md` додано рядок.
- Вердикт ✗: MDE 0.64 п. п. (ворота пройдено), валова різниця терцилів +0.005 п. п. [−0.41; +0.49], нетто −0.76; плацебо p 0.83.
- `execution/` не змінювалось.
