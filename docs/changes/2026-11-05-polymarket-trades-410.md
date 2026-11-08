# Polymarket `trades`: явна відмова на HTTP 410 (пункт 106)

- `providers/polymarket.trades`: HTTP 410 від data-api `/trades` (v1 знято 02.11) тепер дає `ProviderError` з текстом «data-api v1 /trades removed 2026-11-02; migrate to /v2/trades, see BACKLOG 6.13».
- Ретраїв нема: 410 поза `_RETRY_STATUS` у `core/http.py`, тож робиться рівно один запит (перевіряє тест).
- `health` не розрізняє «тимчасові» й «постійні» збої: кожна помилка Http рахується однаково, тож 410 так і рахується. `core` не змінювали, щоб не зачепити інші провайдери.
- Міграцію на `/v2/trades` не робили (потрібен живий зразок від власника).
- Тест: `tests/test_pm_threshold.py::test_trades_v1_410_is_a_clear_provider_error_without_retry` (MockTransport, `retries=3`, один запит).
