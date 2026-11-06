# Рев'ю: units_check (пункт 145, BACKLOG 6.30)

Режим C. Вердикт: **approve**. Блокерів немає.

## Перевірено сам
- `git status`: змінено лише `docs/BACKLOG.md` і картку постійних вдосконалень; нові `scripts/units_check.py`, `tests/test_units_check.py`, `docs/changes/2026-11-06-units-check.md`. `deploy/`, `scripts/server/`, `src/`, `execution/` не змінені.
- `make lint`: чисто. `uv run pytest -q tests/test_units_check.py`: 6 passed.
- Прогін на репозиторії відтворено: 24 сервіси, 24 таймери, 26 посилань, усі списки порожні. Цифри у змінах і BACKLOG збігаються з виводом.
- Скрипт читає лише `*.service` і `*.timer` з `deploy/user-units/` (`read_text`) та перевіряє `is_file()`. Пише тільки у `--out`. Лише stdlib. Мережі, `subprocess` і `os.environ` немає. `EnvironmentFile=` і `Environment=` не читаються: тест це перевіряє (`secret.env`, `never/read.py`).
- Тести змістовні. Покрито коментарі, продовження рядка, `%h` і `%%`, `bash -c` з `|`, `;` і `{}`, `python -m`, значення опції `--json`, зниклий скрипт, таймер без сервісу, явний `Unit=`, відносний шлях без `WorkingDirectory`, сервіс без `ExecStart`. Негативні випадки перевіряють `failed`.

## Зауваження (не блокують)
1. Низька. `test_main_on_repository_writes_out` не перевіряє код повернення `main`. Якщо юніти розійдуться, тест цього не помітить (так і задумано, але нічний `make test` не стане сторожем). Можна додати окремий тест із `assert uc.main([...]) == 0` на репозиторії.
2. Низька. Перевірка лише на існування файла (в змінах це названо). Ключ `Environment=` з `scripts/...` не перевіряється (за вимогою картки).
3. Інформаційно. Спостереження про `tradedesk-strangle-remind.timer` (разова дата 04.11 минула) подано як рішення власника, `deploy/` не чіпали. Це правильно.
