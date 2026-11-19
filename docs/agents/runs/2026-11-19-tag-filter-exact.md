**Крок:** п (Порядок 1, `standing-improvements`, пункт 396a). Картки «Порядок 0» заблоковані: x-traders чекає гілки btc_charlie, agents-mcp-access — Q-20261114-2 (правка `.claude/agents/*` заборонена), options-playbook п. 4 без правила в черзі (4.10–4.11), profit-search п. 2 вичерпано, structural-edges 8f чекає вікна до 2027-02-24.

**Результат:**
• Фільтр `tag` у `journal_list`, `plan_list`, `positions_list` тепер через `json_each`: кириличні теги знаходяться, `%` і `_` — звичайні символи, старі рядки збігаються.
• Нові тести (кирилиця, маски, невалідний JSON, `exclude_tag`); `make lint` і `make test` зелені: 10610 passed.

**Критик:** C (sonnet) — approve, блокерів нема; зауваження про `exclude_tag` з NULL додано в changes. [Рев'ю](../../reviews/2026-11-19-tag-filter-exact.md).

**Далі:** `owner`. Нових питань нема. Змін поведінки на неякісних даних: невалідний JSON у `tags` = «без тегів».
