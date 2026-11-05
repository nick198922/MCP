# Рев'ю (режим C): rss_mb dedupe, 3-тя партія (2026-11-05)

**Вердикт: approve.**

Перевірено сам: `git diff`, `make lint` (чисто), `uv run pytest -q tests/test_common_helpers.py tests/test_forward_scripts.py tests/test_verify_pm_h7_qlike.py` (104 passed).

## Перевірено

1. Поведінка. `common_helpers.rss_mb` = `ru_maxrss / 1024`. Для `verify_option_modules` і `verify_pm_h7_qlike` старе тіло було `/ 1024.0`: те саме число. Інші чотири файли мали обгортку `v0.rss_mb()`, а `v0.rss_mb` у `verify_news_events.py:45` вже є `from common_helpers import rss_mb`. Формат виводу (`:.0f`, `round()`) не змінився.
2. Імпорти за режимом запуску. П'ять скриптів (`verify_option_modules`, `_0b`, `_0c`, `_0d`, `verify_market_makers_news`) мають `sys.path.insert(0, scripts/)` до нового імпорту. `verify_pm_h7_qlike` уже тримав `import common_helpers as cmn` у тому самому місці. Тому імпорт працює і при `python scripts/x.py`.
3. Прибрані імпорти. У `verify_option_modules` і `verify_pm_h7_qlike` `import resource` прибрано, а `resource.` у файлах більше немає. ruff чистий. У решти `v0` ще потрібен: `v0.rd`, `v0.wr` та інші.
4. Зовнішні споживачі. `stage0_condor_forward` бере `verify_option_modules` як `vom`, але `vom.rss_mb` не викликає. Тести `test_forward_scripts` (`verify_option_modules.load`) проходять.
5. `verify_carry_a_entry_ban` справді лишено: `-> int`, `// 1024`, друкує без форматування. Замінити його означало б змінити вивід.
6. Тест. Шість модулів додано до `RSS_MB_IMPORTED`; перевіряє тотожність із хелпером і відсутність `resource`.
7. Документи. Заява «лишилась 1 локальна копія» правдива для `def rss_mb`. Область обмежена скриптами, тестом і документами; `src/`, `execution/`, `registry.csv` не змінено. Запис у `docs/changes/` є.

## Зауваження (не блокують)

1. Низька. У `scripts/verify_deviation_weekly_trend.py:62` є `rss_mb = s7.rss_mb`: аліас, а не копія. Формулювання «лишилась 1» у BACKLOG/HANDOFF/картці стосується локальних `def`. Можна додати слова «(не рахуючи аліасів)». Дію не вимагає.
2. Низька. У `verify_pm_h7_qlike` новий `from common_helpers import rss_mb` стоїть без `# noqa: E402`, але ruff проходить, бо імпорт у верхньому блоці. Все гаразд.
