# 2026-11-26: темп Deribit — перевірка колектора після злиття

Перевірка з BACKLOG («Колектор Deribit ↔ Bybit», пункт координатора). Лише читання: `system_health`, `scripts/diag_deribit_gaps.py --since 2026-11-24` і запити нижче до `/var/lib/tradedesk-mcp/tradedesk.sqlite` (`mode=ro`), 26.11 ≈ 14:40 UTC. Коду не змінено. Зміна темпу потрапила в `main` 26.11 о 07:31 UTC (`1dbee97e`); перший перезапуск служби після цього за рядом `atm_term` — 08:20 UTC (висновок за фазою, журналу служби в клоні нема).

| Умова | Результат |
|---|---|
| Схема 43 | ✓ `system_health`: `store.version` 43 |
| `option_legs` 26.11 без ніг `src = 'summary'` | ✓ 0 з 40 (21–25.11: 23, 21, 22, 22, 23 з 40) |
| `option_xvenue` без `d_miss = 'rate_limited'` | ✓ знімки 12:22, 13:22, 14:22: 84 рядки, `d_miss` порожній у всіх (00:22–05:22 того ж дня без ноги Deribit 92 зі 164) |
| `pm_gap`: `abs(surface_ts - book_ts)` ≤ 30 с після перезапуску | ✓ з 08:20 UTC 16 пар «запуск × актив», найбільше 10 с (09:20 BTC); 19–25.11 до зміни: найбільше 32 с, понад 30 с одна пара з 392 |

- Знак, що знімок 09:00 уже на новому темпі: `quote_ts_ms` ніг `option_legs` розтягнуті на 36 с (21–25.11 — 6–10 с).
- Тиждень кондора 20–26.11 (лише лічба ніг за `src`, цін і результату не читав): вхід 20.11 — 20 ніг, вихід 26.11 — 40 рядків о 09:00:25, усі `ticker`.
- `b_miss = 'not_listed'`: 12 рядків за три знімки — ETH 1670 і 1720 (експірація 27.11) немає у відповіді Bybit, решта 5 страйків того ж запиту є; тобто це відсутні страйки, а не збій.
- Обмеження: після злиття лише три годинні знімки `option_xvenue` і один `option_legs`; п'ятничний вхід 27.11 ще попереду (рядок у BACKLOG). `system_health` показує Deribit `ok` без збоїв, але лише від перезапуску 14:26 UTC.
- Дрібне: текст «normal between 06:00 and 10:00 UTC» в `option_xvenue` хибний (має бути до 12:00) — пункт (4) розробника в BACKLOG.

Запити (поза `diag_deribit_gaps.py`):

```sql
SELECT ts, COUNT(*), SUM(d_instrument IS NULL) FROM option_xvenue_snapshot
 WHERE ts >= strftime('%s','2026-11-26') AND mode = 'hourly' GROUP BY ts;
SELECT d_miss, b_miss, COUNT(*) FROM option_xvenue_snapshot WHERE ts >= strftime('%s','2026-11-26 07:31') GROUP BY 1, 2;
SELECT ts, asset, MAX(ABS(surface_ts - book_ts)) FROM pm_gap_snapshot WHERE ts >= strftime('%s','2026-11-26') GROUP BY ts, asset;
SELECT ts, MAX(quote_ts_ms) - MIN(quote_ts_ms) FROM option_leg_snapshot WHERE day >= '2026-11-21' GROUP BY ts;
SELECT day, src, COUNT(*) FROM option_leg_snapshot WHERE day IN ('2026-11-20', '2026-11-26') GROUP BY 1, 2;
```
