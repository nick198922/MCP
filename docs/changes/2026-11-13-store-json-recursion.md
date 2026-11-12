# 2026-11-13 — глибока вкладеність у JSON-колонках сховища (пункт 355)

Інваріант 1 `CLAUDE.md`; той самий клас, що пункт 328 (`journal` / `positions`) і пункти 352–354.

- **Помилка:** масив `[[[...]]]` на 100 000 рівнів (від іншого записувача) у `carry_paper.tags` / `entry_flags`, `trade_plan.rules` / `tags`, `rec_paper.legs` чи колонках `payload` кидав `RecursionError` із `json.loads` у сховищі. Падали цілком `carry_paper_status`, `carry_paper_close`, `carry_paper_open` (перевірка дублікатів), `plan_list`, `plan_review` і щогодинна задача `plan_resolve`.
- **Виправлено:** `_parse_carry_paper_row` і `_parse_json_cols` у `store/db.py` ловлять `RecursionError`, як уже робить розбір `journal`: така клітинка читається, як нерозбірний текст (`[]` для `carry_paper`, `None` для решти). Правильні значення й нерозбірний текст поводяться як раніше.
- Новий `tests/test_tool_failure_extra_22.py` (10 тестів: обидва розбирачі, `plan_list` / `plan_review` з глибокими `tags` і `rules`, `carry_paper_status` з глибокими `tags` і `entry_flags`, скасування через `carry_paper_close`, незмінність правильних значень). На старому коді падають 9 з 10.
- Відкрите (пункт 356): `tags` на 300 рівнів розбираються, але валять серіалізатор MCP у `plan_list` і `carry_paper_status`; `legs`, що не список, у `assistant_paper_*`.
