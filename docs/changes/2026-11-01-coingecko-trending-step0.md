# 2026-11-01 — крок 0 картки coingecko-trending-attention-fade-forward закрито

- Жива відповідь `GET /search/trending` знята із сервера скриптом `scripts/_cg_live.py` (через `rt.coingecko`, `core.http.Http`, без ключа; `--out` обов'язковий). Форма збігається з тією, яку читає парсер: 15 монет, `data.price` — число, `price_change_percentage_24h.usd` є (разом із ~60 іншими валютами), капа й обіг — рядки `"$…"`; новий верхній ключ `rwas` не заважає. Парсер не змінено.
- Нова фікстура `tests/fixtures/coingecko_trending_live_2026-11-01.json` (позначка `LIVE` і джерело в `_source`) і тест `test_snapshot_rows_on_the_live_sample`; рукописна фікстура від 29.10 лишається для тестів на крайові випадки.
- Колектор працює з 29.10 20:00 UTC (рядок у `deploy/features.env` від 29.10): на 01.11 07:00 UTC — 60/60 годин без пропусків, 900 рядків, 87 різних монет, NULL у `price_usd`/`change_24h` 0.
- Оновлено картку («Прогрес»), `docs/HANDOFF.md`, `docs/BACKLOG.md`. Далі — крок 1 через ~90 діб запису.
