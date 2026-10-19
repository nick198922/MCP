# Obsidian для бази знань

Документація проєкту — Markdown у git (`docs/knowledge/`, `docs/library/`, `docs/ideas/`, звіти). Obsidian зручно показує її: посилання, граф, пошук. Бекап не потрібен — усе вже на GitHub.

Налаштування на Маці (≈ 10 хв, один раз):
1. Клон окремо від усього іншого: `mkdir -p ~/Obsidian && git clone git@github.com:nick198922/MCP.git ~/Obsidian/tradedesk` (без SSH-ключа GitHub — `https://github.com/nick198922/MCP.git` і токен).
2. Obsidian (obsidian.md) → «Open folder as vault» → `~/Obsidian/tradedesk` (увесь репозиторій: так плагін Git працює без налаштувань).
3. Settings → Files and links → Excluded files: `src`, `tests`, `scripts`, `deploy`, `.venv`, `data`.
4. Settings → Community plugins → **Obsidian Git** → Install → Enable; «Auto pull interval» = 30, «Auto commit-and-sync interval» = 0. Лише читання: правки йдуть через Claude й агентів, інакше будуть конфлікти.

Корисне: Graph view (зв'язки між картками ідей і перевірками), Cmd+O — швидкий пошук файлу, Cmd+Shift+F — пошук по тексту. Почати з `docs/HANDOFF.md`, `docs/RESULTS.md`, `docs/knowledge/`.
