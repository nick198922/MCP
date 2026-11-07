# Рев'ю: лишки рев'ю, друга партія (пункт 256)

**Вердикт: approve.** Блокерів немає, є дві дрібні зауваги.

## Що перевірено самостійно
- `make lint` проходить.
- Запущено 6 файлів тестів: `test_review_leftovers_2`, `test_demo_pnl_check`, `test_backtest`, `test_review_leftovers_nonfinite`, `test_tools_wrong_type_fields`, `test_vrp`. Результат: 257 passed. Весь набір не запускався.
- Перейменування `_normalise_bars` на `normalise_bars`: Grep по репозиторію не знайшов старого імені в `src/`, `tests/` і `scripts/`. Згадки лишилися тільки в історичних документах (старі рев'ю, `docs/changes/2026-11-06-...`), їх правити не треба. Усі три місця виклику (`signals_momentum`, `run_backtest`, `tools/backtest.py`) оновлені.
- Інваріанти CLAUDE.md. Інструменти не кидають виняток: нові гілки роблять лише `b.warn` і `b.miss` з `b.build(None, ok=False)`. `analytics/` лишається без I/O, `demo_pnl_check` чистий. Одиниці не змінені.

## Поведінка на чистих даних
- `demo_pnl_check`: `_checked` повертає ті самі умови, що й попередній `_checkable` (статус `closed`, наявні `pnl_usd` і `expected`). Notional лишається `abs(entry*qty)`. `pnl_mismatches` і `unchecked` рахують так само, як раніше.
- Бектест: на чистих даних `usable == len(bars)`, поріг той самий, `bars_used` не змінився. Тест перевіряє `(100, 98)` для чистих і зіпсованих барів.
- Funding: `_funding_entries` на чистих рядках дає ті самі записи. Нове лише попередження, коли відкинуто рядок.

## Чи падають тести без виправлення (за читанням коду, без відкату)
- Мінімум барів і попередження про зіпсовані бари: на старому коді 60 сирих барів проходили перевірку довжини, тож `ok` був би `True`, а не `False`. Тест падає.
- Попередження про відкинуті рядки funding (10 параметризацій): на старому коді таких попереджень немає. Тест падає.
- `deribit_vrp`: код уже попереджав про `continuation` не того типу, тож ці тести пройшли б і без змін. Це регресійні тести, а не відтворення помилки. Автор про це пише в докстрингу й у change-файлі, тож чесно.

## Зауваги (не блокують)
1. **Низька, зміна поведінки.** У `hyperliquid_funding_history` раніше `time == 0` відкидався б лише за відсутності ключа. Тепер `_funding_entries` фільтрує за `x.get(ts_key)` (falsy), тож рядок з `time: 0` пропускається мовчки. Решта шляхів так і робила, а `time = 0` (1970) нереалістичний. Достатньо одного речення в change-файлі, що поведінку вирівняно.
2. **Низька.** `normalise_bars(bars)` у `tools/backtest.py` викликається тричі на одному наборі (у самому інструменті, в `signals_momentum` і в `run_backtest`). Для сотень барів це дрібниця, оптимізація не потрібна.

Файли: `/home/tradedesk/tradedesk-agents/src/tradedesk/tools/backtest.py`, `/home/tradedesk/tradedesk-agents/src/tradedesk/tools/perps.py`, `/home/tradedesk/tradedesk-agents/src/tradedesk/analytics/backtest.py`, `/home/tradedesk/tradedesk-agents/src/tradedesk/analytics/demo_pnl_check.py`, `/home/tradedesk/tradedesk-agents/tests/test_review_leftovers_2.py`.
