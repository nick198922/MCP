# Рев'ю: дедуплікація scripts/, коло 10 (пункт 41 постійної картки)

**Вердикт: approve**

Перевірив сам:

1. Тіла ідентичні. `common_helpers.read_json` це `json.loads(p.read_text())`, як і видалені `rd`. `ymd_ms` це `datetime.fromtimestamp(ms/1000, UTC).strftime("%Y-%m-%d")`, як `_d` у demo_vs_backtest і `day_of` у market_makers_depth. Різниця лише `timezone.utc` замість `UTC`, а це той самий об'єкт. Висновок: вивід не змінюється.
2. Залишкових посилань на видалені імена немає. Grep по `scripts/` і `tests/` не знайшов `rd(`, `_d(` чи `day_of(` у шести змінених файлах. `day_of` у `verify_holders_revenue.py` і `verify_token_unlocks.py` це інша функція (індекс дня), її не чіпали. `rd` в інших файлах (`verify_news_events`, `verify_market_flow_reader`, `s1.rd` у `verify_setup_passport`/`verify_short_setups`) стосується їхніх власних або не змінених визначень. Шість змінених модулів ніхто не імпортує, крім `tests/test_demo_shadow.py`, а той не використовує `_d`.
3. Порядок імпортів і noqa в порядку. `ruff check` і `ruff format --check` проходять (`make lint`: All checks passed, 2593 файли відформатовано). `# noqa: E402` стоїть лише там, де імпорт іде після `sys.path.insert` (`verify_taker_imbalance`). В інших `common_helpers` лежить у блоці до `tradedesk.*`, що відповідає наявній практиці.
4. `uv run pytest -q tests/test_common_helpers.py tests/test_demo_vs_backtest.py`: 74 passed.
5. Межі дотримано. `git status` показує змінені лише шість скриптів у `scripts/`, документи та новий `docs/changes/2026-11-03-scripts-dedup-10.md`. Нічого в `execution/`, колекторах, заморожених форвардах, `scripts/server/` чи `agents_*` не змінено.

Зауваження (низька важливість, без вимог): частину рядків ruff-format переніс на кілька рядків (`liquid_pairs`, `ctrl[coin] = Pair(...)`), це лише форматування, а не зміна поведінки.

Файли: `/home/tradedesk/tradedesk-agents/scripts/common_helpers.py`, `/home/tradedesk/tradedesk-agents/docs/changes/2026-11-03-scripts-dedup-10.md`.
