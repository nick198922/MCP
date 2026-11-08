# Рев'ю: охоронець меж імпортів (пункт 112, BACKLOG 6.20)

Режим C. Вердикт: **approve** (зауваження 1-3 не блокують).

Перевірено сам: `make lint` чистий; `uv run pytest -q tests/test_architecture_imports.py` дає 5 passed; `git status` показує зміни лише в `tests/`, `docs/`, а `src/` і `execution/` не змінені.

## Що перевірено

- Allowlist чесний. Grep по `src/` підтверджує, що `execution/bybit_demo.py:148` має `import ccxt.async_support`, а `:158` має `import ccxt`, обидва всередині функцій. Це справжні порушення правила «ccxt лише в `providers/exchanges.py`». Інших імпортів `httpx` / `ccxt` поза власниками немає (`core/http.py`, `runtime.py`, `providers/exchanges.py`). Тест `test_allowed_entries_are_still_real` не дає списку застаріти.
- AST-скан охоплює всі `Import` / `ImportFrom` через `ast.walk`, тобто і імпорти всередині функцій. Відносні імпорти вирішуються правильно: `_package` дає пакет модуля, `level` відсікає зайве. Для `from X import y` додається `X.y`, тому ловиться `from tradedesk import execution` і `from urllib import request`. Дедуплікація `X`/`X.y` на одному рядку не ховає різних правил. Синтетичний тест покриває кожне правило, функціональний імпорт, relative-імпорт, alias `dt.datetime.utcnow` і відсутність хибного спрацьовування на `open(` у docstring.
- Картка (рядок 112) і BACKLOG 6.20 відповідають коду: 2 порушення, номери рядків ≈148/≈158 вірні, чекбокс 6.20 лишено відкритим, що логічно. Файл `docs/changes/` є.

## Зауваження (низька серйозність, не блокують)

1. Хибнонегативи в `impure_calls`: порівняння іде за текстом імені, тому `from time import time as t; t()` і `from datetime import datetime as D; D.now()` не ловляться (ловляться лише `time.time`, `datetime.now`, `*.datetime.now`). Так само не видно `__import__` / `importlib.import_module`. Для чистоти `analytics/` на сьогодні це не проблема (тест зелений), але охоронець слабший, ніж каже docstring. Варіант виправлення: збирати alias-и з `ImportFrom` і зіставляти з ними. Можна й просто задокументувати обмеження.
2. Правило (б) стосується лише `httpx` і `ccxt`. `providers/liquidations.py:239` імпортує `websockets` (функціонально), що за буквою інваріанта 2 теж мережа поза `Http`. Це за межами формулювання картки, і `websockets` дозволено провайдеру за дизайном. Варто лише зафіксувати це в docstring як свідому межу або додати у `NETWORK_IMPORT_OWNERS` разом з allowlist.
3. `ANALYTICS_FORBIDDEN_IMPORTS` не містить `os` / `pathlib` / `requests`-подібних обгорток, окрім перелічених у картці. Це відповідає завданню, лише для відома.

## Висновок

Охоронець відповідає картці, тести змістовні (були б червоними при порушенні), allowlist правдивий, документація точна, `src/` і `execution/` не чіпали. Дозволити злиття.
