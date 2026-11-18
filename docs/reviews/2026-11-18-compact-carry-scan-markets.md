# Рев'ю: стислі типові відповіді `funding_carry_scan` і `market_markets` (пункт 382)

Режим C, незакомічена зміна: `src/tradedesk/tools/perps.py`, `src/tradedesk/tools/market.py`, `tests/test_compact_defaults.py`, `docs/changes/2026-11-18-compact-carry-scan-markets.md`.

**Вердикт: approve.** Блокерів немає.

## Що перевірив сам
- `make lint`: чисто (ruff check і format).
- `uv run pytest -q tests/test_compact_defaults.py`: 8 passed.
- Суміжні набори (test_server, test_funding_carry_incomplete, test_current_funding_nonfinite, test_review_leftovers_2 і _nonfinite, test_tools_never_raise, test_provider_failures, test_tool_failure_extra_14, test_tools_wrong_type_fields): 552 passed.
- Споживачі (Grep поза docs/): `scripts/live_smoke.py` викликає `market_markets` з `limit` 10 (повні рядки й так); решта згадок — лише тести. Ні `src/`, ні скрипти, ні панель обидва інструменти не читають. Нічого не ламається.
- Інваріанти: інструменти не кидають (збій провайдера лишається в `missing`, є тести для обох); мережа лише через провайдери; `analytics/` не змінено; одиниці не змінено; описи англійською, нові параметри з `Annotated[..., Field(description=...)]`; `READ_ONLY` лишився.
- Однаковість чисел: `summary` у carry тепер обчислюється ДО обрізання `rows`, тобто покриває всі скановані символи в обох режимах; тест порівнює всі поля `data`, окрім `rows`, і `rows[:10]` з повними рядками. Для markets `total_markets`, `matched` і попередження про `limit` однакові; `symbols` у порядку повних рядків.
- Evidence: `_merge_evidence` (існуючий помічник) зберігає кількість запитів (`fetches`, `symbols`); тест це перевіряє.

## Зауваження (не блокують)
1. Низька. `funding_carry_scan`: попередження «compact answer: top N of M rows …» додається завжди в компактному режимі, навіть коли рядків ≤ 10 і нічого не обрізано (`min(len(rows), 10)` of `len(rows)`). У `market_markets` це зроблено правильно (лише коли > 10). Виправлення: емітити попередження лише при `len(rows) > CARRY_COMPACT_ROWS`; evidence зливати завжди.
2. Низька. README (рядок `funding_carry_scan`) не згадує `include_details`; опис інструмента в коді згадує. За бажання додати одне речення.
3. Інформаційно. У компактному `market_markets` рядки з 11-го до `limit` мають лише символ (без type/expiry/contractSize): це задокументовано в описі, попередженні й changes-файлі; клієнт, якому потрібні поля, викликає з `include_details=true` або звужує `query`.
4. Інформаційно. Розміри «на живому виклику» (14 198 → 5 659; 19 074 → 4 339) я не відтворював: мережі бірж із цього середовища немає. Розміри на фейках перевіряються твердженнями в тестах (≤ 8 000 у типовому режимі, > 8 000 у повному), вони проходять.

## Відповідність changes-файлу коду
Усі твердження (параметр `include_details` типово false, top 10, злите evidence, `symbols` + 10 повних рядків, без `symbols` у повному режимі, `live_smoke` з limit 10, наявні тести не змінено) збігаються з diff. Оновлення `docs/PARITY.md` присутнє в робочій копії.
