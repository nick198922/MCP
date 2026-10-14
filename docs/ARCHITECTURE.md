# Архітектура

## Принципи

1. **Спочатку читання.** Усе, що змінює стан (ордери), з'являється останнім, за окремими запобіжниками (див. SECURITY.md).
2. **Кожне число має джерело.** Інструмент повертає конверт з `evidence` (провайдер, виклик, аргументи, час, cache-hit). Цей патерн узято з `dt_edu` у DOUBLETOP й підсилено.
3. **Збій одного джерела не валить відповідь.** Помилка провайдера стає записом у `missing` з класом причини, а решта даних повертається.
4. **Математика окремо від мережі.** `analytics/` складається з чистих функцій без I/O, і їх тестують на реальних зразках.
5. **Один довгоживучий процес.** Сервер має працювати, поки ви спите: це вимагає збереження стану, фонових задач і деплою на постійно ввімкнену машину (Фаза 2).

## Шари

```
Claude (Desktop / Code / claude.ai)
        │  MCP (stdio або streamable-http)
┌───────▼──────────────────────────────────────────┐
│ tools/        тонкі MCP-інструменти, повертають Envelope │
├──────────────────────────────────────────────────┤
│ analytics/    чисті функції: Black-76, smile, GEX, basis, │
│               ймовірності, odds, стакан (без I/O)         │
├──────────────────────────────────────────────────┤
│ providers/    Deribit · Hyperliquid · ccxt-біржі · (далі…)│
├──────────────────────────────────────────────────┤
│ core/         Http (retry, rate-limit, cache), TTLCache,  │
│               HealthRegistry, Envelope/Evidence, errors    │
└──────────────────────────────────────────────────┘
Фаза 2+: store/ (SQLite) · workers/ (collector, alerts) · risk/ · strategies/ · execution/
```

`runtime.py` збирає налаштування, кеш, health і провайдерів в один об'єкт `Runtime`. Інструменти отримують його через замикання `register(server, rt)`, тому в тестах підміняються транспорт, біржі й годинник (`rt.now`).

## Контракт відповіді (Envelope)

```json
{
  "ok": true,
  "data": { "...": "..." },
  "evidence": [{"provider": "deribit", "tool": "public/get_book_summary_by_currency",
                "args": {"currency": "BTC", "kind": "option"}, "fetched_at": "…", "latency_ms": 212, "cached": false}],
  "missing":  [{"field": "funding:okx", "provider": "ccxt:okx", "reason": "rate_limited", "detail": "…"}],
  "warnings": ["okx: funding interval not reported, assumed 8h"],
  "as_of": "2026-10-10T13:07:00+00:00"
}
```

Класи причин (`core/errors.py`): `config`, `auth`, `network`, `rate_limited`, `provider_tier`, `data_unavailable`, `backend_error`, `unknown`.

## Конвенції одиниць (не змінювати без міграції)

| Величина | Одиниця |
|---|---|
| IV, ставки, funding, basis | десяткові дроби (0.45 = 45 %) |
| funding «річний» | простий, без капіталізації |
| Greeks | Black-76, r = 0, форвард експірації. vega на 1 пункт вола, theta на календарний день, у USD на 1 одиницю базового активу |
| Open interest | в одиницях базового активу (BTC), для Deribit-ф'ючерсів в USD |
| Час | UTC, ISO-8601 або epoch ms (суфікс `_ms`) |
| Експірації Deribit | 08:00 UTC; токен без нуля попереду (`5NOV26`) |

## Deribit: два хости

- `rt.deribit` працює з `www.deribit.com/api/v2`: живі дані інструментів, DVOL, `delivery_prices` (історія розрахункових цін, USD) і `funding_rate_history` (погодинний funding перпа, `interest_1h`; ≤ 744 записів за запит).
- `rt.deribit_history` — той самий клас `Deribit` на архівному хості `history.deribit.com/api/v2`, з окремим rate-limit 5 запитів/с і окремим записом `deribit_history` у health. Лише він віддає угоди давно експірованих інструментів:
  - `last_trades_by_currency_and_time`;
  - `last_trades_by_instrument_and_time`.

  Обидва методи повертають до 1000 угод, пагінація за `timestamp`; `iv` у відсотках, ціни опціонів у BTC.

`delivery_prices` і funding на history-хості дають HTTP 400. Використовується лише дослідницькими скриптами (`scripts/verify_put_skew.py`, `scripts/verify_dated_basis.py`), MCP-інструменти його поки не викликають.

`tradingview_chart_data` (OHLCV одного інструмента, `resolution` у хвилинах або `1D`) працює на обох хостах, зокрема для давно експірованих ф'ючерсів (перевірено BTC-28JUN19, 2026-10-14).

## ccxt: сирі публічні ендпоінти

`Exchanges.public_get(ex_id, endpoint, params)` викликає неявний ендпоінт ccxt (наприклад, `fapiPublicGetContinuousKlines`, `fapiPublicGetIndexPriceKlines`, `fapiPublicGetKlines` для експірованих USDT-M квартальних) через `_call`, тобто з health і `Evidence`. Дозволено лише `*PublicGet*`; приватні або змінні ендпоінти відхиляються з `ProviderError(reason="config")`. Призначено лише для дослідницьких скриптів.

## Як додати провайдера

1. `providers/<name>.py`: клас з методами, що повертають `Fetched(data, evidence)`. HTTP тільки через `core.http.Http`, щоб діяли ліміти, ретраї, кеш і health.
2. Підключити в `runtime.py` з власним rate-limit.
3. Мок-транспорт і тести в `tests/` (реальний зразок відповіді в `tests/fixtures/`).

## Як додати інструмент

1. Функція в `tools/<domain>.py` усередині `register(server, rt)`. Параметри через `Annotated[..., Field(description=…)]`, опис англійською (модель краще вибирає інструмент).
2. `b = Builder()`; виклики провайдерів через `await b.guard(...)`; наприкінці `b.build(data)`.
3. Не кидати винятки назовні: помилка йде у `missing`.
4. Додати до `EXPECTED_TOOLS` у `tests/test_server.py` і тест на поведінку при збої.

## Деплой: «працює, поки я сплю»

Mac засинає, тому для автономної роботи потрібна постійно ввімкнена машина.

| Варіант | Плюси | Мінуси |
|---|---|---|
| **VPS (Hetzner/DigitalOcean, ~5 €/міс)** | дешево, повний контроль, SQLite на диску | потрібно налаштувати Docker і TLS |
| Fly.io / Railway | простий деплой із git | вартість росте з диском і CPU, ліміти на IP |
| Mac, що не засинає | безкоштовно | вимкнений ноутбук або мережа вбивають збір даних |

Рекомендація: **VPS + Docker + Caddy (TLS) + доступ через Tailscale або Cloudflare Tunnel з авторизацією**. Сервер стартує в режимі `streamable-http`. Поки немає автентифікації (Фаза 6), його не можна відкривати в публічний інтернет.

Фонові задачі виконуються в тому самому процесі (asyncio): збирач знімків, WebSocket-збирач ліквідацій, рушій алертів. Алерти йдуть у Telegram-бот (потрібен токен бота).

## Відкриті рішення

- Хостинг (VPS чи інше) і домен/тунель.
- Канал алертів (Telegram — припущення; `TelegramChannel` увімкнеться при наявності env-токенів).
- IBKR — **відкладено** за рішенням власника (див. [ADR 0006](adr/0006-no-ibkr-for-now.md)).
