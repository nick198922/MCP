# 2026-10-22 — перевірка керування стопом демо-трейдера

- Нове: `src/tradedesk/analytics/stop_management.py`. Це чистий симулятор виходу на 1h-барах: беззбиток за часткою шляху до цілі, за R або за рухом ціни (ROE на плечі); структурний трейлінг під підтвердженими фрактальними лоу таймфрейму сетапу; варіант без цілі; мейкерські вхід і ціль, тейкерські стопи; R — живий ризик демо. Також є допоміжні `max_drawdown`, `cochran_q`, `chi2_sf`. Тести: `tests/test_stop_management.py`, зокрема перевірка «без зазирання вперед» і рівність з `early_exit.simulate_managed`.
- Нове: `scripts/verify_demo_stop_management.py`. Етапи `sanity`, `count`, `run`, `report`; кеш у `~/.cache/tradedesk-agents/demo_stop_management/`; мережа не потрібна.
- Картка `docs/ideas/IDEA-20261022-demo-stop-management.md` (статус `rejected`), звіт `docs/verification/2026-10-22-demo-stop-management.md` із сирим виводом, нотатка в `docs/knowledge/risk-management.md`, рядок у `docs/knowledge/README.md`.
- Вердикт: лишити беззбиток на 50 % шляху (B). Структурний трейлінг (C) не кращий: в IS −0.006R [−0.017; +0.005]. Жодне правило не робить E[R] сетапів додатним.
- `src/tradedesk/execution/` не змінювалось.
