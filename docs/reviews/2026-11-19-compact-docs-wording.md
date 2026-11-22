approve

# Рев'ю C: точніші описи стислих відповідей (пункт 412)

Перевірив сам: `git diff` усіх шести файлів, код `tools/system.py`, `tools/polymarket.py`, `tools/market.py`, `tools/cycle.py`; `make lint` (чисто); `uv run pytest -q tests/test_tool_descriptions_units.py tests/test_server.py tests/test_compact_defaults.py` (115 passed).

## Звірка тверджень з кодом

1. `system_health`: «нічого не втрачено, крім імен інструментів (`tool_count` замість `tools`)». Новий тест `test_system_health_compact_drops_only_the_tool_names` перевіряє поведінку: у стислому `tools` нема, `tool_count == len(full tools) == len(EXPECTED_TOOLS)`, у повному `tool_count` нема. Твердження правильне. У `PARITY.md` і README узгоджено.
2. `polymarket_search`: `count` = знайдено (до `limit`), `markets` до 10, `shown` = скільки. Тест на фейковому транспорті: `count == 20`, `shown == len(markets) == 10`, у повному `shown == count`. Правильно.
3. `market_tickers` (README): `symbols` — усі повернені, `tickers` — повні рядки лише для топ-10 за обсягом (`TICKERS_COMPACT_ROWS = 10`, сортування за `quoteVolume`), `include_details=true` — усі рядки (`market.py` 242-258). Збігається.
4. `cycle_regime` (README): у `history` останні 10 днів (`COMPACT_HISTORY_DAYS`), `history_summary` рахується з повної історії до зрізу, `transitions` без `changes`, `include_details=true` повертає все (`cycle.py` 181-195). Збігається. Значення 10 перевірено лише за повідомленням у описі та warning у коді, окремого тесту на README-рядок нема (див. зауваження 2).
5. Інваріант 5: усі нові описи MCP англійською, `Annotated`/`READ_ONLY` не чіпали. README, PARITY і `docs/changes/` українською, запис у `docs/changes/` є.
6. Логіку не змінено: у `src/` змінено лише рядки опису `polymarket_search`, рядок опису `system_health` і коментар над `PROVIDER_UNUSED`. Інших файлів поза переліком немає (плюс рядок 412 у картці).

## Зауваження (не блокують)

1. Низька. Рядок 412 у `docs/ideas/IDEA-20261102-standing-improvements.md` лишився `[ ]` без позначки «зроблено». Це робить координатор при злитті, не розробник; нагадування.
2. Низька. Тести на ключові фрази стосуються лише `system_health` і `polymarket_search`; описи `cycle_regime` і `market_tickers` у цьому пункті не змінювались (тільки README), тож розбіжності не виникає, але README-рядки захищає лише ручна звірка. Можна не виправляти.
3. Низька. У тесті `test_system_health_description_names_the_only_loss` перевірка «старої фрази» (`"compact but loses nothing:" not in d`) крихка до майбутніх переформулювань, але нешкідлива.

Блокерів нема.
