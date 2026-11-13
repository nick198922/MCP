# Щотижневий знімок ladder Bybit без людини

Пункт 366 постійної картки [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md); причина — пропущені щотижневі знімки між 17.10 і 13.11 ([нотатка пункту 365](2026-11-13-carry-criteria-e-ye.md)).

- User-таймер `deploy/user-units/tradedesk-ladder-weekly.timer` / `.service`: щонеділі о 12:00 за Києвом (`Persistent=true`, пропущену неділю доганяє після перезавантаження) запускає `scripts/paper_ladder_snapshot.py --out-dir /var/lib/tradedesk-mcp/ladder`. Публічний ендпоінт Bybit, без ключів, без `execution/`, без root; ставить і вмикає `scripts/user_units_sync.sh` (через `tradedesk-update`), перемикача немає.
- `paper_ladder_snapshot.py`: новий `--out-dir DIR` (взаємовиключний з `--out`) пише `DIR/<дата UTC>-bybit.json` і створює каталог; дата — з того самого моменту, що й `taken_utc`.
- `scripts/checkpoint_report.py`: `load_ladder` читає `docs/paper/ladder/` і каталог таймера (`TRADEDESK_LADDER_DIR`, типово `/var/lib/tradedesk-mcp/ladder`; `ladder_dirs()`). Та сама дата в обох місцях — один запис, з `docs/paper/ladder/`; якщо та копія не читається, береться друга (з приміткою). Каталогу нема — не помилка. Результат відсортовано за датою.
- Тести: злиття й сортування двох каталогів, дублікат дня, зіпсована копія з запасною, відсутній каталог (`tests/test_checkpoint_report.py`); `--out-dir` з датою UTC і взаємовиключність (`tests/test_forward_scripts.py`). `tests/conftest.py` спрямовує `TRADEDESK_LADDER_DIR` у неіснуючий шлях, щоб тести не залежали від файлів сервера.
- Живий прогін 13.11 (у кеш, не в `/var/lib`): 63 сторінки, 829 USDT-перпів, нога $2 000 при 2x вміщується у всіх 829, MMR рівня 0.0033–0.125, медіана 0.02. `load_ladder` з цим каталогом: 2026-10-17 і 2026-11-13 (копія з git, 827 символів), помилок нема.
- Обмеження: 30.11 — понеділок, тож останній знімок таймера до розбору — 29.11 (на день старший); знімок того ж дня, якого вимагає реєстрація, лишається ручним кроком.
