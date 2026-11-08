**Крок:** постійна картка (p), відкритий лише заблокований 8a — додано пункт 279 (тести збою інструментів, BACKLOG 6.53).

**Результат:**
• Окремі тести для `funding_zscore`, `skew_percentile`, `liquidations_summary`, `event_study`, `ohlcv_history`.
• Знайдено й виправлено винятки (`TypeError`/`ValueError`/`OverflowError`) на зіпсованих рядках сховища; нове `store_num` у `tools/_util.py`; зіпсовані рядки пропускаються з попередженням.
• `lint` і `make test` чисті: 7539 passed, 3 skipped.

**Критик:** approve ([рев'ю](../../reviews/2026-11-08-tool-failure-tests-2.md)), зауваження не блокують.

**Далі:** `owner`. Відкрито в 6.53: `iv_rank` кидає `TypeError` на зіпсованому `atm_iv`; `ohlcv_history` віддає зіпсовану клітинку як є.
