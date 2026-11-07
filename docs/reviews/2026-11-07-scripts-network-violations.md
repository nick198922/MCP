# Рев'ю: пункт 235, три скрипти переведено на core.http.Http

Режим C, незакомічені зміни робочого дерева. Рев'юер: критик.

**Вердикт: approve.** Блокерів немає, є три зауваження без блокування.

## Що перевірено самостійно

- `ruff check scripts tests`: чисто. У цьому клоні немає `make lint`, тому запускав ruff напряму через uv.
- `pytest tests/test_scripts_http.py tests/test_network_boundary.py tests/test_copy_vaults.py tests/test_vault_profile.py`: 45 passed.
- `git status` не показує змін у `src/` і `src/tradedesk/execution/`. `registry.csv` теж не чіпали.
- `KNOWN_SCRIPT_VIOLATIONS` порожній, а тест межі мережі проходить. Ні в яких скриптах не лишилося `urllib` чи `httpx` поза списком дозволених.
- Усі місця запуску йдуть через `uv run`: `deploy/user-units/tradedesk-copy-vaults*.service`, `tradedesk-vault-watch.service`, `scripts/server/hl-import.sh`. Тому залежність від `httpx` у venv безпечна.

## Перевірені пункти

1. **Вихід за нормальних запусків не змінився.**
   - `_post` повертає той самий `Fetched.data` (JSON).
   - `_get` повертає той самий `result` Bybit.
   - `fetch_fills` дає ту саму пагінацію, дедуплікацію та сортування. Тест проходить на двох сторінках, перша повна.

2. **Семантика помилок збережена.**
   - `vp._post` перетворює `ProviderError` на `RuntimeError("Hyperliquid info <type> failed: …")` з `__cause__`.
   - Тому `load_candles` (`except RuntimeError` у `vault_profile.py`, рядок 138) і `collect` у `vault_watch_tg` (`except Exception`) працюють як раніше.
   - `copy_vaults._get` на транспортному збої кидає `ProviderError`, а на `retCode != 0` кидає `RuntimeError`. Обидва ловить `except Exception` у `main` («run skipped»). Тест `test_copy_vaults_run_skips_on_a_failed_call` це підтверджує.
   - `fetch_fills` тепер кидає `ProviderError` замість `httpx.HTTPStatusError`. Обробника в `main` не було й раніше, тож це той самий traceback.

3. **`run_sync` коректний.**
   - Кожен виклик робить `asyncio.run` і закриває клієнт у `finally` до завершення циклу. `Http` перестворює клієнт на наступному виклику.
   - `TokenBucket` створює `asyncio.Lock` у `__init__`. Блокування прив'язується до циклу лише при змаганні, а виклики послідовні, тож проблем немає. Тест з двома викликами поспіль це підтверджує.
   - Усі виклики з синхронного коду: `main`, `collect`, `summary`, `run`. Усередині працюючого циклу ніхто не викликає, а docstring про це застерігає.
   - Ціна рішення: на кожен запит нове TLS-з'єднання. Для таймера раз на 2 хвилини це нормально.

4. **Ліміти розумні.**
   - Bybit: 5/с, ретраї 2.
   - HL info у `vault_profile`: 2/с, burst 1, ретраї 4, backoff до 5 с. Раніше було 5 спроб із паузами 2+5+8+11 с.
   - HL info в імпорті: 3/с, що відповідає колишній паузі 0,3 с.
   - Зміна поведінки: клієнтські 4xx (422) більше не ретраяться, раніше ретраїлися всі помилки. Це покращення, і тест на це є.

5. **Тести змістовні.** Використано `httpx.MockTransport`. Перевіряються тіло запиту, шлях, кількість викликів при 5xx та 4xx, пропуск монети при збої в `load_candles`, пагінація й дедуплікація, `retCode`.

## Зауваження без блокування

1. **Низька.** `vault_profile._post` не передає `args=` у `post_json`. Тому в `Evidence.args` потрапляє `{"body": {..."user"/"vaultAddress": адреса}}`. Evidence тут нікуди не пишеться, `health` не підключено, помилки беруть лише фрагмент відповіді. Витоку немає, але для узгодженості з `hyperliquid_import` (там `args={"startTime": t}`) варто додати `args={"type": body.get("type")}`.
2. **Низька.** Docstring `vault_watch_tg.py` досі каже `python3 scripts/vault_watch_tg.py`. Після зміни скрипту потрібен venv (раніше теж був numpy у `vault_profile`). Юніт використовує `uv run`, тож працює; це тільки неточність у тексті.
3. **Низька.** У `hyperliquid_import.py` докстрінг став довшим за 120 символів. Ruff пройшов, тож це лише косметика.

## Вимоги чекліста

- Документи: `docs/changes/2026-11-07-scripts-network-violations.md` і примітка BACKLOG 6.45 є.
- Нові інструменти MCP не додавалися, тож чекліст нового інструмента не застосовується.
- Ключів і ордерного коду немає.
