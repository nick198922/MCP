# 2026-11-27 — smart money: ліміт спроб `--stage full` на вирішальну дату

- Картка `IDEA-20261015-smart-money-persistence`: поправка 27.11 до «Порядку спроб» (пп. 7–12, рев'ю підготовки зрізу 2, раунд 2, R2-1), записана до будь-яких даних зрізу 2 і переписана того ж дня після [рев'ю B](../reviews/2026-11-27-smart-money-attempt-cap.md) (request changes). Перше формулювання не діє.
  - Спроба зараховується лише тоді, коли її `.md` дійшов до рядка `## Зріз:` (після `fetch`, перед `### H1`, тобто перед першим наслідком). Збій до нього спроби не витрачає. Ліміт — 3 зараховані спроби на серію.
  - Повтор після спроби без JSON або з `stopped` обов'язковий. 48 год — ціль (пропуск — порушення у звіті, серію не закриває). Закриває лише межа 30 днів. Кінець спроби — mtime `.md` / JSON.
  - R1 «виправити пакетування й перезапустити» не скасовано. Після закритої серії 1 одне виправлення Gamma через критика обов'язкове і відкриває рівно одну серію 2 з тим самим лімітом. Код між спробами однієї серії не змінюють.
  - Якщо й серія 2 без вирішальної спроби: H3 — «зупинено», H1 / H1w / H2 — з останньої за часом спроби, що записала JSON. «Нема даних» — лише коли JSON не записала жодна спроба. Наступний зріз іде без `--h3-prior-json`.
  - Перевірку стопу Gamma до `### H1` (зауваження 4) не взято: вона змінила б вивід вирішального прогону і прибрала б H1 / H2 із зупинених JSON.
- `scripts/verify_smart_money_slice1.py`:
  - `MAX_ATTEMPTS = 3`, `SLICE_HEAD`, `series_attempts()` (зараховує `.md` з рядком `## Зріз:`, as-of за цілою секундою; нечитаний — зараховує, порожній чи без рядка — ні), `check_attempt()`.
  - `--series 2` (`full-s2*.md`): лише `--stage full` на вирішальній даті без `--no-fetch` / `--out` і лише після 3 зарахованих спроб серії 1 без вирішальної.
  - `--as-of` з дробовою секундою відхиляється до будь-якого запису.
  - `emit` пише у файл (`flush` + `fsync`) до `print`.
  - Новий рядок `code: <sha256[:16]>` (раннер, `common_helpers.py`, модулі аналітики) у кожному звіті.
- [Рев'ю B, раунд 2](../reviews/2026-11-27-smart-money-attempt-cap.md): approve; незаблокувальні зауваження 1–4 записано відкритим пунктом картки (лише до 16.12).
- Відбиток реєстрації (`registration()`), пороги, seed і вибірки не змінено. `full.json` зрізу 1 приймається як prior так само. Тест фіксує sha256 відбитка `50025b124d73…f843`.
- Тести: `test_series_attempts_counts_reports_that_reached_the_outcomes`, `test_full_stage_prints_the_attempt_line_before_any_outcome`, `test_emit_writes_the_report_before_the_screen`, `test_main_refuses_attempts_the_card_rule_does_not_allow` (разом із потоком coverage → full), `test_main_refuses_a_fractional_as_of_before_any_write`, `test_stopped_crashed_and_clean_attempts_behave_as_the_card_says`, `test_series_2_only_after_series_1_closed_and_only_as_attempts`, `test_registration_fingerprint_is_the_one_slice_1_was_run_with`. Усе синтетичне, без мережі й живої бази.
- Регресія виводу: офлайн-повтор зрізу 1 (`--no-fetch`, копія бази в `smart_money_slice1`, `--out ~/.cache/tradedesk-agents/tmp/replay_attemptcap_r2.md`). Порівняно з `replay_nofetch.md` / `.json`: усі ключі JSON, крім `hl_zero_quality`, рівні. У `.md` нові лише рядки `out:` / `code:`, час і RSS, а також таблиця якості нулів HL. Її додав раунд 26.11, після `replay_nofetch`, і цей раунд її не змінював.
