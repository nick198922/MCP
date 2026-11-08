# Рев'ю: пункт 167, тест-охоронець інваріанта 2 (`tests/test_network_boundary.py`)

Режим C. Дата 2026-11-06. Вердикт: **approve**.

## Що перевірено самостійно
- `make lint`: чисто. `uv run pytest -q tests/test_network_boundary.py`: 9 passed.
- `git status`: змінено лише `docs/BACKLOG.md`, картку, `docs/changes/2026-11-06-network-boundary.md`, новий тест. `execution/`, колектори, провайдери не чіпані.
- Кількість файлів: `rglob("*.py")` у `src/tradedesk` дає 393, як у змінах і BACKLOG. Мережеві імпорти в 5 файлах (ручний Grep: `exchanges.py`, `liquidations.py`, `runtime.py`, `core/http.py`, `execution/bybit_demo.py`), дозволено 4, порушень 1. Збігається з документами.
- Перевірка по суті `ast.walk` обходить усе дерево, тож імпорти в `try`, `if`, функціях і методах ловляться; фікстурні тести це підтверджують (`try`, клас→метод→`if`, підкинутий файл у `tmp_path`). Відносні імпорти (`level != 0`) та `urllib.parse`, `socketserver`, `httpx_mock_helpers` не дають хибних спрацювань (`_matches` по межі крапки). `from urllib import request` ловиться.
- Причини в `ALLOWED` правдиві:
  - `runtime.py`: `import httpx` (рядок 9) і єдине використання `httpx.AsyncBaseTransport` у типі параметра (рядок 59); запитів немає.
  - `providers/liquidations.py`: лінивий `import websockets` у `_ws_consume` (рядок 239), URL `wss://` без ключів, вікно `max_seconds`; споживач лише `collector/scheduler.py`, у `tools/` модуль не імпортується.
  - `execution/bybit_demo.py`: `import ccxt.async_support` у `_make_exchange` (148) і `import ccxt` у `is_refusal` (158), як записано.
- `KNOWN_VIOLATIONS` чесний: порушення не приховано, не переписано в `ALLOWED`, `execution/` не змінено, рішення передано в BACKLOG 6.45. Тест вимагає точної рівності зі сканом (нове порушення і застарілий запис обидва падають), ALLOWED і KNOWN не перетинаються.
- Нестабільності немає: чистий `ast`, без мережі й годинника, `tmp_path`, детермінований порядок (`sorted`). Єдина залежність від дерева, `n > 300`, має запас.

## Знахідки
1. (низька) Список `NETWORK_MODULES` не охоплює `http.client`, `urllib3`, `asyncio.open_connection`/`ssl`, `smtplib`, `subprocess` з `curl`. Зараз їх у `src` немає (Grep по `httpx|ccxt|websockets|requests|aiohttp|socket|urllib` чистий), тож це лише межа охоплення; в докстрінгу вже сказано «не ловиться мережа через сторонні бібліотеки поза списком». Фікс за бажанням: додати `http.client`, `urllib3`.
2. (низька) Голий `import urllib` з подальшим `urllib.request.urlopen` не ловиться (імпорт підмодуля має бути десь ще). Практично не трапляється; можна згадати в обмеженнях.
3. (інфо) Дозвіл `runtime.py` дає `httpx` усьому модулю, а не лише типу; майбутній виклик `httpx.get` у `runtime.py` пройде. Для охоронця цього рівня прийнятно, причина так і описана.

Блокерів немає. Документи відповідають числам, обмеження (`__import__`, `scripts/`, `tests/`) названі чесно.

Вердикт: approve
