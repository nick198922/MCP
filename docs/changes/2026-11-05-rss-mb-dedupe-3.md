### 2026-11-05 — Скрипти: остання партія локальних `rss_mb` → `common_helpers.rss_mb` (BACKLOG 6.4)

- Картка [standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), пункт 91 (продовження пунктів 75 і
  87: [перша](2026-11-05-rss-mb-dedupe.md), [друга партія](2026-11-05-rss-mb-dedupe-2.md)). Лише `scripts/verify_*.py`
  і тест; `src/`, `execution/`, колектори, форварди не чіпали; вивід скриптів не змінився.
- Замінено на `from common_helpers import rss_mb` (6):
  - `verify_option_modules`, `verify_pm_h7_qlike`: тіло `ru_maxrss / 1024.0` дає те саме число, що `/ 1024` у хелпері
    (ділення на степінь двійки точне; `test_rss_mb_is_peak_rss_in_mb` це перевіряє); `import resource` прибрано.
    `vom.rss_mb` ніхто не бере (`stage0_condor_forward` рахує RSS сам).
  - обгортки `return v0.rss_mb()` у `verify_news_events_0b`, `_0c`, `_0d`, `verify_market_makers_news`: `v0.rss_mb`
    з пункту 75 — це вже `common_helpers.rss_mb`, тож обгортка лише додавала виклик.
- Лишилась 1 локальна копія: `verify_carry_a_entry_ban` (`-> int`, `// 1024`) друкує `peak rss {rss_mb()} MB` без
  форматування; хелпер дав би дріб (`123.45… MB`), тобто інший вивід, тому не чіпали.
- Тест: 6 модулів додано до `RSS_MB_IMPORTED` у `tests/test_common_helpers.py` (`rss_mb is common_helpers.rss_mb`,
  немає `resource`).
- Перевірка: `make lint`; `uv run pytest -q tests/test_common_helpers.py tests/test_verify_pm_h7_qlike.py` (91 passed);
  `uv run pytest -q tests/test_forward_scripts.py` (13 passed, там `verify_option_modules.load`);
  `python -m py_compile` усіх 7 скриптів. Повний `make test` не запускав (за завданням).
- Ім'я файлу `-3`: `2026-11-05-rss-mb-dedupe-2.md` уже зайняте пунктом 87.
