# 2026-11-06: перевірка розбиття тесту пропусків GEX (пункт 161)

- Пункт 161 уже виконано раніше: `test_gex_by_strike_skips_rows_without_iv_time_or_oi` у `tests/test_analytics_forward_inputs.py` розбито на п'ять випадків `parametrize` з назвами (`no_iv`, `zero_iv`, `expired`, `zero_oi`, `negative_oi`) у коміті a020e4da ([зміни 05.11](2026-11-05-review-nits-0405.md), [рев'ю, п. 4](../reviews/2026-11-05-forward-inputs-tests.md)). Тест і `src/` цього разу не змінювались.
- Додано лише ручну перевірку з картки: умову пропуску в `gex_by_strike` п'ять разів підмінено в пам'яті (файли `src/` не чіпались), по одній умові. Щоразу падає рівно один випадок з відповідною назвою, решта чотири проходять.
- `make lint` чистий; `tests/test_analytics_forward_inputs.py`: 44 passed.
