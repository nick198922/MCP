**Крок:** p, постійна картка `standing-improvements` (Порядок 1): усі картки Порядку 0 заблоковані (питання власника, гілка btc_charlie в роботі, чекають даних / часу); доданий пункт 400 за «Як поповнювати» → BACKLOG.
**Результат:**
• `contract_risk` має друге джерело — honeypot.is (безкоштовне, без ключа; ланцюги 1 / 56 / 8453; `providers/honeypot_is.py`, ліміт 0.5/с).
• Honeypot, cannot_sell_all, buy_tax, sell_tax: гірший вердикт із двох джерел; розбіжність → `warnings`; збій honeypot.is → `missing`, без «зеленого».
• Нова перевірка `holder_sell_fail_rate` (high → critical, medium → red_flag); пороги не відкалібровано (на 5 живих токенах окремо не спрацювала).
• Наслідок: на Arbitrum / Polygon / Optimism / Avalanche `overall` не може бути pass.
• Живі зразки (WETH, SHIB, FLOKI, EverGrow, SafeMoon) — `tests/fixtures/honeypot_is_2026-11-19.json`. `make lint && make test`: 10705 passed.
**Критик:** C — два раунди виправлень (знахідки 1–6: хибне зіставлення fail-rate з cannot_sell_all, суперечливі value / detail, «pass» при відсутніх flags). Остання правка (знахідка 6) перевірена мною (тести, lint), але критик її окремо не переглядав.
**Далі:** `owner`; питання нових немає. Відкрите: список flags обрізано до 20 записів (на живих відповідях 0–1); `ETHERSCAN_API_KEY` для служби — Q-20261119-1.
