# Рецензія: мапа паритету ончейн (пункт 1 картки onchain-parity)

Режим C, лише документи. Вердикт: **approve**.

Перевірено сам:
- Тести `tests/test_idea_cards.py`, `test_owner_cards.py`, `test_docs_links.py`, `test_docs_paths.py`: 24 passed.
- Повнота таблиці проти `docs/reference/doubletop-catalog.md`. Ончейн-інструменти всіх бекендів на місці: Etherscan 8, Helius 5, mempool 5, DefiLlama 7, DexScreener 2, dt_edu 6 (без `provider_readiness` і `whoami`, це службові), CoinStats `wallet_balances`, Pendle 6. Разом 40. Пропущених і переплутаних інструментів немає. Решту CoinStats (`coin_chart`, `coins_markets` тощо) не ончейн, виключення виправдане.
- Статуси проти коду. `dex_pairs`, `contract_risk`, `chain_metrics`, `stablecoin_supply`, `coin_snapshot`, `perps_open_interest`, `pendle_fixed_yields`, `pendle_yield_curve`, `pendle_expiry_calendar` існують. `pendle_market` у рядку 39 не має глибини котирування свопу, тож «нема» чесно. `contract_risk` покриває 7 EVM-мереж (`CHAINS` в `analytics/contract_risk.py`). Код GoPlus 4029 є в `providers/goplus.py`.
- Підсумок збігається з таблицею: є 2 (рядки 27, 30), частково 11 (7, 8, 22, 23, 28, 29, 31, 35–38), нема 27. P1 мають 5 рядків (3, 4, 6, 24, 31).
- Вигаданих цін і лімітів немає. Усе неперевірене позначено «не перевірено». Твердження про Dune (0 кредитів, HTTP 402) і платні мітки Etherscan `nametag` підтверджені `docs/verification/2026-10-25-market-makers-onchain.md` (рядки 17–18, 145–149).

Зауваження (не блокують):
1. Низька. Рядок про те, що `stats/ethprice` «перевірено живцем», у документах підтвердження не має. Збігається лише `scripts/server/onchain-keys.sh`, у якому цей виклик є, а результату перевірки в репозиторії немає. Виправлення: написати «викликається скриптом `onchain-keys.sh`» або вилучити слово «живцем» для `ethprice`.
2. Низька. Рядки 35–38 (Pendle) мають статус «частково» там, де логічніше «є» (рядок 36 прямо каже, що паритет не звірено). Підсумок від цього не страждає, бо рядки поза ончейн-пріоритетами (P3). Якщо захочете, додайте в підсумок примітку, що 4 з 11 «частково» це Pendle.
3. Інформаційно. Рекомендація про порядок пунктів лишається координатору, це в картці сказано явно.

Файли: `/home/tradedesk/tradedesk-agents/docs/ideas/IDEA-20261114-onchain-parity.md`, `/home/tradedesk/tradedesk-agents/docs/changes/2026-11-19-onchain-parity-map.md`.
