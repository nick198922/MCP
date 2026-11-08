# 2026-11-06 · Найповільніші тести: реальні паузи лімітерів у тестах

Пункт 131 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md). Змінено лише `tests/`, `src/` і `scripts/` не чіпали, тести не видалено, асерти не змінено.

- **Що виміряно.** Повний набір без xdist (`pytest -n 0 --durations=40`, разом зі `slow`): 5159 passed, 3 skipped, 1110 с; поруч ішли ще 1–2 тестові процеси. Окремо — прогін без `slow` із плагіном, що рахує запитані паузи `asyncio.sleep` / `time.sleep` і очікування `TokenBucket` на кожен тест (5160 тестів, сума часу тестів 845 с). Висновок: `time.sleep` у тестах немає, а єдина реальна пауза — **лімітери провайдерів**. Продакшн-ліміти (GoPlus 0.2 запиту/с, DefiLlama stablecoins 1/с, ForexFactory 0.5/с, Polymarket 5/с, Deribit 8/с) змушували тести чекати на фейкових транспортах у реальному часі, разом ≈ 38 с очікування токенів за прогін.
- **Що змінено.** Runtime у тестах будується під `patch("tradedesk.core.http.TokenBucket", …TokenBucket(1000, 1000))`. Такий прийом уже є в `test_tools_bad_input` і десятку інших тестів. Лімітер самого `Http` у цих тестах не перевіряється, його тест (`test_core::test_token_bucket_throttles`) не змінено.
  1. `tests/test_provider_lifecycle.py`: Runtime тесту `test_every_provider_survives_runtime_aclose`. Третя сесія чекала 5 с на GoPlus.
  2. `tests/test_pm_gap.py`: хелпер `_rt` (книги Polymarket і Deribit у кожному виклику інструмента).
  3. `tests/test_assistant_snapshot.py`: хелпер `_rt` (другий знімок у тесті чекав на ForexFactory, DefiLlama й Deribit).
  4. `tests/test_stablecoin_supply.py`: новий хелпер `_runtime` для `make_server` і тесту `aclose`. Тим самим прискорено й `tests/test_dashboard_stablecoins.py`, який будує сервер через `make_server`.
- **До / після** (послідовно, `-n 0`, по файлах; «до» міряно, коли поруч ішов ще один тестовий процес, «після» — на вільній машині):

  | Файл | До | Після | Найдовший тест: до → після |
  |---|---|---|---|
  | `test_provider_lifecycle.py` | 5.95 с | 0.59 с | `every_provider_survives_runtime_aclose` 5.03 → 0.04 с |
  | `test_pm_gap.py` | 21.0 с | 12.3–13.5 с | `tool_lists_gaps_after_costs_and_depth_filter` 4.9 → 2.3 с |
  | `test_assistant_snapshot.py` | 23.0 с | 16.4 с | `my_options_failure_…` 4.8 → 1.6–1.8 с; `stablecoins_failure_…` 3.1 с → поза п'ятіркою (< 1.3 с) |
  | `test_stablecoin_supply.py` + `test_dashboard_stablecoins.py` | 9.7 с | 2.3 с | `provider_failure` 2.4 → 0.4 с; `stables_fold_on_provider_failure…` 2.3 → 0.1 с; `survives_runtime_aclose` 2.1 → 0.1 с |

  Разом ці файли: ≈ 59.6 → ≈ 32 с. Окремий прогін «до» не повторено: `git stash` у цьому запуску заборонено. Числа «до» окремих тестів збігаються з повним прогоном: `lifecycle` 5.02–5.03 с у трьох прогонах.
- **Нестабільні тести:**
  - `test_my_options.py::test_collect_happy_path_only_reads` брав справжній годинник. Опціони фікстури (`BTC-27NOV26-…`) експіруються 27.11.2026 о 08:00 UTC, після цього `theta_usd_day` стає `None` і асерт `< 0` падає з `TypeError`. Перевірено підміною годинника на 28.11. **Виправлено:** годинник скрипта (`bop._now`) і `my_options_view` беруть час фікстури `ASOF`.
  - `test_strategy_sieve.py::test_s3_trades_equal_the_stop_management_study` (7–10 с) залежить від локального кешу `~/.cache/tradedesk-agents`. Він іде лише на сервері, у CI пропускається. Тест не нестабільний, але залежить від машини.
  - Чекання першого тіку планувальника в реальному часі: `test_collector.py`, до 50 × 0.05 с, і `test_logfmt.py`, до 50 × 0.02 с. За сильного навантаження можуть впасти. У трьох повних прогонах цього дня не падали.
  - Тестів, що залежать від порядку чи мережі, не знайдено. `test_tools_provider_outage` і далі ловить запити в справжню мережу.
- **Лишилось (не цей пункт):**
  - паузи лімітерів у `test_tools_provider_outage.py` (≈ 2.2 с) і `test_flow_snapshot.py` (≈ 2.1 с);
  - `Store.init` через aiosqlite ≈ 0.2 с на кожну нову базу. Спільний шаблон бази змінив би, які шляхи міграцій покривають тести;
  - повний прогін паперового carry (`test_carry_paper_engine`, ≈ 5 с на стратегію) рахується в кожному воркері xdist. Тест перевіряє саме цей прогін.
