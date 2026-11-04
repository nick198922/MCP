# Рев'ю (режим C): BACKLOG, слова «Вимкнене» (пункт 62 standing-improvements)

Дата: 2026-11-04. Вердикт: **approve**.

## Що перевірено особисто

1. `deploy/features.env`: рядок 28 `TRADEDESK_COLLECT_OPTION_XVENUE_ENABLED=1`, рядок 32 `TRADEDESK_COLLECT_TRADFI_SPREAD_ENABLED=1`, рядок 64 `TRADEDESK_COLLECT_HL_OI_CAP_ENABLED=1`. Усі три колектори справді ввімкнені, слово «Вимкнене» без дати було застарілим.
2. Дати й посилання:
   - коміт `e15361f3` існує («Collector: enable TradFi perp spread job in features.env», 24.10.2026): збігається з «увімкнено 24.10»;
   - `docs/changes/2026-10-23-option-xvenue-registration2.md` (додано 23.10) існує: збігається з «увімкнено 23.10»;
   - `docs/changes/2026-11-01-hl-oi-cap-enable.md` існує; у самому BACKLOG коміт `12d6c67` і «збір з 01.11» вже були: збігається з «увімкнено 01.11»;
   - `docs/reviews/2026-10-24-tradfi-spread-collector.md`, `docs/changes/2026-11-04-backlog-stale-sweep.md` і `docs/reviews/2026-11-04-backlog-stale-sweep.md` існують. Відносні посилання (`changes/…`, `reviews/…`, `../changes/…`) правильні для розташування файлів.
3. Зміст пунктів (рядки ≈ 70, 72, 78 BACKLOG) не змінено: у `git diff` лише в кожному рядку додано пояснення в дужках після «**Вимкнене** (`…ENABLED=1`; …)». Решта тексту (стани, «Лишилось», вирази про крок 1, ранній кіл) дослівно та сама. Позначки `- [ ]` / `◐` збережено, нічого не закреслено й не перенесено.
4. Код не змінено: `git status` показує лише `docs/BACKLOG.md`, `docs/ideas/IDEA-20261102-standing-improvements.md` і новий `docs/changes/2026-11-04-backlog-disabled-sweep.md`.
5. `make lint`: ruff check і format --check проходять (2718 файлів).

## Зауваження

1. (nit, не блокує) У рядку `option_xvenue` лишилося два майже однакові повідомлення про ввімкнення: нова дужка й старе «Увімкнення — зроблено (23.10 …)» далі в рядку. Це зайве повторення, але суперечності немає, зміст не змінено, як вимагав пункт.
2. (nit, не блокує) Для `tradfi_spread` у «зміни» чесно зазначено, що окремого файлу змін на ввімкнення немає; посилання на коміт і рев'ю достатнє.

Блокерів немає.
