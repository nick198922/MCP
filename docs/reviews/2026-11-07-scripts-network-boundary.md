# Рев'ю: охоронець мережевих імпортів у `scripts/` (пункт 220, BACKLOG 6.45)

Режим C. Дата 2026-11-07. Переглянуто `git diff` (`tests/test_network_boundary.py`, `docs/BACKLOG.md`, картка 220) і новий `docs/changes/2026-11-07-scripts-network-boundary.md`.

## Вердикт: approve

Блокерів немає.

## Що перевірив сам
- `make lint`: чисто. `uv run pytest -q tests/test_network_boundary.py`: 12 passed.
- `git status`: змінено лише тест, BACKLOG, картка 220 і файл змін. `src/`, `execution/`, самі скрипти не чіпали. Ключів і токенів у дифі немає, у причинах лише назви env-файлів і шляхи.
- Незалежний Grep по `scripts/**/*.py` (імпорти `httpx`, `requests`, `urllib.request`, `ccxt`, `aiohttp`, `websockets`, `socket`) дає те саме коло файлів, що в списках. Це 17 файлів: 14 у `ALLOWED_SCRIPTS`, 3 у `KNOWN_SCRIPT_VIOLATIONS`. Файли, що імпортують лише `urllib.parse` (`radar_weekly_tg`, `assistant_brief_tg`, `docs_links`, `common_helpers`, `forecast_weekly_tg`), правильно не ловляться.
- Скан коректний. Це `ast.walk`, тож ловляться імпорти всередині функцій і `try`. Враховано `import x as y`, `from urllib import request`, підмодулі (`requests.adapters`, `ccxt.async_support`) і відносні імпорти. `socketserver` і `httpx_mock_helpers` не плутаються з `socket` і `httpx`. Скан використовує існуючу, вже перевірену функцію `network_imports`.
- Причини в `ALLOWED_SCRIPTS` чесні. Звірено з кодом:
  - `thegraph_sample`: `httpx` справді лише для `Recorder(httpx.AsyncBaseTransport)`, запити йдуть через `rt.thegraph`.
  - `bybit_demo_key`: `urllib.request.Request`/`urlopen` лише до `api-demo.bybit.com`, `/v5/user/query-api`.
  - `bybit_options_positions`: `ccxt.bybit` з ключем і читанням через `bybit_import`.
  - `dashboard`: `urlopen` лише до `api.anthropic.com/api/oauth/usage`.
- Фікстурні тести змістовні: порушення, дозволений файл, імпорт `Http`, а також дозвіл по модулях (дозволеному `tg_send.py` не можна `ccxt`). Тест на репозиторії двосторонній: нове порушення і застарілий запис обидва падають. Перевірка `n > 200` ловить хибний шлях `SCRIPTS`.

## Зауваження (не блокують)
1. Низька. Дозвіл дається на файл і модуль, а не на хост. Наприклад, `tg_send.py` міг би почати ходити по ринкові дані через `urllib.request`, і тест цього не побачить. Це прийнятне обмеження, але його варто назвати в докстрингу. Тепер у ньому перелічено лише `__import__`/`importlib`, сторонні бібліотеки, `.sh` і `tests/`.
2. Низька. `import urllib` з наступним `urllib.request.urlopen(...)` скан не ловить, бо `urllib` не в списку, а `urllib.request` не імпортується явно. Реально виконується лише якщо інший модуль уже підвантажив `urllib.request`. У `scripts/` таких випадків немає (Grep), але в «Не ловиться» це не записано.
3. Низька. Три порушення (`hyperliquid_import`, `copy_vaults`, `research/vault_profile`) лишилися в `KNOWN_SCRIPT_VIOLATIONS` без рішення, як і було задумано (не переписувати). `copy_vaults` ходить кожні 2 хвилини; рішення для власника записано в BACKLOG 6.45.
4. Інформаційно. Рядки списків ~150 символів. Lint проходить, тож це не проблема.
