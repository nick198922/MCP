# Рев'ю: scripts-dedup-5 (пункт 36 постійної картки, `rss_mb`)

Режим C, незакомічений diff. **Вердикт: approve.**

## Що перевірено особисто
- `git diff`: 14 файлів. Змінено `scripts/common_helpers.py`, 10 `verify_*.py`, `tests/test_common_helpers.py`, `docs/BACKLOG.md` і картку; `docs/changes/2026-11-03-scripts-dedup-5.md` новий. Заморожені форварди, `execution/`, колектори, `src/` не чіпані (`git status` по цих шляхах чистий).
- Поведінка: усі видалені копії були `getrusage(RUSAGE_SELF).ru_maxrss / 1024(.0)`. Спільна версія ділить на `1024`, а в Python 3 `/` завжди дає float, тому значення те саме. Формати виводу (`:.0f`) і текст рядків не змінені. Тільки в `verify_pm_modules.py` один рядок `print` обгорнуто в дужки через довжину, текст той самий.
- Імпорти зі змінених скриптів: Grep `\.rss_mb|import.*rss_mb|from verify_\w+ import` по `scripts/`, `tests/`, `src/`. Жоден скрипт не бере `rss_mb` зі змінених 10. Псевдоніми `vsp`, `v0`, `dl`, `dsm`, `s7`, `base`, `s1` вказують на `verify_setup_passport`, `verify_news_events`, `verify_carry_a_delisting`, `verify_demo_stop_management`, `verify_course_step7_basics`, `verify_session_stop_hunt`, `verify_market_makers_onchain`, `verify_course_s14_ema`, жоден із них не в списку змін. `verify_pm_h7_qlike` лишає власний `rss_mb` і не залежить від зміненого.
- Імпорт усіх шести скриптів, які тест не покриває (`carry_cost_calibration`, `paper_carry_bench`, `pm_h11_recheck`, `pm_h7_iv`, `pm_modules`, `smart_money`), пройшов: власного `rss_mb` нема, `cmn.rss_mb()` > 0.
- `make lint`: чисто (ruff check, 2578 файлів format OK).
- `tests/test_common_helpers.py`: 15 passed.

## Знахідки
1. (низька) Тест `test_scripts_use_the_shared_rss_mb` покриває 4 із 10 скриптів. Решту покриває лише ручний імпорт з опису, у CI його нема. Не блокує: помилка тут дала б `NameError` при першому `log`, а `ruff` (F821) її вже ловить. За бажання розширити parametrize на всі 10.
2. (інформаційна) Тест на підміненому `getrusage` змінює `cmn.resource.getrusage`, тобто глобальний модуль `resource`. `monkeypatch` відновлює його після тесту, тож проблеми немає.

Змін у коді не потрібно.
