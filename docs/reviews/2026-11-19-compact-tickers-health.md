# Рев'ю: стислі типові відповіді `market_tickers` і `system_health` (пункт 406)

Режим C. Вердикт: **approve** (зауваження нижче не блокують).

Перевірено сам: `git diff` для src/tests/README/PARITY; `make lint` (чисто); `pytest tests/test_compact_defaults.py tests/test_collector_rows_dropped.py tests/test_server.py` (114 passed).

## Інваріанти
- Нових мережевих викликів і виняткових шляхів немає. Збій провайдера в `market_tickers` лишається в `missing` (тест `..._compact_still_reports_a_provider_failure`), в `system_health` збій Deribit дає повний рядок `down` (тест є).
- Описи англійською, одиниці/застереження є; нові параметри `include_details` мають `Annotated[..., Field(description=...)]`, типово `false`, узгоджено з `market_markets`.
- Нового інструмента немає, `EXPECTED_TOOLS` без змін. README (рядок `system_health`), PARITY і `docs/changes/2026-11-19-compact-tickers-health.md` оновлено.

## Діагностика `system_health`
- `collector[].stream` не чіпається; `probe=true` повертає повний звіт (гілка `include_details or probe`).
- Скорочення лише там, де значення випливає з інших полів: `successes = runs - failures`; null-поля при `failures == 0`; `last_rows_dropped`/`rows_dropped_total` лише коли 0 (і `None` до першого запуску). Порівняння враховує тип (`type(...) is type(...)`), тож `0`/`False`/`0.0` не плутаються.
- Тест `_expand_health(compact) == full` відновлює повний звіт, включно з 26 задачами і потоками, а не лише перевіряє розмір. Тест змістовний.

## Зауваження
1. Низька. Опис і change-нота стверджують «без втрат», але `tools` (імена) замінено на `tool_count`; у тесті для порівняння є `_without_tools`. Це названо явно, імена доступні через `list_tools`, але формулювання «loses nothing» точніше було б «нічого, крім імен інструментів». Виправлення: уточнити фразу в описі (не обов'язково).
2. Низька. Стисла `system_health` додає `warning` «compact answer (not a fault)» при кожному звичайному виклику. Якщо якийсь споживач трактує `warnings` непорожніми як проблему, буде шум; Grep показав, що `system_health` читають лише тести, тож безпечно.
3. Інформаційно. На живій службі з колектором стисла відповідь ≈ 9.7 тис. символів (> 8 тис. цілі). Автор це відкрито зазначив і обґрунтував (решта лічильників не випливає з інших полів). Прийнятно.
4. Інформаційно. `market_tickers` з `limit=500` у compact-режимі повертає `symbols` до ~9.5 тис. символів (зазначено в ноті). Прийнятно.

Блокерів немає.
