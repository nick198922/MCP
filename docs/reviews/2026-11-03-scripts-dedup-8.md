# Рев'ю: scripts dedup, восьме коло (пункт 39 standing-improvements)

Режим C, незакомічений diff. Перевірено самостійно.

## Що перевірив
- Тіла видалених функцій порівняв з `git diff` (HEAD) і зі спільними `common_helpers.read_json` / `write_json` / `ymd_day` / `ymd_ms`. Ідентичні: `rd`, `wr` (compact separators, mkdir, `.tmp`+replace) у `premarket_fade`, `premarket_inventory`, `premium_spike`; `day(day)` у `premium_spike`; `ymd`, `day`, `_day`, `day_str`, `day_of` (мс/1000, UTC, `%Y-%m-%d`) в інших. `UTC` проти `timezone.utc` — те саме.
- `verify_usde_funding.wr` справді відрізняється (`json.dumps` без separators), тож лишено правильно. `verify_dated_basis.write_json` не чіпали.
- Посилання на видалені імена: у 10 файлах не лишилось жодного виклику `ymd/rd/day/day_str/day_of/_day` (Grep). `wr` у `premium_spike` в `asyncio.to_thread` замінено коректно.
- Зовнішні споживачі: `verify_put_skew` імпортують `load_snapshots`, `STRESS`, `_ms`, `LAST_EXPIRY_MS`, `ENTRY_START_MS`, `COSTS`, `make_books`, `SPLIT_MS`; `verify_premarket_inventory` — лише `flags_by_base`. Видалених імен ніхто не бере.
- `make lint`: чисто (невикористаних імпортів нема; `import json` прибрано там, де треба).
- Усі 10 модулів імпортуються під `python -I` з `scripts/` у `sys.path`.
- Заморожені форварди, `execution/`, колектори, `scripts/server/`, `agents_*` не змінені (`git status`: лише 10 verify_*, 3 доки, новий changes).
- Вивід не змінюється: лише перенесення форматування (дві довгі рядки розбито ruff-ом, зміст той самий).

## Зауваження
1. (низька) Лічильник «17 копій» у `docs/changes/2026-11-03-scripts-dedup-8.md` і BACKLOG 6.4 неточний: у diff видалено 15 визначень (btc 1, course 1, dated 1, fade 2, inventory 2, spike 3, put_skew 1, s3 1, usde 2, weekend 1). Виправити число на 15 в обох місцях.
2. (інформаційна) Тесту на скрипти немає, але нового хелпера не додано, а проміжні перевірки на 105 зразках у змінах — лише твердження автора; я підтвердив еквівалентність читанням тіл.

## Вердикт
request changes (лише виправити число 17 → 15 у changes і BACKLOG 6.4; код — approve).
