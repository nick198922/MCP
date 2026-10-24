# 2026-10-24: паперова копія двох волтів HL

- `scripts/copy_vaults.py` + `deploy/user-units/tradedesk-copy-vaults*`: Delta_01 і Jade Lotus Capital копіюються кожні 2 хв на папері. Книга $10 000 на волт, ціни Bybit (тейкер за bid/ask, комісія, funding). Щотижня — копія проти волта. Вибір власника «А» 24.10.
- Чому не на демо-рахунку: одна позиція на символ у Bybit змішала б копію з угодами S3.
- `scripts/vault_watch_tg.py`: `since(..., window)` — дрібніші точки `month`.
