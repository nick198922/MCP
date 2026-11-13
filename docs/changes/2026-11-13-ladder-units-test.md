# 2026-11-13 · Тест unit-файлів таймера ladder (пункт 370)

Пункт 370 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md), зауваження 1 [рев'ю таймера ladder](../reviews/2026-11-13-ladder-weekly-timer.md). Додано лише `tests/test_ladder_weekly_units.py`; unit-файли, скрипти й `execution/` не чіпав.

- **Що перевіряє:** у `deploy/user-units/tradedesk-ladder-weekly.service` рівно один `ExecStart`, він запускає `scripts/paper_ladder_snapshot.py` з одним `--out-dir`, і цей шлях дорівнює `LADDER_SERVER` у `scripts/checkpoint_report.py` (куди таймер пише, звідти звіт і читає); у `.timer` — `OnCalendar=Sun *-*-* 12:00:00 Europe/Kyiv` і `Persistent=true`.
- **Мутаційна перевірка** (скрипт поза репозиторієм, на змінених копіях тексту): інший шлях у unit, інший `LADDER_SERVER`, перейменований скрипт, без `--out-dir`, субота замість неділі, `Persistent=false` — у всіх шести випадках тест падає; без змін проходить.
- `make lint` чисто; `uv run pytest -q tests/test_ladder_weekly_units.py`: 1 passed.
