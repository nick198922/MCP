# 2026-11-10 · Найповільніші тести, четвертий прохід (пункт 304)

Пункт 304 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md). Попередні проходи: [06.11](2026-11-06-slow-tests.md), [07.11](2026-11-07-slow-tests.md), [07.11-2](2026-11-07-slow-tests-2.md), [07.11 ast](2026-11-07-test-speed.md). Змінено лише `tests/test_tool_failure_extra_7.py`. `src/`, `execution/` і `collector/` не чіпав.

- **Замір до змін.** `uv run pytest -q --durations=25` без xdist: 8463 passed, 3 skipped за 1017 с. Машину ділили інші процеси, тож поодинокі часи в повному прогоні на 30–80 % більші, ніж в окремому запуску (`deribit_vrp`: 7.7 с у прогоні проти 4.3 с окремо). Перший за часом — `test_s3_known_result_in_sample` (34 с), але він має позначку `slow`. 10 найповільніших без нього:
  - `test_scripts_import_without_side_effects` 9.6 с;
  - `test_no_naive_time_outside_allowed` 8.0 с;
  - `test_tools_wrong_type_fields[deribit_vrp]` 7.7 с;
  - `test_deps_check::test_main_on_repository` 7.2 с;
  - `test_carry_paper_engine`: обидва no-lookahead (7.0 і 6.8 с), engine B (6.7 с), engine A (5.4 с);
  - `test_calculators_extreme_but_finite_inputs_answer` 5.1 с;
  - `test_sieve_batch14::test_causal[bollinger_reentry_fade]` 5.1 с.
- **Прискорено `test_calculators_extreme_but_finite_inputs_answer`:** 5.12 → 0.62 с (`-n 0`). Тест слав `odds_devig` і `fib_retracement` списки зі 100 000 елементів. Близько 4.5 с ішло на `round()` півмільйона рівнів масштабу 1e308 і на повні обходи `first_nonfinite`. Тепер списки мають 10 000 елементів (константа `LONG`). Асерти ті самі. Жоден інструмент не обмежує довжину списку (`odds` має лише `min_length=2`, `levels` не має меж), а кожен крок над списком лінійний, тож 10k проходить той самий код, що й 100k.
- **Не змінено**, бо причина не в тесті (як у BACKLOG 6.37):
  - `test_carry_paper_engine`: прогін рушія вже робиться раз на процес. За профілем 4.7 з 7.3 с забирає `analytics/carry.py` (`_Leg.__init__`, 3696 викликів);
  - сканери `ast` (`test_scripts_import_safe`, `test_naive_datetime`): час іде на `compile` / `ast.walk` по ≈ 620 файлах, префільтри вже стоять;
  - `deribit_vrp` (`vrp_summary` у `src/`), `test_deps_check` (`scripts/deps_check.py`), `test_causal` (специфікації сита в `scripts/`).
- **Розглянуто й відкинуто:** у файлах `test_tool_failure_extra*` (875 тестів, 223 с при `-n 0`) кожен тест сам по собі швидкий (≈ 0.25 с), але більшість часу йде на свіжий сервер і MCP-сесію в кожному тесті. `build_server` з уже теплою пам'яттю реєстрації забирає 38 мс. З них 4.5 мс припадає на `deepcopy` схем, решта — на SDK, тож переписувати пам'ять у `conftest.py` заради ≈ 1 % не варто.
- **`make test` після змін:** 8462 passed, 3 skipped за **171.2 с** (стіна, `-n auto`). Окремого `make test` до змін немає: за завданням можна лише два повні прогони, а перший пройшов без xdist. Зекономлено ≈ 4.5 с процесорного часу, тобто менше 1 с стіни на 6 воркерах. Це менше за шум від навантаження сервера. `make lint` чисто, змінений файл — 97 passed.
