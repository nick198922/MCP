**Крок:** p/refill, постійна картка `standing-improvements`: усі 3 відкриті пункти чекають даних / часу, тож додано пункт 404 із джерела «рев'ю не блокує».
• Картки Порядку 0 заблоковані: options-playbook (черга порожня, 4.11), x-traders (картку міняє відкрита гілка), agents-mcp-access (Q-20261114-2, правка `.claude/`).

**Результат:**
• `tests/test_perps_open_interest.py`: 8 поведінкових тестів `perps_open_interest` (oi_usd з openInterestValue / oi_base×last + warning, Deribit oi_base null, Hyperliquid, збій провайдера → `missing`).
• `src/` не змінено; розбіжностей з описом нема.
• `make lint` чисто, `make test`: 10715 passed, 3 skipped.

**Критик:** C (sonnet) — approve; дрібне: крихкий пошук рядка BTC у `test_d`, нема окремого тесту на збій Deribit.

**Далі:** `owner`, пункт 404 завершено; нових питань нема.
