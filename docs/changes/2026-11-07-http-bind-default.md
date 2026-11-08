# Тест-охоронець: HTTP-режим за замовчуванням слухає лише loopback (пункт 188)

- `server.py`: чиста функція `is_loopback(host)` — так для `127.0.0.0/8`, `::1` (і `[::1]`, IPv4-mapped `::ffff:127.0.0.1`), `localhost`; ні для `0.0.0.0`, `::`, адрес мережі, інших імен і порожнього рядка.
- `main()`: якщо streamable-http запускається на не-loopback `host`, у журнал іде попередження `tradedesk.auth` з адресою і станом автентифікації (`enabled` / `DISABLED`). Запуск не блокується: власник може навмисне ставити сервер за проксі. Сам токен у журнал не потрапляє.
- Новий `tests/test_http_bind_default.py` (без мережі, `uvicorn.run` і `build_server` підмінено): `host` за замовчуванням у `Settings()` і `Settings.from_env()` без `TRADEDESK_HOST` — `127.0.0.1`; випадки `is_loopback`; на `127.0.0.1` попередження немає, на `0.0.0.0` є (з токеном і без), і сервер все одно стартує.
- `execution/`, колектори, реєстрації не чіпали.
- Перевірка: `make lint`; `tests/test_http_bind_default.py` + `tests/test_server.py` — 99 passed.
