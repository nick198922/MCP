# 2026-11-03 — тест-охоронець метаданих інструментів (інваріант 5)

Пункт 25 [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md).

- **Новий тест** `tests/test_tool_metadata.py` обходить усі 108 інструментів `build_server()`: анотація read-only; опис ≥ 40 символів і без кирилиці; `description` у схемі кожного параметра; якщо опис говорить про ймовірності й опціони — слово «risk-neutral».
- **Порушень до виправлення — 179:** read-only 0, порожніх/коротких описів 0, кирилиця в описі 4, без «risk-neutral» 1, параметрів без `description` 174 (у 55 інструментах).
- **Виправлено 5 (лише текст опису, поведінка та сама):** `range_detect`, `range_setup`, `range_setup_scan` (назви лекцій DoubleTop англійською), `plan_review` («замало даних» → англійською, з приміткою, що вердикт українською), `touch_probability` (додано «risk-neutral model value with r=0, not a forecast»).
- **Allowlist (174 параметри, 55 інструментів)** — `PARAM_DESCRIPTION_ALLOWLIST` у тесті. Найбільші: `strategy_iron_condor` (16), `strategy_risk_reversal` і `strategy_strangle` (по 13), `strategy_straddle` (12), `range_setup_scan` (8), `strategy_cash_and_carry` (7); далі `deribit_*`, `hyperliquid_*`, `perps_*`, `market_*`, `journal_*`, `rules_*`, `events_*` по 1–5 параметрів. Список лише скорочується: тест падає і на новий параметр без опису, і на запис, що вже має опис.
