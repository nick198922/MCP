# Obsidian для бази знань

Документація проєкту — Markdown у git (`docs/knowledge/`, `docs/library/`, `docs/ideas/`, звіти). Obsidian зручно показує її: посилання, граф, пошук. Бекап не потрібен — усе вже на GitHub.

Налаштування на Маці (≈ 10 хв, один раз):
1. Встановити Obsidian (obsidian.md) і git (`xcode-select --install`, якщо ще нема).
2. Клонувати репозиторій окремо від усього іншого: `git clone git@github.com:nick198922/MCP.git ~/Obsidian/tradedesk`.
3. Obsidian → «Open folder as vault» → `~/Obsidian/tradedesk/docs`.
4. Settings → Community plugins → увімкнути, знайти **Obsidian Git** → Install → Enable. У його налаштуваннях: «Auto pull interval» = 30 хв, «Auto commit-and-sync» = вимкнено (лише читання: правки йдуть через Claude й агентів, інакше будуть конфлікти).
5. Settings → Files and links → «Use [[Wikilinks]]» вимкнути (у нас звичайні Markdown-посилання).

Корисне: Graph view (зв'язки між картками ідей і перевірками), Ctrl/Cmd+O — швидкий пошук файлу, Ctrl/Cmd+Shift+F — пошук по тексту.
