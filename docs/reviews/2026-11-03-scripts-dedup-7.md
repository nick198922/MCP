# Рев'ю: scripts dedup, сьоме коло (пункт 38 постійної картки)

Режим C. Вердикт: **approve**.

Перевірено самостійно:

1. Видалені копії ідентичні. У HEAD `ymd` у всіх 10 скриптах — `datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d")` (де-не-де з `tz=`). Нова `cmn.ymd_ms` робить те саме з `timezone.utc`. `rss_mb` (8 скриптів) збігається з `common_helpers.rss_mb` рядок у рядок. `rd`/`wr` у `verify_perp_launch_existing` збігаються з `read_json`/`write_json` (compact separators, `.tmp` + rename, `mkdir`).
2. Імпортів видалених імен деінде немає. Grep по `verify_*.(ymd|rss_mb|rd|wr)` порожній. `verify_dvol_filtered_straddle` імпортує `verify_weekly_straddle as vws`, але до `vws.ymd` не звертається.
3. Вивід не змінюється: тільки заміна викликів, плюс кілька рядків, які ruff format переніс (`verify_event_straddle`, `verify_vrp_real_marks`, `verify_weekly_straddle`). Тестом `test_ymd_ms_matches_the_removed_copies` порівняно зі старою формулою на межових значеннях, включно з дробовими мілісекундами.
4. Тести покривають хелпер (`test_ymd_ms`, межа доби, float, від'ємне значення). `test_scripts_use_the_shared_ymd_ms` перевіряє, що локальних копій немає. `tests/test_common_helpers.py`: 41 passed. `make lint`: чисто.
5. Заборонені зони не зачеплені: у diff лише `scripts/common_helpers.py`, 10 `scripts/verify_*.py`, тест, `docs/` (BACKLOG, HANDOFF, картка, change-нотатка). `execution/`, колектори, заморожені форварди, `scripts/server/`, `agents_*`, `nightly_agents.sh` не змінені.
6. Цінність невелика (менше дублювання, ~80 рядків мінус), але ризику немає: це вже скрипти-верифікатори з готовими звітами.

Зауваження (low): не перевіряв повторним запуском самих verify-скриптів (потрібен кеш, багато хвилин). Еквівалентність перевірена порівнянням коду і тестом хелпера.
