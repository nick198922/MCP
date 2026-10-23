# 2026-10-23 · I1: керування розміром у волті BredoStrategy

- `scripts/research/vault_sizing.py`: шлях плеча лідера волта проти постійного плеча й циклічних зсувів на тих самих дохідностях (рівноважений альт-кошик і власна книга); капітал без заглядання вперед (`anchored_equity`). Тести: `tests/test_vault_sizing.py`.
- Звіт `docs/verification/2026-10-23-vault-sizing-i1.md`: ✗ NOT CONFIRMED. Добори й фіксації не кращі за постійне плече; меншу просадку дав склад монет.
- Оновлено `docs/library/LIB-20261023-cryptobred-vaults.md` (I1) і `docs/knowledge/hyperliquid-vaults.md`.
