# HANDOFF — стан проєкту

Оновлено: **2026-10-17**. Одна сторінка: поточний стан і рішення.
- Історія сесій до 17.10, подробиці перевірок і відомі обмеження коду — [`history/HANDOFF-2026-10-17.md`](history/HANDOFF-2026-10-17.md).
- Повний огляд — звіт [`reports/2026-10-17-project-report.pdf`](reports/2026-10-17-project-report.pdf).
- Оновлюйте цей файл наприкінці сесії: коротко, старе переносьте в `history/`.

## Що це
- Особистий MCP «трейдинг-деск» власника (Микола, Київ): перпи, опціони Deribit, Polymarket, квант-аналітика, пам'ять, правила.
- Мета — знайти стратегію з доведеною перевагою, пройти форвард на папері, потім Фаза 4 (ордери) за ADR 0005.
- Ордерного коду немає. Режим лише читання.

## Де що
- **Сервер:** Contabo `169.58.169.184`, користувач `tradedesk`.
  - Служби: `tradedesk-mcp` (MCP і колектор 24/7), `tradedesk-agents-loop`, `tradedesk-tgbridge`.
  - Оновлення з `main` щоночі о 02:15. Шляхи — `docs/AGENTS.md` → «Запуск».
- **Сховище:** SQLite, схема 12. Колектор пише:
  - ATM-строки, funding, basis, ліквідації (WebSocket);
  - історію OHLCV, пропозицію токенів;
  - лідерборди й позиції трейдерів HL і Polymarket.
- **Перевірка стану з Мака** (лише читання): `scripts/server/` — `health-check.sh`, `disk-check.sh`, `usage-check.sh`, `agents-status.sh`, `agents-progress.sh`, `coinalyze-check.sh`, `doubletop-check.sh`. Адмінське — лише нумеровані скрипти, їх запускає власник.
- **Ключі:**
  - Coinalyze — у `/etc/tradedesk-mcp.env`, лише MCP-сервіс;
  - Telegram — там само й у `~/.config/tradedesk/agents.env`;
  - GitHub-токен агентів.
  Нічого з цього не в git.

## Стан (17.10)
- **85 MCP-інструментів, 882+ тести.** Таблиця — `README.md`; відповідність DOUBLETOP — `docs/PARITY.md`.
- **Агенти** (`docs/AGENTS.md`) працюють безперервно з автозливом: 46 кроків здано за 14–17.10.
  - Лаунчер уміє паркувати кілька гілок.
  - Від 80 % тижневого ліміту — лише картки власника.
  - Пауза — при 95 %.
- **Дослідження:** 33 картки, 0 підтверджених. Дошка — [`RESULTS.md`](RESULTS.md) (`scripts/results_board.py`).
  - Кандидати з позитивним бектестом: carry альтів, carry перп-перп, VRP BTC (на реальних марках не значущий).
  - Технічний аналіз (курс DoubleTop, SMC, заклики трейдерів) — ✗ або ◯ скрізь.

## Рішення власника 2026-10-17 (усі 9 пропозицій звіту)
- **Пріоритет до чекпоінту 30.11 — форвард-папір і Polymarket.**
- **Технічний аналіз без механізму більше не тестуємо.**
- **Пріоритет ідей — за цінністю** (ефект × імовірність / вартість).
- **Фаза 4** — не раніше 4+ тижнів паперу, що узгоджується з бектестом.

Деталі — `docs/AGENTS.md` → «Візія».

## Черга агентів (картки власника, `Порядок: N`)
1. [deviation-weekly-trend](ideas/IDEA-20261017-deviation-weekly-trend.md) — H1, улюблений сетап Іллі.
2. [forward-paper-carry](ideas/IDEA-20261017-forward-paper-carry.md) — паперовий рушій у колекторі, тижневий звіт.
3. [doubletop-to-ours](ideas/IDEA-20261017-doubletop-to-ours.md):
   - звірка з DOUBLETOP;
   - `dex_pairs`, `chain_metrics`;
   - історія Coinalyze і повтор перевірок ліквідацій і натовпу у funding.
4. [vrp-real-marks](ideas/IDEA-20261017-vrp-real-marks.md).
5. [polymarket-vs-options-forward](ideas/IDEA-20261017-polymarket-vs-options-forward.md) — CLV.
6. [course-setups-crypto](ideas/IDEA-20261016-course-setups-crypto.md), кроки 8–11 — потім серію закрити.

Поза чергою: «розумні гроші» (квант зараз), калібрування Polymarket (правки після рев'ю).

## MCP DOUBLETOP (тимчасово)
- Конектор claude.ai на сервері через акаунт власника. Агентам — лише для звірки й зразків (`tests/fixtures/doubletop/`), не з коду. Правила — `docs/AGENTS.md`.
- Їхній `dt_edu` слабкий; цінні Coinalyze і публічні API.

## Матеріали DoubleTop (платні) — лише локально
- `docs/library/courses/` (gitignored): курс, Discord-канали трейдерів, звіти по закликах. У git — лише наші нотатки (`docs/library/LIB-*`).
- **Розібрано:**
  - Good_Mood — 14 міс., −0.20R/угоду механічно;
  - gmilnikov — 20 угод, канал з серпня;
  - f1ntech — перший шматок; власник докидає далі через `scripts/clip.sh f1ntech`.

## Відкрите / чекає власника
- Один раз запустити паперовий carry вживу, щоб перевірити на біржах (з картки 2 буде команда).
- Живий заклик Good_Mood на ETH (16.10, 2390–2425, стоп 2318, ціль 2650) — відкритий. Дорахунок: `scripts/calls_hourly.py`.
- `docs/QUESTIONS.md` — питання агентів.
