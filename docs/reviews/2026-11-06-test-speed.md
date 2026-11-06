# Рев'ю: найповільніші тести (пункт 131), режим C

Вердикт: **approve**

Перевірив сам: `git diff` по `tests/` і докам, `make lint`, прогін названих файлів. `make lint` чистий. Тести `test_provider_lifecycle`, `test_pm_gap`, `test_assistant_snapshot`, `test_stablecoin_supply`, `test_dashboard_stablecoins`, `test_my_options` і `test_core` разом дали 117 passed за 40 с (`-n 0`).

## Знахідки

1. Асерти не послаблено, тести не видалено (info). У diff лише імпорти, патч `TokenBucket` у чотирьох місцях побудови Runtime і правка `test_my_options`. Жодного `assert` не вилучено. У `test_my_options` один асерт додано (`out["asof_utc"] == ASOF_S`).
2. Патч не ховає перевірене (info). `Runtime.__init__` будує всі `Http` одразу, а `Http.__init__` створює `TokenBucket` (`core/http.py:93`). Патч у `with` на час побудови Runtime тому діє на всі лімітери. Лише `src/tradedesk/alerts/channels.py:74` будує `Http` пізніше, але ці тести його не торкаються. Лімітер покриває `test_core::test_token_bucket_throttles` (не змінено і проходить). Тести з правок перевіряють клієнтів, ретраї й логіку, а не темп запитів. Прийом не новий: у твердженні змін він уже є в `test_tools_bad_input` та інших.
3. Правка годинника в `test_my_options` правильна (info). `bop._now()` повертає рядок для `asof_utc` (`scripts/bybit_options_positions.py:43,51,114`), а `ASOF_S` — той самий формат. Підміна `lambda: ASOF_S` коректна. `my_options_view(out, ASOF)` тепер замість `datetime.now(UTC)` бере час фікстури, тож тест не залежить від експірації 27NOV26. Додаткове `out["asof_utc"] == ASOF_S` перевіряє, що підміна подіяла.
4. Твердження в `docs/changes`, BACKLOG 6.26, HANDOFF і картці збігаються з diff (info). Чотири названі файли справді змінені, `src/` і `scripts/` не чіпали. Числа до/після я не перевіряв: «до» не відтворити без stash, як зазначено в змінах. Прогін після дав 40 с на сім файлів разом з `test_core`, що не суперечить заявленому.
5. Мінорне (low, без блокування). Зміни не називають `test_my_options` у таблиці до/після, а в `docs/changes` рядок «асерти не змінено» не згадує одного доданого асерта. Правки не потрібні, лише уточнення.
