# 2026-11-12 · Polymarket «кількість пасажирів TSA»: крок 0, жорстка брама — не пройдено

Картка: [`IDEA-20261112-pm-tsa-daily-seasonal-nowcast`](../ideas/IDEA-20261112-pm-tsa-daily-seasonal-nowcast.md). Скрипт: `scripts/verify_pm_tsa_seasonal.py gate` (мережа лише через `rt.polymarket`, кеш `~/.cache/tradedesk-agents/pm-tsa-daily-seasonal-nowcast/`). Ціни ринку й результати не відкривались; H1 не проганялась (умова 1 критика: прогін «для знань» без форварду не робити).

## Метод

Перелік усіх подій про TSA у Gamma на 12.11.2026, трьома шляхами:
1. `/events/keyset` з `tag_slug=tsa`, `closed=false` і `closed=true` (курсорна пагінація, `complete=True`);
2. `/public-search` за фразами «Number of TSA passengers», «TSA passengers», «TSA checkpoint», «TSA», «Thanksgiving TSA», «Christmas TSA», «air travel passengers», статуси `active` і `closed` (фільтр: слово TSA у назві чи slug);
3. усі серії, до яких належать знайдені події (10716 `tsa-passengers` тижнева, 10779 денна, 11406, 11415), `closed=false/true`.

Брама (картка, «Критерій вбити»; умова 1 критика): є хоч одна подія, що закінчується, починається чи створена після 2026-07-05, або відкрита подія, або анонс на свята (День подяки 26.11, Різдво) → крок 0 (б)–(в); інакше `archived`.

## Результат

| Перевірка | Число | Висновок |
|---|---|---|
| Подій TSA усього (унікальні) | 166 (endDate 2021-04-10 … 2026-07-05) | історія є |
| Відкритих (`tag_slug=tsa`, `closed=false`) | 0 | ✗ |
| Відкритих у 4 серіях | 0 / 0 / 0 / 0 | ✗ |
| `public-search` active: TSA-подій | 1 — `number-of-tsa-passengers-january-2` (closed=True, застарілий індекс пошуку) | не нова |
| Пошук «Thanksgiving TSA» / «Christmas TSA», active | 0 / 0 TSA-подій | анонсу нема |
| Подій з end/start/creation після 2026-07-05 | 0 | ✗ |
| **Брама** | | **не пройдено → `archived`** |

Згасання серії (кількість подій і медіанний обсяг за місяцем закінчення):

| Місяць | Подій | Медіана обсягу, $ |
|---|---|---|
| 2025-11 | 3 | 98 583 |
| 2025-12 | 18 | 33 061 |
| 2026-01 | 30 | 23 184 |
| 2026-02 | 34 | 34 528 |
| 2026-03 | 36 | 883 |
| 2026-04 | 14 | 8 320 |
| 2026-05 | 4 | 2 726 |
| 2026-07 | 1 | 916 |

Денні події закінчились у квітні 2026; з кінця квітня лишились лише тижневі (обсяг $0.9–6 тис.), остання — тиждень 29.06–05.07 ($916). Навіть якщо серія повернеться, книга такого розміру не тримає угоди $20–40 по кількох кошиках без суттєвого впливу.

## Застереження

- **Час лістингу.** Між створенням події й її кінцем медіана 6 днів, p90 16 днів, максимум 141. Торішня подія на тиждень Дня подяки (`total-number-of-tsa-passengers-november-23-30`, обсяг $464 тис.) з'явилась за 17 днів, тобто ≈ 13.11.2025. Отже відсутність подій 12.11.2026 ще не виключає святкової події на 26.11.2026: брама відсічена за правилом картки, але тригер повернення залишено (див. картку).
- Кроки 0 (б) (покриття `prices-history` на t0, глибина, комісія) і 0 (в) (таблиці TSA 2019–2026, SD моделі, наявність D−2 на t0) не виконувались: за брамою вони не потрібні. Попередньої реєстрації нема; жодних цін ринку проти результатів не переглядалось, тож майбутній тест (якщо серія відновиться) лишається чистим.
- Пошук Gamma повертає не більше 50 подій на фразу; повноту дає `tag_slug=tsa` і перелік серій (обидва `complete=True`).

## Вердикт

| Твердження | Вердикт |
|---|---|
| Ринки TSA ще виставляються (брама кроку 0 (а)) | ✗ — 0 відкритих, 0 нових після 05.07.2026, анонсів на свята нема |
| H1 (Brier ринку − моделі ≥ 0.10) | ◯ — не перевірялась (брама) |
| H2 (угода) | ◯ — не перевірялась |

Картка → `archived`. Повернути до `accepted`, якщо в Gamma з'явиться відкрита подія з тегом `tsa` (найімовірніше — на тиждень Дня подяки, 23–30.11.2026, або різдвяні дати); тоді почати з кроку 0 (б).

## Сирий вивід (скорочено)

```
  tag_slug=tsa closed=false: 0 events, complete=True, rss 156 MB
  tag_slug=tsa closed=true: 168 events, complete=True, rss 171 MB
  search q='Number of TSA passengers' active: 1 events, tsa-matching 1   # january-2, closed=True
  search q='Thanksgiving TSA' active: 1 events, tsa-matching 0
  search q='Christmas TSA' active: 11 events, tsa-matching 0
  series 10716 closed=false: 0 events / closed=true: 26
  series 10779 closed=false: 0 events / closed=true: 111
  series 11406 closed=false: 0 events / closed=true: 17
  series 11415 closed=false: 0 events / closed=true: 12
TSA events total: 166 (non-TSA dropped: 30)
endDate range: 2021-04-10 .. 2026-07-05
active & not closed: 0
events with end/start/creation after 2026-07-05: 0
  last: 2026-07-05T00:00:00Z closed=True vol=916.33 number-of-tsa-passengers-june-29-july-5-20260626182033669 series=[10716 tsa-passengers]
lead days (creation -> end): median 6, p90 16, max 141
```
