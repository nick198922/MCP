# 2026-11-07 — звірка гортання фандингу Hyperliquid (пункт 195)

- `HyperliquidProvider.funding_history_range`: зіпсований рядок (час не ціле) відкидається, `complete=false`, лічильник у `evidence.args.dropped_rows`; рішення гортати далі — за сирим розміром сторінки.
- Це вже було виправлено у пункті 184; `tests/test_pagination.py` — 11 passed.
- Код не змінено; у `docs/BACKLOG.md` 6.41 закреслено застаріле зауваження.
