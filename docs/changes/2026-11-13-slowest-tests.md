# 2026-11-13 · Найповільніші тести (пункт 371)

Пункт 371 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md). Змінено лише `tests/test_tools_wrong_type_fields.py`; `src/`, `execution/`, правила рушіїв і мережевий код не чіпав.

- **Замір:** `uv run pytest -q --durations=30 -n 0` по всьому набору (разом із `slow`): 9965 passed, 3 skipped, 1 failed (див. нижче), 1323 с послідовно на 6-ядерному сервері (load ~1.4).
- **10 найповільніших:**
  1. 35.3 с `test_strategy_sieve::test_s3_known_result_in_sample` (позначений `slow`, у `make test` не входить);
  2. 8.8 с `test_naive_datetime::test_no_naive_time_outside_allowed`;
  3. 7.7 с `test_deps_check::test_main_on_repository`;
  4. 7.5 с `test_carry_paper_engine::test_stopped_a_opens_nothing_and_closes_held_positions_as_without_the_stop`;
  5. 6.4 с `test_carry_paper_engine::test_no_lookahead_truncated_source_gives_the_same_cycles[B]`;
  6. 6.3 с `test_scripts_import_safe::test_scripts_import_without_side_effects`;
  7. 6.1 с `test_carry_paper_engine::test_engine_b_equals_backtest_with_stops`;
  8. 5.8 с `test_carry_paper_engine::test_no_lookahead_truncated_source_gives_the_same_cycles[A]`;
  9. 5.4 с `test_sieve_batch14::test_causal[bollinger_reentry_fade]`;
  10. 5.2 с `test_carry_paper_engine::test_engine_a_equals_backtest`.
- **Що змінено:** `test_no_tool_raises_on_a_wrong_type_field[deribit_vrp]` кликав інструмент ~43 рази з типовою історією 1000 днів (~0.09 с чистого обчислення VRP на виклик). Тепер `history_days=400` лише в цьому файлі (`TOOL_ARGS`): 400 > 365-денне вікно перцентилів + 30-денний стредл, тож відповідь має ті самі розділи. Перевірено скриптом: ті самі назви полів, ті самі 40 вибраних мутацій, ті самі ключі відповіді; змінились лише кількості рядків (401/400 замість 1001/1000, `last` лишається окремим рядком). Час: 4.5–4.8 с → 2.35 с (окремо, `-n 0`). Файл цілком: 182 passed.
- **Мутаційна перевірка:** тимчасово `_dvol_row` у `tools/options.py` без перевірки (`int(x[0]), float(x[4])`) — тест падає з 6 збоями (`abc`/`null`/`[]`, зокрема `@last`); мутацію відкочено, `src/` без змін.
- **Решта > 3 с — потрібні й довгі по суті:**
  - `test_naive_datetime`, `test_deps_check`, `test_network_boundary` (5.1 с), `test_store::test_every_store_write_goes_through_write_block` (3.7 с): `ast.parse` коду `src/tradedesk/` і `scripts/` (651 файл, 9.7 МБ; у `test_naive_datetime` 263 файли проходять текстовий префільтр, і їхній парсинг займає ≈ 7–8 с із 8; у `test_network_boundary` і `test_store` — лише `scripts/`); префільтри вже є, звужувати їх означає послабити детектор.
  - `test_carry_paper_engine` (5 тестів): прогін рушія по 307 циклах 110-денного сценарію. Повний прогін уже спільний (`full_run_snapshot`, раз на стратегію й процес), тому 5 с платить перший тест; `no_lookahead` і «зупинена A» мусять мати власний прогін (обрізаний фід / стоп входів). 110 днів потрібні для ≥ 3 закритих угод і стопу в B.
  - `test_scripts_import_safe`, `test_scripts_import::test_guarded_scripts_import_without_side_effects` (4.4 с), `test_forward_scripts::test_stage0_condor_*` (3.3–3.7 с): окремі процеси Python (імпорт скриптів без побічних дій, код виходу); суть тесту в процесі.
  - `test_sieve_batch14/9::test_causal`, `test_setup_signals` (s14, s23, live_scan: 3.3–5.1 с кожен), `test_sfp_backtest::test_walk_forward_signals_do_not_depend_on_future_bars` (4.5 с): перевірка причинності на десятках префіксів, O(n²) за побудовою; вже розбиті на параметри для xdist. Менше префіксів = слабша перевірка.
  - `test_tools_bad_input::test_no_tool_raises_on_absurd_input` (4.8 с): ≥ 500 викликів по всьому реєстру інструментів, суть — повнота обходу.
  - `test_tools_wrong_type_fields[cycle_regime]` (3.5 с окремо): 42 виклики по ~80 мс на реальному зразку денних свічок; `[chart_markup]` (5.0 с у повному прогоні) і `test_tool_failure_extra_14::test_a_corrupt_live_bar_*` (4.1 с) окремо 2.3 с і ≤ 2.1 с: у повному послідовному прогоні їх розганяє шум (GC великої купи, навантаження сервера).
- **Побічне спостереження (не виправлено, поза пунктом):** `test_tool_failure_extra_13::test_dsl_bounds_themselves_still_evaluate` має межу `time.monotonic() - t0 < 2` і впав у послідовному прогоні (2.65 с), окремо — 0.07 с. У `make test` (xdist) не падав; ризик флейку лише при `-n 0` на всьому наборі.
- `make lint` чисто; `uv run pytest -q -n auto tests/test_tools_wrong_type_fields.py`: 182 passed.
