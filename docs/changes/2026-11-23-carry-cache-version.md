# verify_carry_b_leg_gap: версія формату в імені кешу барів (23.11)

Пункт 448 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md): ім'я кеш-файлу барів тепер починається з `CACHE_VERSION = "v2"` (`cache_path()`), тож файли до 23.11 (невідомий обсяг → `0.0`, хибний «R1 zero volume») більше не читаються; `bars()` лишає невідомі open/high/low `None` (рядок без close, як і раніше, відкидається), `row_checks` їх не використовує.
Тести без мережі в `tests/test_carry_leg_gap.py` (ім'я містить версію; файл без версії не читається; None зберігається через кеш): `uv run pytest -q tests/test_carry_leg_gap.py` — 16 passed, `make lint` чисто. Наслідок: наступний прогін без `--no-fetch` заново завантажить бари; з `--no-fetch` старий кеш дасть «◯ no data».
