# Рев'ю: пункт 187, сире expires_ts у plan_review (режим C)

Вердикт: **approve**

Перевірено самостійно: `git diff -- src tests`, `make lint` (чисто), `uv run pytest -q tests/test_trade_plan.py` (22 passed). Перевірено читанням усі місця, де `expires_ts` використовується в `tools/trade_plan.py`.

## Інваріанти
- Інструмент не кидає: `review_open` пропускає план з нечисловим `expires_ts` до групування за ринком, тому `int(...)` у `resolve_plan` і `max(...)` не отримують сміття. Перевірка staleness у `plan_review` теж через `usable_expires`.
- `analytics/trade_plan.py` лишається чистим (без I/O, лише розбір значення). `usable_expires` відсікає bool, str, bytes, None, NaN (ValueError) та inf (OverflowError).
- Числа для нормальних планів не змінені: гілка `exp_s` дає ті самі `int(expires_ts)*1000`. Тест підтверджує R нормального плану (`resolved_now == [2]`, `r_result` ≈ очікуваному) і що `ok` без ключа `invalid`.

## Рішення warnings замість missing
Правильне. `collect_plan_resolve` кидає `RuntimeError` при непорожньому `b.missing`, тож один зіпсований рядок валив би годинну задачу назавжди. Тест перевіряє `collect_plan_resolve(rt) == 0` без винятку. Коментар у коді пояснює причину.

## Зауваження
1. Низька. У колекторі `Builder` відкидається, тож `b.warn` там не логується: поламаний план мовчить у задачі, видно лише у виклику `plan_review`. Бажано (не блокер) `log.warning` у `review_open` або в колекторі.
2. Нітпік. `usable_expires(...) or 0` у `until_ms` недосяжний для None після фільтра, але `0` теж «хибне» значення; поведінка коректна, бо 0 → `min(now, 0)`, і так лише для плану з `expires_ts=0`, який у вибірку потрапляє легально. Зміни не потрібні.
3. Тести: `test_resolve_plan_leaves_a_raw_expires_open` покриває 5 поганих значень і нормальний контроль; інтеграційний тест перевіряє `expires_raw`, warning, відсутність впливу на сусідній план. Ламаються при регресії. Достатньо.
