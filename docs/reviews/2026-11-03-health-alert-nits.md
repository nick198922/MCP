# Рев'ю (режим C): дрібниці алерту health провайдерів (пункт 32 постійної картки)

**Вердикт: approve.**

## Перевірено самостійно
- `make lint`: чисто. `pytest tests/test_alerts_health.py tests/test_collector.py tests/test_alerts.py`: 43 passed.
- Інваріанти: нових інструментів і мережі немає, `analytics/` не чіпали, `execution/` і колектори даних не змінено (diff лише `alerts/engine.py`, `collector/scheduler.py`, тест, документи).
- (1) `try/except Exception` з `log.warning` навколо `_health_alerts` у `_evaluate_alerts`. Тест `test_health_failure_does_not_block_other_alerts` справді ходить цим шляхом: підміняє `last_alerts_with_prefix` на виняток, провайдер у стані 3 збоїв (тож `_health_alerts` викликає відновлення стану), очікує 1 доставлений алерт `funding_hot` і `health_watch is None`. Без обгортки виняток підняв би весь тік, тест би впав.
- (2) `test_secret_in_detail_never_reaches_message` перевіряє `token`, `apiKey`, `secret` і в `message`, і в `payload()`; покладається на існуючий `redact`, що й було метою зауваження.
- (4) `load_rules` пропускає id з префіксом `health:` з попередженням; імпорт `RULE_PREFIX` усередині функції виправданий циклічним імпортом (`health` імпортує `engine`), `noqa` є. Твердження, що DSL-правила з БД завжди `db:<name>`, збігається з кодом `evaluate_dsl_rules`. Тест перевіряє і пропуск, і збереження звичайного правила.
- (3), (5): зафіксовано в `docs/changes/` і HANDOFF (рядок про 404 є), без зміни коду, що відповідає картці.

## Зауваження (не блокують)
1. **Низька.** `load_rules` викликається щотіку (`scheduler.py:332`), тож попередження про зарезервований id повторюватиметься щохвилини в логу. Шум невеликий і лише при помилковій конфігурації; за бажання логувати один раз.
2. **Низька.** `docs/changes` пише «27 passed» для двох файлів, без `test_alerts.py`; тепер із ним 43. Інформаційно.
3. **Інформаційно.** Позначка `[x]` картки 32 не виставлена (у diff `- [ ] 32.`); координатору виставити після злиття.
