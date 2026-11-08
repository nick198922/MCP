# Рев'ю (режим C): охоронець від справжньої мережі в тестах (пункт 238)

Вердикт: **approve**. Блокерів немає.

Перевірено сам: `git diff` і `git status`; `make lint` (чисто); `uv run pytest -q tests/test_no_real_network.py` (23 passed); `-n auto` на `test_network_boundary`, `test_scripts_http`, `test_dashboard_flows` і `test_no_real_network` (54 passed, xdist і `from conftest import ...` працюють). Повний `make test` не запускав, це робить координатор.

## Що перевірено

1. Фікстура блокує не-loopback. `socket.socket.connect` і `connect_ex` підмінено через `monkeypatch`. Відмова — `RealNetworkBlocked(OSError)`. Блокуються лише AF_INET і AF_INET6. `is_loopback` покриває 127/8, `::1`, IPv4-mapped, `localhost`, bytes і зону IPv6. Будь-яке інше ім'я хоста, включно з `0.0.0.0`, вважається не-loopback. Це доречно для fail-closed.
2. Не ламає loopback, AF_UNIX і asyncio. AF_UNIX пропускається за family, шлях-рядок — через `is_loopback`. Тест `test_guard_lets_loopback_through` піднімає справжній `asyncio.start_server` на 127.0.0.1 і підключається до нього. `open_connection` і `create_connection` на 192.0.2.1 відмовляються (тест є). `test_scripts_import.py` підміняє сокети лише у власному підпроцесі, конфлікту немає.
3. Падіння на teardown доречне. Спроба записується, і тест падає, навіть якщо `Http` проковтнув `OSError` у `missing`. Саме це маскування й треба ловити. `pytrace=False` з чітким текстом, що робити. Самоперевірки очищають `attempts` явно. Маркер `allow_network` зареєстровано в `pyproject.toml`, без нього pytest скаржився б на невідомий маркер.
4. Інваріанти CLAUDE.md. `src/`, `execution/`, колектори не змінено. Тест лише читає файли `tests/` через `ast`, нічого не виконує. Секретів немає, адреси лише TEST-NET-1 (RFC 5737) і публічні назви бірж у фікстурних рядках. Запис у `docs/changes/` є, BACKLOG 6.45 і картку оновлено, числа в них узгоджені зі сканом.

## Не блокує (рекомендації)

1. Сканер можна обійти (хибні проходження, низька серйозність). У модулі достатньо будь-якого імені з `Mock` або `Fake*` (навіть неповʼязаного), щоб відключити перевірку «імпорт без фейка». Прямі виклики це не маскує: `httpx.get` і `httpx.AsyncClient()` без `transport=` ловляться завжди. Також не ловляться `AsyncClient(**kw)`, `transport=None`, клієнт зі `src/`-хелпера (його ловить фікстура) і `__import__`. Перше й останнє названо в docstring і в `docs/changes`. Другий рівень (фікстура) покриває ці прогалини під час виконання. Виправляти не обовʼязково. Якщо захочеться, можна звузити «faked» до `MockTransport`/`transport=`.
2. Не покрито DNS (`getaddrinfo`), підпроцеси, сокети з session/module-фікстур, що створюються до фікстури тесту. Усе задокументовано. Реальний ризик малий: без `connect` дані не підуть.
3. `ALLOWED` на 2 файли — хибні спрацювання з поясненням і перевіркою «застарілих» записів. Прийнятно.
4. Крихкість: `from conftest import REAL_CONNECT, ...` залежить від того, що `tests/` без `__init__.py` і rootdir-режиму імпорту. Зараз працює, включно з xdist. Якщо додадуть `__init__.py`, імпорт зламається гучно, а не тихо.
