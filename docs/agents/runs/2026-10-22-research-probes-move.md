**Крок e (BACKLOG, технічний борг).** Картки з пріоритетом власника зараз заблоковані (час, ключі, ручні кроки Claude; картка асистента вже змінена відкритою гілкою `radar-tab-situation`), нових матеріалів бібліотеки нема, звіт закликів свіжий (21.10), карток `proposed`/`accepted` достатньо.

**Що зроблено:** п'ять дослідницьких зондів закритих карток (`probe_hl_depth`, `probe_dated_basis`, `probe_token_unlocks`, `probe_deribit_history`, `verify_pm_ts_check`) скопійовано в `scripts/research/` (змінено лише рядок Usage; псевдонім `verify_pm_ts_check` шукає `verify_pm_yes_calibration.py` рівнем вище). Пункт BACKLOG закрито, запис у HANDOFF і `docs/changes/`.

**Перевірка:** `make lint` чисто, `make test` — 1708 passed, 2 skipped. Жоден тест, Makefile чи скрипт не посилається на старі шляхи. Окремого рев'ю критика не було: зміна лише переносить скрипти, без логіки.

**Що лишилось власнику:** агент не може видалити відстежувані файли, тож старі копії ще в `scripts/`. Після злиття: `git rm scripts/probe_hl_depth.py scripts/probe_dated_basis.py scripts/probe_token_unlocks.py scripts/probe_deribit_history.py scripts/verify_pm_ts_check.py`.

**Далі:** `owner`. Нових питань нема. Гілка `radar-tab-situation` досі чекає на власника.
