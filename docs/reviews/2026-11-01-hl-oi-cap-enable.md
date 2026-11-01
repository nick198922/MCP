# Рев'ю: вмикання колектора hl_oi_cap (deploy/features.env)

Вердикт: **approve**

Зміна: один рядок `TRADEDESK_COLLECT_HL_OI_CAP_ENABLED=1` + коментар у `deploy/features.env`.

Перевірено сам:
1. Назва змінної збігається з `src/tradedesk/config.py:240` (`collect_hl_oi_cap_enabled`, значення "1/true/yes"); дефолт False, `deploy/env.example:75` її описує.
2. Правила префіксів: тест `tests/test_hl_oi_cap.py:208` перевіряє `features.allowed(...)` для ENABLED і SECONDS, проходить; `-k features` (21 тест) зелений.
3. Джоба підключена в `scheduler.py:645-648` лише при прапорці, період 600 с (тест на реєстрацію є).
4. Інваріанти 1-2: усе I/O через `rt.hyperliquid` (`perps_at_oi_cap`, `asset_contexts`) → `core.http.Http`; два публічні read-only POST /info, ордерного коду немає.
5. Збій провайдера: `Builder.guard` повертає None/порожнє, джоба логує `missing`, нічого не пише, повертає 0 і повторює на наступному тіку (тести 155-177: порожня БД, лог, наступний успіх пише 4 рядки). Без стору або вже записана година: 0, без запитів (тест 186). Планувальник додатково ловить винятки джоб.
6. Запис ідемпотентний (`INSERT OR IGNORE`, ключ ts+coin), тест 196-200.
7. `uv run pytest -q tests/test_hl_oi_cap.py`: 14 passed.

Зауваження (низька важливість, не блокують): `make lint` не запускався (змінено лише env-файл); навантаження ~2 запити/год, ризику для ліміту HL немає.
