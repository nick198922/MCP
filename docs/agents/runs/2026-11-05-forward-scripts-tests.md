**Крок:** p, постійна картка standing-improvements, новий пункт 68 (аудит тестів скриптів форвардів); усі інші картки чекають на час, сервер чи власника.

**Результат:**
• `tests/test_forward_scripts.py`: 7 тестів для `traders_report`, `paper_carry_once`, `paper_ladder_snapshot` (без мережі, БД у `tmp_path`).
• Помилок у скриптах не знайдено, код не змінено.
• Лишилось у BACKLOG 6.10: `paper_bench_manifest`, `stage0_condor_forward`.
• `make lint` чисто, `make test`: 4473 passed, 2 skipped.

**Критик:** C — approve ([рев'ю](../../reviews/2026-11-05-forward-scripts-tests.md)); дрібниці не блокують.

**Далі:** `owner`. Нових питань немає. Заблоковані: structural-edges 8k/8l (чекають тижнів даних), 8a (сервер), market-flow-reader крок 4 (власник), demo-depth 6/9 (чекають ≥ 30 угод).
