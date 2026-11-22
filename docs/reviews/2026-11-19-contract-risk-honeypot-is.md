# Рев'ю C: `contract_risk` — друге джерело honeypot.is (пункт 400)

Дата: 2026-11-19. Об'єкт: незакомічені зміни робочого дерева (`providers/honeypot_is.py`, `analytics/contract_risk.py`, `tools/onchain.py`, `runtime.py`, тести, фікстура `honeypot_is_2026-11-19.json`, документи). `scripts/_honeypot_is_*.py` не розглядались (тимчасові).

**Вердикт: request changes** (дві блокувальні знахідки, обидві невеликі).

## Що перевірено самостійно
- `make lint` — чисто.
- `uv run pytest -q tests/test_honeypot_is.py tests/test_contract_risk.py tests/test_provider_lifecycle.py tests/test_tools_envelope_shape.py tests/test_tools_never_raise.py tests/test_server.py tests/test_onchain.py` — 153 passed.
- Інваріант 1: `token_check` іде через `b.guard(...)`; 503, непідтримувана мережа (polygon — без запиту), 404 і неочікувана форма → `missing`, `ok=true` за GoPlus. Збій GoPlus, як і раніше, `ok=false`.
- Інваріант 2/6: запит лише через `Http.get_json` з `check=`, `Fetched` і evidence від `Http`; власний ліміт 0.5/с, burst 2; 404 кешується як `data_unavailable` (поведінка `core/http.py`, рядки 178, 215). Фікстура — живі обрізані відповіді з датою й джерелом.
- Інваріант 3: `parse_honeypot_is` / `cross_check` — чисті функції, стійкі до сміття (`num`, `clean_text`, перевірки типів; рядок замість тіла → усе `None`).
- Інваріант 7: `aclose()` у `Runtime.aclose`, провайдер доданий у `test_provider_lifecycle`.
- Відсотки → дроби: `v / 100`, FLOKI 0.3 → 0.003 (тест), від'ємний податок → `None` → unknown (перевірив скриптом: `overall=unknown`, `unknown=['sell_tax']`), як і `_tax_check` для GoPlus. Податки читаються лише при `simulationSuccess is True`.
- «Збій другого джерела не дає зеленого»: при `second=None` усі чотири перевірки — `unknown` (GoPlus `fail` лишається `fail`); тіло з `simulationSuccess: true` без `honeypotResult` / `simulationResult` теж не дає pass (honeypot unknown, податки unknown). Поріг податку `> TAX_MAX` той самий, що в GoPlus.
- Злиття «гірший вердикт»: `_RANK` fail > unknown > pass; honeypot.is pass ніколи не підвищує GoPlus (тест `test_honeypot_is_pass_never_upgrades_goplus`); `overall` / `critical_failed` перераховуються через `_summary`. Тести впали б, якби злиття брало кращий вердикт або вважало відсутнє джерело за pass.

## Знахідки

### 1. (блокує) `medium_fail_rate` / `high_fail_rate` → критичний fail `cannot_sell_all` — надмірно і під чужою назвою
`_second_verdicts` ставить `cannot_sell_all = fail`, якщо в `flags` є `high_fail_rate` або `medium_fail_rate`, навіть коли симуляція honeypot.is успішна, `isHoneypot=false` і податки 0. Перевірив скриптом (WETH-відповідь + `flags=["medium_fail_rate"]`): `overall=fail`, `critical_failed=['cannot_sell_all']`, а в `warnings` — «GoPlus and honeypot.is disagree on cannot_sell_all». Проблеми:
- **Інша властивість.** GoPlus `cannot_sell_all` — функція контракту, що не дає продати весь баланс. Прапорці fail-rate honeypot.is — частка вибірки холдерів, у яких симуляція продажу не вдалась (причини: блеклист, ліміти гаманця / транзакції, кулдаун, контракти-холдери тощо). honeypot.is про `cannot_sell_all` нічого не каже, тож «розбіжність» у `warnings` — неправда, а `detail` пояснює fail словами GoPlus.
- **Ескалація понад саме джерело.** У живій фікстурі honeypot.is сам оцінює `medium_fail_rate` як `severity: "high"` (`severityIndex` 16), а `high_fail_rate` як `"critical"` (20). Ми робимо з обох критичний fail, а `pre_trade_checklist` (рядок 11) на будь-який критичний fail каже «❌ пропустити, далі чекліст не проходь».
- **Не відкалібровано й не перевірено на живих даних.** В обох живих зразках із прапорцем (EverGrow — уже honeypot, SafeMoon — симуляція не вдалась) прапорець нічого не змінює для `overall`. Той випадок, де мапінг справді діє (успішна симуляція, не honeypot, є прапорець), не має ні живого зразка, ні тесту; частка хибних спрацьовувань на нормальних токенах (вестинг-контракти, блеклист ботів) невідома.

**Виправлення:** `cannot_sell_all` від honeypot.is — лише з `isHoneypot` (продаж відкотився → fail; pass лише при успішній симуляції та `isHoneypot=false`). Прапорці fail-rate — окрема перевірка лише honeypot.is (напр. `holder_sell_fail_rate`): `high_fail_rate` → fail (`critical`, як у джерела), `medium_fail_rate` → fail `red_flag` (або unknown), нема прапорця при успішній симуляції → pass, без запису в `disagreements`. Якщо окрема перевірка зайва — мінімум: `medium_fail_rate` не дає критичного fail, fail від прапорця не рахується «розбіжністю», а `detail` називає причину. Тест на випадок «успішна симуляція + прапорець» і оновлення опису інструмента, `docs/changes/…`, `defi.md`.

### 2. (блокує) Злита перевірка суперечить сама собі у `value` і `detail`
Для `honeypot` і `cannot_sell_all` `_merge_check` лишає `value` і `detail` від GoPlus, навіть коли вердикт дав honeypot.is. Живий EverGrow: `{"verdict": "fail", "value": false, "detail": "not flagged; worse verdict of GoPlus and honeypot.is ..."}`. Модель, що пояснює вето власнику, читає «not flagged» біля «fail»; правда лише в `sources`. Для податків це вже зроблено правильно (`value` = більший податок).
**Виправлення:** коли вирішальний вердикт від honeypot.is, `value` береться з нього (або `value` = гірше значення, як у податків), а `detail` каже, яке джерело дало вердикт (напр. «honeypot.is buy/sell simulation: sell reverted»). Тест: EverGrow → `value is True`, у `detail` є «honeypot.is».

### 3. (не блокує) Застарілий опис у README і промптах
- `README.md`, рядок On-chain / DEX: «фільтр безпеки … (GoPlus + ліквідність / кап DexScreener …)» — додати honeypot.is. Запис змін каже «README без змін», але змінився не перелік інструментів, а джерела.
- `prompts/pre_trade_checklist.md:14` і `prompts/token_deep_analysis.md:24`: «евристика одного чекера» — тепер двох. Там само варто додати: `honeypot_is` у `missing` (і завжди на arbitrum / polygon / optimism / avalanche) → honeypot, cannot_sell_all, buy_tax, sell_tax «не перевірено», а не ✓.

### 4. (не блокує) Прогалини в тестах
Нема тесту на гілку `risk == "honeypot"` при `isHoneypot = null` і на від'ємний податок honeypot.is (поведінка правильна, перевірив вручну). Додати по одному рядку в `test_honeypot_is.py`.

### 5. (не блокує) `summary.risk` "high" / "very_high" ніде не сигналізується
Якщо honeypot.is каже `isHoneypot=false`, але `risk` "high" чи "very_high" (через інші прапорці), це видно лише в `second_source.risk`. Варто додати окреме попередження в `warnings` (без тексту прапорців). У описі інструмента варто згадати, що симуляція йде на парі, яку обирає honeypot.is, а не обов'язково на тій, де торгуватиме власник.

## Поза зауваженнями
Обсяг у межах пункту 400; `_summary` — мінімальний рефакторинг, потрібний для перерахунку. Коду ордерів, ключів чи повтору недовіреного тексту (`honeypotReason`, описи прапорців) нема; тест це перевіряє.

## Повторне рев'ю

Дата: 2026-11-19. Об'єкт: те саме незакомічене робоче дерево після раунду виправлень (`git diff` + неприхований `providers/honeypot_is.py`, `tests/test_honeypot_is.py`, фікстура, запис змін).

**Вердикт: request changes** (знахідки 1–5 виправлено; одна нова невелика блокувальна знахідка 6).

### Що перевірено самостійно
- `make lint` — чисто.
- `uv run pytest -q tests/test_honeypot_is.py tests/test_contract_risk.py tests/test_onchain.py tests/test_server.py tests/test_provider_lifecycle.py tests/test_tools_envelope_shape.py tests/test_tools_never_raise.py` — 157 passed.
- Скрипт-проба `cross_check` на зіпсованих тілах (рядок замість bool, сміття в `summary`, NaN / рядок у податках): жодного винятку; `isHoneypot: "false"` → honeypot і cannot_sell_all unknown; податки-сміття → unknown.

### Стан знахідок
1. **Виправлено.** `_second_verdicts` дає `cannot_sell_all` лише з `isHoneypot` / `risk == "honeypot"` (той самий кортеж, що й honeypot); pass лише при `simulation_success is True` і `is_honeypot is False`. Прапорці fail-rate — окрема перевірка `holder_sell_fail_rate` (`_fail_rate_check`): `high_fail_rate` → fail critical, `medium_fail_rate` → fail red_flag, без запису в `disagreements` (перевірка додається після злиття, повз `_merge_check`). Тести `test_fail_rate_flag_after_clean_simulation_is_a_red_flag_not_a_disagreement` (успішна симуляція + `medium_fail_rate` → `critical_failed == []`, розбіжностей нема) і `test_failed_simulation_is_unknown_and_high_fail_rate_is_its_own_critical_check` впали б при старій логіці. Опис інструмента, `docs/changes/…`, `defi.md`, `pre_trade_checklist` оновлено.
2. **Виправлено.** `_merge_check`: `value` і `detail` від джерела, що дало вердикт (`by_second`), `detail` починається з «verdict from honeypot.is: …» або «verdict from GoPlus: …»; для податків `value` — більший. Тест на живому EverGrow перевіряє `value is True` і префікс `detail` для honeypot, cannot_sell_all, sell_tax.
3. **Виправлено.** README (рядок On-chain / DEX: honeypot.is, лише ethereum / bsc / base, 25 перевірок), обидва промпти: «два чекери», `honeypot_is` у `missing` → п'ять перевірок «не перевірено», а не ✓. У `pre_trade_checklist` `holder_sell_fail_rate` при `high_fail_rate` додано до критичних.
4. **Виправлено.** `test_risk_honeypot_without_is_honeypot_fails`, `test_negative_honeypot_is_tax_is_unknown`.
5. **Виправлено.** Попередження при `risk` "high" / "very_high" без honeypot (рядок з фіксованого набору, не сирий текст; тест `test_tool_warns_on_high_risk_without_honeypot`); в описі — «simulates on a pair it picks itself».

Інваріанти після раунду: виклик honeypot.is і далі через `b.guard`; при `second=None` чотири злиті перевірки й `holder_sell_fail_rate` — unknown, `overall` не pass (тест `test_second_source_missing_means_no_green`, перевірено пробою); GoPlus fail лишається fail; pass honeypot.is не підвищує GoPlus.

### 6. (блокує, нове) `holder_sell_fail_rate` = pass, коли поля `flags` нема або воно іншої форми
`parse_honeypot_is` перетворює відсутній / `null` / не-список `flags` на `[]`, а рядки-не-рядки відкидає; `_fail_rate_check` при `simulation_success is True` і порожньому списку дає **pass**. Проба на живій WETH-відповіді: без ключа `flags` → `holder_sell_fail_rate=pass`, `overall=pass`; `flags: null` → pass; `flags: [{"flag": "high_fail_rate"}]` (саме така форма вже є в `summary.flags` тієї ж відповіді) → pass. Це порушує правило модуля й опису інструмента «missing data is always unknown, never pass»: зміна схеми honeypot.is тихо зробить нову критичну перевірку зеленою для кожного токена, у тому числі для токена з «very high amount of users cannot sell» при успішній симуляції.
**Виправлення:** у `parse_honeypot_is` `flags = None`, якщо верхній `flags` не список або в ньому є не-рядки (або брати назви з `summary.flags[].flag`, якщо верхнього нема); у `_fail_rate_check` pass лише коли `flags` — список і симуляція успішна, інакше (за відсутності прапорців high / medium) unknown. Тест: WETH-тіло без `flags` і з `flags: [{"flag": "high_fail_rate"}]` → `holder_sell_fail_rate` не pass, `overall` не pass. Оновити `test_parse_live_samples`, якщо зміниться форма `flags` у `second_source`.

### Не блокує
- При `second=None` і GoPlus pass `value` злитої перевірки стає `None` (значення GoPlus лишається лише в `sources.goplus`). Логічно за правилом «value від джерела вердикту», але для моделі зручніше лишити значення GoPlus; на розсуд автора.
