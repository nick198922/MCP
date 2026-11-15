# 2026-11-15 — `deribit_atm_term` злито в `deribit_skew_term` (картка mcp-cleanup, пункт 2, перше злиття)

Рішення власника Q-20261114-1 (А1), картка [`IDEA-20261114-mcp-cleanup`](../ideas/IDEA-20261114-mcp-cleanup.md). Інструмент `deribit_atm_term` прибрано з MCP; ту саму строкову структуру ATM IV тепер віддає `deribit_skew_term` з параметром `view="atm"`. Інструментів було 105, стало 104.

## Стара → нова

| Було | Стало |
|---|---|
| `deribit_atm_term(currency?, min_days?, max_days?)` | `deribit_skew_term(currency?, min_days?, max_days?, view="atm")` |

Типові значення ті самі: `currency="BTC"`, `min_days=0.5`, `max_days=400`. `target_delta` у `view="atm"` ігнорується. Без `view` (або `view="skew"`) `deribit_skew_term` відповідає, як і раніше (RR / fly, `most_extreme`, `rr_shape`).

Відповідь `view="atm"` — та сама, що в старого інструмента, без додаткових ключів: `data = {currency, shape, expiries}`, рядок `expiries[]` = `{expiry, days, forward, atm_iv, strikes}`; `shape` = `contango` / `backwardation` / `flat` (ATM IV останньої експірації проти першої; `null`, якщо експірація одна). Порожнє вікно — `ok=false`, у `missing` запис `atm_term` / `data_unavailable` / «no priced expiries in [min_days, max_days]», як і раніше.

## Що змінилось у поведінці

- Числа ті самі: той самий код (функція `_atm_term` у `tools/options.py` — тіло старого інструмента без змін) на тій самій книзі Deribit. Перевірено на фікстурах тестів до і після злиття: чотири виклики (типові параметри; 0–400 д; ETH 20–60 д з однією експірацією і `shape=null`; порожнє вікно 1000–2000 д) дали побайтно ту саму відповідь. Еталонні значення (30OCT26 / 27NOV26 / 26MAR27: ATM IV 0.4011 / 0.401 / 0.4002, форварди 82915.5 / 83246.5 / 84736.0, по 17 страйків, `backwardation`) закріплено в `tests/test_server.py::test_deribit_skew_term_atm_view` з допуском.
- Різниця між видами, яка лишилась навмисно: `view="skew"` пропускає експірації, де посмішка не досягає цільової дельти (немає RR), а `view="atm"` показує всі експірації з посмішкою (≥ 3 страйки) — як старий `deribit_atm_term`. ATM IV на спільних експіраціях однакові (одна функція `atm_iv`), це теж перевіряє тест.
- Живцем 15.11 (`build_server()`, Deribit): BTC і ETH — по 11 експірацій в обох видах, `shape=backwardation`, ATM IV на всіх спільних експіраціях збігаються (BTC 16NOV26 0.6357 → 24SEP27 0.4154; ETH 0.7894 → 0.548).
- Збій Deribit: як і раніше, `ok=false`, `deribit` у `missing` (`tests/test_provider_failures.py` покриває обидва види).

## Хто викликав стару назву

- Переведено на `deribit_skew_term(view="atm")`: `scripts/option_ideas_tg.py` (`market_for`, умови P10 і вибір місячної експірації для P15), `src/tradedesk/collector/assistant_snapshot.py` (секція `term` знімка асистента: вибір експірацій тижня / місяця / найближчої і перелік ланцюгів), `scripts/live_smoke.py`.
- Оновлено згадки: промпти `fomc_playbook.md`, `morning_briefing.md`, `pre_trade_checklist.md`; `docs/knowledge/options-playbook.md` (таблиця «що міряємо», умова P10); докстрінги `analytics/option_ideas.py` і `analytics/calendar_p10.py`; рядок «як міряється» правила P10 у `option_ideas.RULES` (текст для власника); `README.md`, `docs/PARITY.md`; тести `test_server`, `test_provider_failures`, `test_option_ideas`.
- Чисті функції не чіпали: `analytics/options.py` (`atm_iv`, `build_smile`), `analytics/calendar_p10.py`, `analytics/option_ideas.py` рахують так само.
- Старі назви лишились лише в історичних записах (`CHANGELOG.md`, `docs/history/`, `docs/changes/`, `docs/reviews/`) і в картках ідей — їх не переписуємо.
- Служба MCP і таймери на сервері підхоплять зміну після злиття гілки й перезапуску; до того `option_ideas_tg.py` і знімок асистента з нового коду викликатимуть новий маршрут у власному процесі (`build_server()`), тож розбіжності версій нема.
- `CLAUDE.md` досі каже «109 шт.» — застаріло, правка за власником.
