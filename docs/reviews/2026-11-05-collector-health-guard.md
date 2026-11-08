# Рев'ю: тест-охоронець джоб колектора (пункт 96, 05.11)

Режим C. Вердикт: **approve**.

Перевірено сам: `git diff`, `git status`, `make lint` (чисто), `uv run pytest -q tests/test_collector_health.py tests/test_dashboard_health.py` (24 passed).

1. Не порожній тест (OK). Джоби беруться зі справжнього `build_default_scheduler` з усіма перемикачами; рівність `jobs == registered` (regex по `PeriodicJob(name=…)` у `scheduler.py`) ловить недосяжні реєстрації. Нова джоба без `Spec`, без причини в `NOT_CHECKED` (ключ — перемикач-власник) і без запису в `EXEMPT` потрапляє в `gaps` і валить тест. Є також перевірки на застарілі записи, на `Spec.flag` не того перемикача та на перемикач без джоби.
2. Зміни лише в тесті й документації (OK). Колектори, `analytics/`, `execution/`, `collector_health.py` не змінено; у `git status` лише тест, BACKLOG, HANDOFF, картка й новий файл у `docs/changes/`. Мережі й БД тест не торкається (`store=object()`, `monkeypatch` повертає оточення).
3. Інваріанти CLAUDE.md (OK). Інструментів і провайдерів не додано; записка в `docs/changes/` є, BACKLOG 6.13 (в) і речення в HANDOFF оновлено.
4. Причини в `EXEMPT` (перевірено частково). `retention` (`_retention_sweep`), `alerts` (`_evaluate_alerts`, коментар про DB-правила), `assistant_snapshot` і умова реєстрації `ohlcv_hist` (потрібен непорожній `MARKETS`) відповідають `scheduler.py`; у `deploy/features.env` рядка `OHLCV_HIST` немає. Не перевірено: твердження, що вкладка брифу панелі показує застарілий файл як порожній стан.
5. Низька, без блокування. Тест читає `config.py` і `scheduler.py` regex-ами: якщо перемикач перестануть писати літералом `"TRADEDESK_…_ENABLED"`, він його не побачить мовчки. Захист частковий (`jobs == registered`), тож для джоб ризик закритий; для перемикачів можна додати перевірку, що знайдено не менше відомого числа.
