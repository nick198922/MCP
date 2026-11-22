# Рев'ю C: тести perps_open_interest, збій Deribit (пункт 404b)

Вердикт: **approve**.

Перевірено сам: `git diff`, `git status`, `uv run pytest -q tests/test_perps_open_interest.py` (10 passed), `make lint` (чисто), код гілки `dbt()` у `src/tradedesk/tools/perps.py` (1271-1276, 1278-1288).

1. `src/` не змінено: у diff лише тест, картка ідей і запис у `docs/changes/`. Ок.
2. `test_d`: рядок BTC береться за індексом назви в `universe`, є явний `assert "BTC" in names` з повідомленням, магічного `"1500"` нема. Ок. Припущення «контекст за тим самим індексом» збігається зі справжнім `metaAndAssetCtxs`.
3. `test_e_deribit_down` (BTC, ETH) не тавтологія. Він упав би, якби гілка зламалась: без `b.guard` виняток провайдера дав би не `ok=true`; без запису в `missing` падає перевірка `provider == "deribit"`; якщо Deribit лишається в `rows` попри 503, падає `"deribit" not in rows`; втрата решти бірж чи `_warn_partial` падає на `>= set(rows)` і `partial:`. Для ETH гілка дійсно досягається (запис у `missing`), тож параметризація не порожня.
4. Дрібне, не блокує: `total_oi_usd == sum(venues)` повторює формулу коду (слабка перевірка), але поруч стоїть `> 0` і сума часток ≈ 1, що ловить зсув. Окремої перевірки «Deribit у happy-path для ETH» нема (`test_c` бере BTC), для цього пункту це не потрібно.
5. Документація: `docs/changes/2026-11-19-perps-oi-deribit-failure-test.md` і пункт 404b відповідають diff. Ок.
