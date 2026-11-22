**Крок:** p, картка onchain-parity (Порядок 2), пункт 2 (EVM, Etherscan v2): картки з Порядком 0–1 заблоковані (чекають часу, даних сервера, питання власнику або гілки x-btc-charlie).

**Результат:**
• Чотири нові інструменти `onchain_balance`, `onchain_address_txs`, `onchain_token_info`, `onchain_contract_source` (мережі: ethereum, bsc, polygon, arbitrum, optimism, base, avalanche); провайдер `providers/etherscan.py`, ліміт 2 запити/с.
• Топ-холдери не зроблено: на Etherscan це платний Pro (є `contract_risk`).
• Без ключа в службі інструменти повертають `ok=false`, `missing: config`: служба не бачить `~/.config/tradedesk/onchain.env`. Питання власнику Q-20261119-1 і скрипт `scripts/server/etherscan-key.sh`.
• Фікстура зроблена за документацією, не жива; пункт 2 лишається `- [ ]` до ключа, живої перевірки й живого зразка.
• Перевірено мною: `make lint` чистий, `make test` 10674 passed, 3 skipped.

**Критик:** C (opus) спершу request changes (кеш тимчасових збоїв `eth_call` як «не ERC-20»; не було питання про ключ), після виправлень (opus, sonnet) — approve, `docs/reviews/2026-11-19-etherscan-onchain-tools.md`. Не взято: повтор при rate-limit у тілі відповіді.

**Далі:** `owner`. Питання: Q-20261119-1 (ключ Etherscan у службі). Не взято: options-playbook п. 4 (черга порожня), agents-mcp-access (Q-20261114-2), x-traders (чекає гілку), structural-edges 8f (до 2027-02), demo-setups 6 (S38 28 < 30), book-walls (дані й 15.12), standing-improvements (усі три пункти заблоковані — сервер / дата).
