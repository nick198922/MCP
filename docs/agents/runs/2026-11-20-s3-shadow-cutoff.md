**Крок:** постійна картка `standing-improvements`, новий пункт 428 (усі картки власника заблоковані, див. нижче); розробник → критик C.

**Результат:**
• `verify_s3_demo_vs_passport.py`: нові `--cutoff` і `--db`; розділ тіньових планів при `--cutoff` рахується з БД (read-only) як у S12/S38, тож повтор дає той самий розділ.
• Без `--cutoff` поведінка попередня (з JSON). Закриті угоди й решта звіту лишаються з JSON.
• Тести: `tests/test_verify_s3_demo_vs_passport.py` (2). `make lint` чисто, `make test`: 10986 passed, 3 skipped.

**Критик:** approve (C), блокерів нема; дрібні зауваження: рядок у виводі про те, що cutoff не діє на закриті угоди, і прямий тест фільтра за `signal_ts`.

**Чому не інші картки:**
• options-playbook п. 4: черга «правило без доказів» порожня.
• agents-mcp-access: потрібні зміни в `.claude/agents/*` і `.claude/nightly.md`, які мені заборонені правилами координатора — питання власнику: зробити самому або дозволити розробнику.
• x-traders чекає гілку `agent/2026-11-18-x-btc-charlie-sample-registration`.
• profit-search п. 2/3/4: кандидаток нема; structural-edges 8f/8k/8l і demo-setups п. 6, short-setups, book-walls: чекають дат, даних чи сервера; sieve на паузі.

**Далі:** `owner`.
