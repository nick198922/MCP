# Рев'ю (режим C): btc-review-findings (пункт 393)

Вердикт: **approve**

Перевірено самостійно: `git diff`, `make lint` (зелений), `uv run pytest -q tests/test_bitcoin.py tests/test_server.py` (127 passed).

1. Логіку не змінено: `src/tradedesk/analytics/` без змін, у `tools/bitcoin.py` лише текст опису й один `b.warn`. Опис англійською, одиниці (sat/vB) збережено. Серйозність: немає зауважень.
2. Тести ловлять переплутані `_FEE_KEYS`: `fees_summary` будує `{name: _num(body.get(key))}`, а тест дає п'ять різних значень (5/4/3/2/1) і порівнює словник повністю. Заміна імен між двома парами змінить значення в полях, тест впаде. Другий тест перевіряє, що відсутній `hourFee` дає `None` лише в `hour_sat_vb`. Сам обмін ключів я не запускав (змінювати код мені не можна), висновок з читання.
3. Попередження `fee_bands are approximate` перевіряється в `test_btc_mempool_happy`. Формулювання відповідає коду (рядок гістограми йде в смугу за нижньою межею; граничні випадки покриває наявний `test_fee_bands_edges_and_bad_rows`).
4. Низька, не блокує: приклад в описі («bin spanning 1.95-2.3 sat/vB lands wholly in 1-2») гіпотетичний; мовою «each histogram bin is counted in the band of its lower fee-rate edge» припускається, що ставка в рядку гістограми є нижньою межею. Якщо це не так, перефразувати на «by the fee rate listed in its row». Виправляти необов'язково.
5. Рядок опису став дуже довгим, але `ruff` проходить. Запис у `docs/changes/2026-11-19-btc-review-findings.md` є.
