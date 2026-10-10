# ADR 0002 — Єдиний конверт і graceful degradation

Статус: прийнято (2026-10-10)

**Контекст.** У DOUBLETOP добре працював контракт `evidence_ref` + `missing_providers` («ніколи не падати»), але були обрізані відповіді й застарілі health-статуси.

**Рішення.** Кожен інструмент повертає `{ok, data, evidence[], missing[], warnings[], as_of}`.
- `evidence`: звідки число (провайдер, ендпоїнт, аргументи, `fetched_at`, `latency_ms`, `cached`).
- `missing`: що не вдалося отримати, з `reason` ∈ {config, auth, network, rate_limited, provider_tier, data_unavailable, backend_error, unknown} і безпечною `detail`.
- `warnings`: методологічні застереження (припущення, евристики).
- Виняток з інструмента не виходить: `Builder.guard` перетворює `ProviderError` на `missing`.
- `system_health(probe=true)` — жива перевірка замість застарілих кешованих статусів.

**Наслідки.** Модель бачить джерела й прогалини; частковий успіх не ховається. Дані з API розглядаються як недовірений текст.
