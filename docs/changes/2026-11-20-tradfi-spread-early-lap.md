# 2026-11-20 — ранній кіл (б) картки tradfi-perp-offhours-reopen: реєстрація

- Картка [tradfi-perp-offhours-reopen](../ideas/IDEA-20261024-tradfi-perp-offhours-reopen.md): у «Попередній реєстрації» записано правило раннього кілу за форвардними витратами (78 імен G3, 20 сесій NYSE 26.10–20.11, тейкер 0.09 % + повний спред о 09:25 і 10:30 ET, поріг 0.20 % на ≥ 70 % імен, правила покриття). Результатів ще не дивилися.
- `analytics/tradfi_spread.py`: чисті `covered_sessions` і `early_kill` (рішення `reject` / `continue` / `wait`).
- `scripts/verify_tradfi_spread_early_lap.py`: читає `tradfi_spread` лише `mode=ro`, вантажить тільки 5-хвилинні as-of вікна; `--stage smoke` (покриття, без витрат) і `--stage run` (зареєстрований прогін).
- Тест `tests/test_tradfi_spread_early_lap.py` (без мережі): поріг включно, покриття, as-of і cutoff, перехід на зимовий час.
- Smoke: 19 сесій 26.10–19.11 по 78/78 імен, санітарні лічильники нульові; прогін — наступним кроком після 16:00 UTC 20.11.
