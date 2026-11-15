**Крок:** p, картка mcp-cleanup (Порядок 1), пункт 1. Плейбук опціонів (черга порожня), agents-mcp-access (питання про `.claude/`), x-traders (Trader_XO чекає дозавантаження, docXBT / btc_charlie — питання), profit-search п.2 (вичерпано), structural-edges 8f (чекає даних до 2028), demo-setups-depth п.6 (жоден сетап не має нових ≥ 30) — пропущено.
**Результат:**
• `strategy_straddle`, `strategy_strangle`, `strategy_risk_reversal`, `strategy_iron_condor` → `strategy_payoff(preset=…)`; інструментів 109 → 105.
• Числа ті самі: 13 випадків старий / новий збіглися (ok, data, missing, warnings).
• Таблиця «стара → нова» — `docs/changes/2026-11-15-mcp-cleanup-strategy-presets.md`.
**Критик:** C — approve; дрібні зауваження (PARITY, чекліст) виправлено, тест еталона лишається частково самопорівнянням.
**Перевірка:** make lint і make test — пройшли (10286 passed).
**Далі:** owner. `CLAUDE.md` досі каже «109 інструментів» — оновити власнику. Пункти 2–3 картки відкриті.
