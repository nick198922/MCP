# 2026-11-07 — скрипти PM імпортують сусіда звичайним `import` (пункт 226, BACKLOG 6.51)

- `scripts/verify_pm_h11_recheck.py`: `verify_pm_threshold` тепер `import verify_pm_threshold as vt` замість `importlib` (`spec_from_file_location` + `exec_module`).
- `scripts/verify_pm_h7_qlike.py`: `verify_pm_h7_iv` тепер `import verify_pm_h7_iv as VH` (тест далі підміняє `M.VH`); `_load(name)` для відкладеного `verify_pm_modules` у `replay()` — звичайний `importlib.import_module(name)`, бо тест підміняє `M._load`.
- `tests/test_scripts_import_safe.py`: обидва записи прибрано з `ALLOWED` (лишилось 3).
- Числа й вивід скриптів не змінено (`--help` той самий); `src/`, `execution/`, колектори не чіпали.
