# 2026-11-13 · `VanishFeed` на зупиненій книзі: нога справді зникає (пункт 369)

Пункт 369 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md), зауваження «не блокує» [рев'ю пункту 360](../reviews/2026-11-13-carry-held-only-inline-feeds.md). Змінено лише `tests/test_carry_v2.py`; код продукту, правила замороженого рушія carry і `execution/` не чіпав.

- **Що було:** `test_vanish_feed_runs_on_a_stopped_book` проганяв один цикл зупиненої книги A на `VanishFeed` і перевіряв лише, що прапорець `held_only` дійшов до `FixtureFeed.load`. Чи зникла нога і що з нею зробив рушій, тест не дивився: фід, який нічого не прибирає, теж проходив.
- **Що стало:** три цикли (`GAP_CONFIRM_CYCLES`) зупиненої книги на свіжому `VanishFeed`, виплати ноги зникають з першого. На циклах 1–2 — рівно одна подія `funding_payment_missing` з `gap_cycles` 1, 2, нога відкладена (`gap_cycles` у стані, ні `mark`, ні `close`); на циклі 3 — `funding_gap_confirmed` з `cycles` 3, нога розрахована (`mark` або `close`, `gap_cycles` прибрано). Решта відкритих ніг книги (тест вимагає, щоб вони були) на кожному циклі отримують `mark`.
- **Мутаційна перевірка:** `VanishFeed` з `cut=10**15` (нога не зникає) — тест падає (`[] == [('funding_payment_missing', 'S02/USDT:USDT', 1)]`); мутацію відкочено.
- `make lint` чисто; `uv run pytest -q tests/test_carry_v2.py`: 22 passed.
- Номер 369, а не 362: 362 у картці вже зайнятий (дублікат 6.53 у BACKLOG, зроблено 13.11).
