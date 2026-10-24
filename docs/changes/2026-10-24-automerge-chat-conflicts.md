# 2026-10-24: автозлиття `chat/` і конфлікти в документації

- `scripts/automerge.py`, `.github/workflows/automerge.yml`: гілки `chat/` (сесії Claude з Telegram) зливаються самі, як `agent/` і `claude/`, якщо PR позначено готовим.
- `scripts/pr_watch.py`: готовий PR з конфліктом з `main` (`mergeable_state = dirty`) — злиття `origin/main` у гілку в клоні `~/tradedesk-prfix`; конфлікти, де обидві сторони лише додали рядки в документації, розвʼязуються «спершу `main`, потім гілка» і пушаться без force; решта — повідомлення ⚔️ раз на коміт. Рішення власника 24.10 (Telegram, «Так»).
