# Рев'ю: collector_health бачить типові джоби (пункт 80, 2026-11-05)

Режим C, незакомічений diff + `docs/changes/2026-11-05-collector-health-defaults.md`.

**Вердикт: approve** (блокерів немає; три зауваження нижче, жодне не блокує).

Перевірено сам: `make lint` чистий; `tests/test_collector_health.py` + `tests/test_dashboard_health.py` дають 22 passed. Таблиці, колонки, періоди й групи звірив із `collector/scheduler.py`, `store/schema.py` і `config.py`.

## Що відповідає коду
- Таблиці й колонки збігаються зі схемою: `atm_term`, `basis_hist` (група `currency`), `funding_hist` (група `venue`), `supply_snapshot` і `trader_snapshot` (колонка `day`, потім `_DAY_TS`), `trader_positions` (`ts`, `venue`).
- Періоди збігаються з `config.py`: atm_term і basis 300 с, funding 3600 с, traders і supply 86400 с, positions 3600 с. Година з ≥ 1 знімком для 300-секундних джоб дає запас.
- `funding.groups` дорівнює `PERP_EXCHANGES + hyperliquid + deribit`, і тест це звіряє. Тиха пропущена біржа в `_collect_funding` тепер видна по групі.
- Для траверсу `enabled_specs` семантика коректна: ключ відсутній означає `default_on`; значення не з «1/true/yes/on» означає вимкнено, як у `Settings`. Явний «0» вимикає. Не-default специфікації поводяться як раніше.
- Панель і ранковий бриф ходять через `collect()` і `enabled_specs`, тож нові серії з'являються без змін коду; тести панелі явно вимикають типові джоби, щоб перевіряти лише pm_gap. Це чесно і коментовано.
- Інваріанти CLAUDE.md не порушено: `analytics/` лишається чистим, I/O немає, БД `mode=ro`, `execution/` не чіпається.

## Зауваження

1. **Низька.** `traders`, `trader_positions` і `supply` у `build_default_scheduler` створюються лише всередині `if s.collector_enabled` (рядок 696). Specs мають `flag` на власний перемикач, тому при `TRADEDESK_COLLECTOR_ENABLED=0` у features.env вони все одно вважатимуться ввімкненими й дадуть хибні `never`/`low`. Тест `test_traders_gap_in_one_venue_is_visible_and_empty_db` навіть вимикає `COLLECTOR_ENABLED` і очікує, що traders рахуються, що не відповідає реальному планувальнику. Виправлення: в `enabled_specs` для `default_on` специфікацій додатково вимагати, щоб `TRADEDESK_COLLECTOR_ENABLED` не був вимкнений, і поправити тест.
2. **Низька.** `Settings.collector_enabled` за замовчуванням False, а `default_on` для atm_term/basis/funding вважає її ввімкненою за відсутності ключа. Це правда лише для сервера (05-services.sh пише `=1`; у `deploy/features.env` рядка немає). Це варто назвати в докстрингу `enabled_specs` або в change-файлі як припущення, а не лише «`/etc/tradedesk-mcp.env` не читається».
3. **Інформаційна.** `trader_positions` ловить дірки лише при годині без жодної зміни, як і зазначено в докстрингу модуля. Якщо Polymarket-акаунти стануть спокійними, з'являться хибні «low». Це зафіксовано в докстрингу, прийнятно, але варто стежити в перший тиждень.
4. **Нітик.** У `scripts/collector_health.py` рядок докстрингу довший за 120 символів, хоча ruff його пропускає. Виправляти не обов'язково.

Тести: нові перевіряють відсутність перемикача, явне «0», дірку в одному `venue` (traders і positions), порожню БД і текст рядка брифу. Вони впали б при поломці логіки `default_on`.
