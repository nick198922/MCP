**Крок:** p, постійна картка `standing-improvements`, пункт 276 (додано координатором): захист `smart_money` від `OverflowError`/NaN/inf.

**Результат:**
• `parse_portfolio` і `realized_pnl_windows` (`analytics/smart_money.py`) пропускають зіпсовані точки й угоди та називають поле в `dropped`; `verify_smart_money.py` виводить попередження з адресою трейдера.
• Проблему відтворено до змін (`OverflowError` на `10**400`/inf, NaN у сумах); звичайні входи дають ті самі числа (фікстура 17.10).
• Перевірка координатора: `make lint` чисто, `make test` 7511 passed, 3 skipped.

**Критик:** approve (C, sonnet); три низькі зауваження, не блокують ([рев'ю](../../reviews/2026-11-08-smart-money-float-guard.md)). Заяву розробника про живу перевірку на 3 акаунтах критик не відтворив.

**Далі:** `owner`. Відкрите в BACKLOG 6.53: `collector/` (`parse_bar`, DVOL у `scan_gaps`), `pm_threshold.history_alignment`. Нових питань немає.
