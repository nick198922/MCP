**Крок:** p → поповнення постійної картки (`IDEA-20261102-standing-improvements`, пункт 409): усі інші картки власника заблоковані, не заблокованих пунктів не було.

**Результат:**
• Новий інструмент MCP `demo_status` (лише читання): позиції, закриті угоди й середній R за сетапами проти смуг паспортів, замітки, правило зупинки S12.
• Числа ті самі, що в `scripts/checkpoint_report.py`: спільні чисті функції в `analytics/checkpoint.py`, вивід звіту не змінився; на копії живої БД (269 угод) збігається по всіх 9 сетапах.
• Типова відповідь 6.0 тис. символів; повна — `include_details=true`.
• `make lint` і `make test` пройшли (10815 passed, 3 skipped).

**Критик:** C, approve. Три дрібні зауваження (поріг 100 000 рядків без попередження, явне сортування за часом і id, довгі замітки в компактному режимі) — не блокують, записано в рев'ю `docs/reviews/2026-11-19-demo-status.md`.

**Пропущено:** options-playbook п. 4 (черга порожня, 4.12); agents-mcp-access (`.claude/agents/*` заборонено, Q-20261114-2); x-traders (гілка `agent/2026-11-18-x-btc-charlie-sample-registration` чекає власника); profit-search, structural-edges 8f, book-walls, onchain-parity — чекають часу, даних чи ключа; сито на паузі.

**Далі:** `owner`. Після злиття перевірити: `uv run python scripts/mcp_call.py demo_status '{}'`.
