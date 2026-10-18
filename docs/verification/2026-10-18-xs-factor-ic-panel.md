# Крос-секційні цінові фактори перпів (R7, M28, MAX30, IVOL30): рангова IC на point-in-time всесвіті — 2026-10-18

Картка: `docs/ideas/IDEA-20261018-xs-factor-ic-panel.md`. Умови критика 1–6: `docs/reviews/2026-10-18-setup-hunt-triage.md`.

Порядок дій:
1. Реєстрацію записано в картці до будь-якого розрахунку сигналів.
2. Числа стадії 0 (без середніх) дописано в картку до запуску `full`.
3. Розвідувальну стадію `extra` додано **після** перегляду результатів `full`, і вона так і позначена.

Коміту картки перед прогоном не було: координатор цього запуску заборонив індексувати файли. Тому порядок підтверджують лише ці записи.

- Скрипт: `scripts/verify_xs_factor_ic_panel.py` (стадії `panel`, `stage0`, `full`, `extra`).
- Аналітика: `src/tradedesk/analytics/xs_factors.py`. Тести: `tests/test_xs_factors.py`.
- Дані: лише наявний кеш `~/.cache/tradedesk-agents/funding_crowding/` (без мережі). Вихідні файли — у `~/.cache/tradedesk-agents/xs_factor_ic_panel/`.

## Підсумок

**За зареєстрованим правилом: MAX30 ✓, M28 ✗, R7 і IVOL30 ◯ (ворота потужності) → за буквою реєстрації `confirmed`; **рішенням критика статус картки — `rejected`** (MAX30 дублює IVOL30, після контролю IC +0.011; див. docs/reviews/2026-10-18-xs-factor-ic-panel.md). Це підтвердження рангової IC, а не торгової переваги. Розвідувальна перевірка показує, що рангова IC MAX30 майже повністю пояснюється волатильністю: волатильні монети мають нижчу медіану тижневої дохідності, а не нижче середнє. Різниці в середній дохідності, яку можна заробити, не видно. Описовий кошик після funding і витрат дає −6 % річних.**

- **MAX30.** IC −0.091, CI [−0.108; −0.074], t −10.6, Holm-p < 0.0001. Ефект стабільний:
  - IS −0.084, OOS −0.112;
  - кожен рік від −0.069 до −0.122;
  - кожне ковзне 52-тижневе вікно від'ємне;
  - без топ-10 тижнів −0.078;
  - без MYX-подібних символів −0.094;
  - з лагом входу 1 доба −0.083;
  - нейтралізована за funding і обігом −0.086;
  - плацебо p 0.001.

  Формально все пройдено.
- **Чому це не гроші (розвідувально, після `full`):**
  - нахил Fama–MacBeth (середня дохідність на 1 SD рангу) −0.05 п. п./тиждень, t −0.47;
  - середня дохідність квінтилів пласка: Q1 −0.15 %, Q5 −0.46 %/тиждень; різниця Q1 − Q5 +0.31 %, CI [−0.32; +0.91];
  - медіани квінтилів падають монотонно: від −1.15 % до −3.12 %;
  - нуль «однакові середні, логнормальна дохідність із власною trailing-волою монети» сам дає IC −0.049, тобто більше половини спостереженої;
  - IC трейлінгової 30-денної воли −0.105, а MAX30 після нейтралізації за цією волою дає +0.002 (t +0.44).

  MAX30 — це вола, а рангова IC вимірює медіану, а не середнє.
- **Кошик 20/20 (описово).**
  - Ціна +0.37 %/тиждень. Funding −0.38 %/тиждень: шорт «лотерейних» монет платить від'ємний funding.
  - Витрати 0.11 %. Нетто −0.12 %/тиждень (−6.3 % річних на валовий номінал), CI [−0.71; +0.42] %/тиждень.
  - Найгірший тиждень −40 %.
- **M28 (моментум).** IC −0.010, CI [−0.027; +0.007], Holm-p 0.69. В IS −0.016, в OOS +0.006 (знак змінився). Без топ-10 тижнів +0.004 ✗. Моментуму на тижневому горизонті на цьому всесвіті немає, ні в бік моментуму, ні в бік реверсу. Ефект ≥ 0.029 виключено з потужністю 80 %.
- **R7, IVOL30.** SD IC 0.149 / 0.173 — більша, ніж 0.117 у funding, як і передбачав критик (умова 1). MDE після Holm 0.033 / 0.033 > 0.03 → ◯, середні не рахувались.

Практично для власника: **торгувати нічого з цього не варто.** «Лотерейні» монети (з великим денним стрибком за місяць) у типовий тиждень справді падають більше. Але середнє тягнуть вгору рідкі великі злети. Шорт таких монет платить funding, тож переваги в середньому немає. Як фільтр ризику (не тримати лонгом «лотерейні» альти, якщо важлива медіана, а не середнє) — так, як джерело прибутку — ні.

| Пункт | Поріг | Результат | Вердикт |
|---|---|---|---|
| Survivorship: делістнуті у всесвіті | закрити (умова 3) | 27.8 / 26.4 / 22.4 / 11.1 / 1.8 % символо-тижнів 2022…2026; 121 нога обрізана делістингом і **збережена** | ✓ |
| Point-in-time сигналу | unit-тест (умова 3) | `test_signals_point_in_time`: зміна чи видалення барів ≥ t не змінює сигналів; зміна бару t−1 змінює | ✓ |
| Контрольне число | close BTCUSDT 2024-12-31 = 93 548.9 | 93 548.9 | ✓ |
| Узгодженість з `funding-crowding` | IC funding −0.004 (неділя) | −0.001 (понеділок), CI [−0.017; +0.016] | ✓ |
| Ворота R7: MDE_Holm | ≤ 0.03 | **0.0328** (SD 0.149) | ◯ |
| Ворота M28 | ≤ 0.03 | 0.0288 (SD 0.152) | ✓ пройдено |
| Ворота MAX30 | ≤ 0.03 | 0.0286 (SD 0.147) | ✓ пройдено |
| Ворота IVOL30 | ≤ 0.03 | **0.0334** (SD 0.173) | ◯ |
| **M28: рангова IC** (n 247, медіана 198 симв.) | Holm-p < 0.05, \|IC\| ≥ 0.03, знак IS = OOS | −0.0104 [−0.027; +0.007], Holm-p 0.69; IS −0.016 / OOS +0.006 | **✗ NOT CONFIRMED** |
| M28 нейтралізована (funding, обіг) | CI без 0 | −0.0074 [−0.024; +0.009] | ✗ |
| **MAX30: рангова IC** | ті самі | **−0.0911** [−0.108; −0.074], Holm-p < 1e-4; IS −0.084 / OOS −0.112; без топ-10 −0.078 | **✓ CONFIRMED (як рангова IC)** |
| MAX30 нейтралізована (funding, обіг) | CI без 0, той самий знак | −0.0858 [−0.103; −0.069] | ✓ |
| MAX30: плацебо 1000 | — | p 0.001 (SD плацебо 0.0048) | ✓ |
| MAX30 як середня дохідність (FM, розвідувально) | — | −0.05 п. п./тиждень на 1 SD, t −0.47 | ✗ ефекту в середньому не видно |
| MAX30 після нейтралізації за 30-денною волою (розвідувально) | — | +0.0024 [−0.009; +0.013] | ✗ окремого ефекту понад волу нема |
| Кошик MAX30 20/20, нетто (описово) | не у вердикті | −6.3 % річних, CI [−37 %; +22 %] річних; ціна +19 %, funding −20 %, витрати −5.6 % | — (MDE 0.80 п. п./тиждень) |
| Кошик M28 20/20, нетто (описово) | не у вердикті | −9.8 % річних, CI [−36 %; +21 %] | — |

Дохід кошиків подано **на валовий номінал** (половина лонг, половина шорт; річні = тижневі × 52). На капітал при плечі k на ногу він у k разів більший, тобто так само від'ємний.

## Метод

**Всесвіт** (без змін від `funding-crowding`, функція `week_candidate`):
- 678 придатних `…USDT` перпів Binance з архіву `data.binance.vision`, з SETTLING і прибраними символами;
- вік безперервного ряду ≥ 30 діб;
- медіанний обіг за 30 діб ≥ 5 млн USD;
- бар на добу t;
- без дірки funding у вікні [t − 7 д; t);
- один символ на актив.

Відрізняється лише день: понеділок 00:00 UTC, з 2022-01-03 по 2026-09-21 (247 тижнів). У кожного члена всесвіту були визначені всі чотири сигнали, тому всесвіт спільний.

**Сигнали** (лише бари діб < t; t−1 — неділя, її close — ціна о пн 00:00):
- R7 = C(t−1)/C(t−8) − 1;
- M28 = C(t−8)/C(t−29) − 1 (28 діб без останніх 7, тобто 21 доба дохідності);
- MAX30 — максимальна денна дохідність за [t−30; t−1];
- IVOL30 — SD залишку OLS на BTC за ті самі 30 діб.

**Ціль:** open t → close t+6. Делістнутий символ закривається за останнім close.

**IC:** Спірмен по всесвіту тижня. Середнє за тижнями, SE Newey–West (лаг 4), Holm по 4 сигналах. **Нейтралізована IC** — часткова рангова кореляція: z-ранги сигналу й дохідності очищено від z-рангів trailing funding (7 д) і log 30-денного обігу.

**MDE:** MDE = 2.8 × SE_NW; MDE_Holm = 3.34 × SE_NW (найсуворіший крок Holm з 4, двобічно, потужність 80 %). Ворота — окремо для кожного сигналу.

**По активах:** внесок символо-тижня z(сигнал) × z(дохідність), Cochran Q за символами з ≥ 20 тижнями і перестановка міток всередині тижня (500). Стійкість: топ-10 і топ-чверть символів за IS проти решти в OOS (кластерний бутстреп за тижнями).

**Кошик:** n = min(20, ⌊N/10⌋) на сторону. Витрати — модель `funding-crowding`: 0.05 % комісії + 0.05/0.10 % проковзування за обігом, на фактичному обороті. Funding фактичний. Напрям — за знаком IC на всій вибірці (in-sample, тому лише описово).

## Результати по активах (5a)

| Сигнал | k активів | Q / df | p | I² | Перестановка міток p | Топ-10 IS → OOS (різниця з рештою) | Топ-чверть IS → OOS | ρ IS–OOS |
|---|---|---|---|---|---|---|---|---|
| M28 | 487 | 680 / 486 | < 0.001 | 0.29 | 0.002 | −0.005 [−0.127; +0.124] | +0.041 [−0.012; +0.092], p 0.06 | +0.19 |
| MAX30 | 487 | 669 / 486 | < 0.001 | 0.27 | 0.002 | −0.028 [−0.173; +0.115] | **+0.062 [+0.021; +0.103]**, p 0.002 | +0.13 |

- **Розкид за активами більший за шум** (I² ≈ 0.3). Але внески символо-тижнів мають важкі хвости, і в M28 вони автокорельовані: сусідні тижні мають 2 з 3 спільних тижнів у вікні сигналу. Тому Q, ймовірно, завищено.
- **Топ-10 за IS** в OOS нічим не кращі за решту. Список «найкращих монет» — це везіння.
- **MAX30, топ-чверть за IS.** Це монети, де ефект був найслабшим або протилежним. В OOS вони теж мали слабший ефект (−0.072 проти −0.134). Отже, частина неоднорідності прив'язана до властивості монети. Найімовірніша властивість — рівень і стабільність воли. Це узгоджується з розвідувальним висновком «MAX30 = вола». Новою гіпотезою це не є: підгрупа, де ефект слабший, грошей не дає.

## Розвідувальне (додано після `full`, вердикту не змінює)

Додано, бо IC −0.09 у 3 рази більша за очікувану з літератури (0.02–0.05), а кошик за знаком IC збитковий. Перевірено, що саме міряє IC.

| Перевірка | MAX30 | M28 |
|---|---|---|
| Fama–MacBeth (середнє, п. п./тиждень на 1 SD рангу) | −0.050, t −0.47 | −0.019, t −0.19 |
| Середня дохідність Q1 … Q5, %/тиждень | −0.15 / −0.17 / −0.22 / −0.00 / −0.46 | −0.29 / −0.20 / +0.01 / −0.08 / −0.45 |
| Медіана квінтиля Q1 … Q5, %/тиждень | −1.15 / −1.48 / −1.70 / −2.17 / −3.12 | −2.24 / −1.69 / −1.43 / −1.50 / −2.57 |
| Q1 − Q5, ціна до витрат | +0.31 % [−0.32; +0.91] | +0.15 % [−0.45; +0.85] |
| Q1 − Q5, funding (лонг Q1, шорт Q5) | −0.39 %/тиждень | −0.08 %/тиждень |
| Нуль «рівні середні, логнормальна з власною волою» (20 прогонів) | IC −0.049 (−0.062…−0.040) | −0.015 |
| IC після нейтралізації за 30-денною волою | +0.002 [−0.009; +0.013] | +0.007 [−0.009; +0.024] |

Окремо: IC трейлінгової 30-денної воли (SD денних дохідностей) −0.105, t −8.9. Це близько до IVOL30, для якого ворота потужності не пройдено. Нуль із логнормальною дохідністю пояснює ≈ 0.05 з 0.09. Решту, найімовірніше, дають хвости, важчі за логнормальні: у волатильних монет рідкі великі злети, тож медіана низька, а середнє — ні. Нуль цього не моделює.

## Застереження

1. **`confirmed` тут — рангова IC, а не стратегія** (умова 5). Ефект у середньому (FM, квінтилі) не значущий. Описовий кошик за знаком IC збитковий після funding. За умовою 5 наступним кроком мала б бути окрема картка з форвард-папером. Розвідувальні результати кажуть, що її відкривати не варто: заробляти нічого. Рішення — за критиком і власником.
2. **Рангова IC чутлива до асиметрії розподілу.** Для сигналів, пов'язаних із волою (MAX, IVOL, бета), Спірмен міряє зсув медіани, який за правої асиметрії не дорівнює зсуву середнього. Це вада зареєстрованої метрики для таких сигналів. Для майбутніх карток із вола-подібними сигналами варто реєструвати і FM-нахил (середнє), і нейтралізацію за волою.
3. **Холм і MDE.** Ворота R7 та IVOL30 не пройдено з запасом 0.003. Середніх цих сигналів не рахували, як зареєстровано. IVOL30 майже напевно повторює MAX30, бо обидва — вола (див. розвідувальне), але це не перевірено.
4. **Одна крипто-хвиля** (2022–2026), один майданчик (Binance), всесвіт за обігом, а не за капіталізацією.
5. **Перевірки даних** ті самі, що в `funding-crowding`:
   - 66 денних рухів ×3 / ×1/3 — реальні сквізи й обвали (LUNA, FTT, MYX тощо);
   - інтервали funding 1/2/4/8 год;
   - TradFi відфільтровано за сьогоднішнім `exchangeInfo`.

## Сирий вивід

### `panel`

```
loaded 678 symbols in 45s; peak RSS 941 MB
  week 50 2022-12-12 N=138 (49s; peak RSS 944 MB)
  week 100 2023-11-27 N=207 (52s; peak RSS 948 MB)
  week 150 2024-11-11 N=235 (58s; peak RSS 953 MB)
  week 200 2025-10-27 N=314 (66s; peak RSS 960 MB)
panel: 247 weeks in 72s; reasons {'ok': 50134, 'young': 2304, 'no_entry_bar': 1108, 'funding_gap': 277, 'dup_asset': 0, 'illiquid': 21595}; peak RSS 986 MB
check BTCUSDT perp close 2024-12-31 = 93548.9 (listing-drift / token-unlock reports: 93,548.9)
check FTTUSDT funding 2022-11: 126 settlements, min rate -0.0250 (fixture: 126, -0.0250)
check day-over-day close moves >= x3 or <= x1/3 (squeezes, collapses or bad prints): 66: ['AIAUSDT 2025-10-02 x3.15', ... (той самий список, що в звіті funding-crowding)]
funding interval hours in the archive (settlement counts): {1.0: 80526, 2.0: 3492, 4.0: 1557941, 8.0: 865622}
```

### `stage0`

```
rank Mondays 247 (2022-01-03..2026-09-21); weeks with N >= 50: 247
universe N per week: min 57, median 198, max 327; exclusion reasons {'ok': 50134, 'young': 2304, 'no_entry_bar': 1108, 'funding_gap': 277, 'dup_asset': 0, 'illiquid': 21595}
  2022: {'TRADING': 5052, 'REMOVED': 570, 'SETTLING': 1372} delisted share 27.8%
  2023: {'TRADING': 6495, 'SETTLING': 1887, 'REMOVED': 447} delisted share 26.4%
  2024: {'TRADING': 9919, 'SETTLING': 2567, 'REMOVED': 298} delisted share 22.4%
  2025: {'TRADING': 13250, 'SETTLING': 1566, 'REMOVED': 85} delisted share 11.1%
  2026: {'TRADING': 6505, 'SETTLING': 121} delisted share 1.8%
legs truncated by delisting inside the week (kept, return to last close): 121; symbol-weeks with |r| > 100 %: 129
  R7     p1 -0.3531 p50 -0.0153 p99 +0.5762 (n 50134)
  M28    p1 -0.5386 p50 -0.0444 p99 +1.2860 (n 50134)
  MAX30  p1 +0.0311 p50 +0.1170 p99 +0.8605 (n 50134)
  IVOL30 p1 +0.0113 p50 +0.0389 p99 +0.2084 (n 50134)
  beta   p1 -0.6750 p50 +1.2897 p99 +3.6318 (n 50134)
  BTCUSDT IVOL30 max 0.00e+00 (expected ~0); in universe 247 weeks
  r_lag1 missing: 22

STAGE 0 (no means): MDE = 2.8 x SE_NW, MDE_Holm = 3.339 x SE_NW (Holm 4, NW lag 4); gate MDE_Holm <= 0.03
  R7      n=247 SD=0.1487 SE_NW=0.0098 MDE=0.0275 MDE_Holm=0.0328 gate=FAIL -> inconclusive | neutralised SD=0.1441 SE_NW=0.0099
  M28     n=247 SD=0.1520 SE_NW=0.0086 MDE=0.0241 MDE_Holm=0.0288 gate=PASS | neutralised SD=0.1480 SE_NW=0.0082
  MAX30   n=247 SD=0.1468 SE_NW=0.0086 MDE=0.0240 MDE_Holm=0.0286 gate=PASS | neutralised SD=0.1462 SE_NW=0.0088
  IVOL30  n=247 SD=0.1728 SE_NW=0.0100 MDE=0.0280 MDE_Holm=0.0334 gate=FAIL -> inconclusive | neutralised SD=0.1729 SE_NW=0.0101
done; peak RSS 223 MB
```

### `full`

```
weeks 247; IS 185 (2022-01-03..2025-07-14); OOS 62 (2025-07-21..2026-09-21)
  R7: gate failed in stage 0 (MDE_Holm 0.0328) -> inconclusive, mean not computed
  IVOL30: gate failed in stage 0 (MDE_Holm 0.0334) -> inconclusive, mean not computed

=== MAIN (registered): mean weekly rank IC, NW lag 4; Holm over 4 signals ===
  M28
    raw IC                                       n=247 IC -0.0104 CI95 [-0.0273; +0.0065] t -1.20 p 0.2293 Holm 0.6878
    neutralised (funding 7d, log turnover 30d)   n=247 IC -0.0074 CI95 [-0.0235; +0.0086] t -0.91 p 0.3619
    neutralised + beta30 (descriptive)           n=247 IC -0.0128 CI95 [-0.0275; +0.0019] t -1.70 p 0.0885
  MAX30
    raw IC                                       n=247 IC -0.0911 CI95 [-0.1079; -0.0743] t -10.62 p 0.0000 Holm 0.0000
    neutralised (funding 7d, log turnover 30d)   n=247 IC -0.0858 CI95 [-0.1031; -0.0686] t -9.76 p 0.0000
    neutralised + beta30 (descriptive)           n=247 IC -0.0887 CI95 [-0.1019; -0.0754] t -13.13 p 0.0000
    check: trailing funding IC (fc Sunday: -0.0038) n=247 IC -0.0009 CI95 [-0.0174; +0.0156] t -0.10 p 0.9169

=== ROBUSTNESS M28 (registered; sign of the full-sample IC -1) ===
    IS                                           n=185 IC -0.0158 CI95 [-0.0363; +0.0047] t -1.51 p 0.1318
    OOS (last 25 %)                              n= 62 IC +0.0058 CI95 [-0.0196; +0.0313] t +0.45 p 0.6539
    neutralised IS                               n=185 IC -0.0115 CI95 [-0.0309; +0.0079] t -1.17 p 0.2438
    neutralised OOS                              n= 62 IC +0.0048 CI95 [-0.0204; +0.0299] t +0.37 p 0.7102
    year 2022                                    n= 52 IC -0.0151 CI95 [-0.0608; +0.0307] t -0.65 p 0.5189
    year 2023                                    n= 52 IC -0.0276 CI95 [-0.0673; +0.0120] t -1.36 p 0.1724
    year 2024                                    n= 53 IC -0.0336 CI95 [-0.0590; -0.0083] t -2.60 p 0.0094
    year 2025                                    n= 52 IC +0.0141 CI95 [-0.0278; +0.0560] t +0.66 p 0.5094
    year 2026                                    n= 38 IC +0.0186 CI95 [-0.0076; +0.0448] t +1.39 p 0.1649
    rolling 52w: min -0.0364 max +0.0231 share with the full-sample sign 70%
    without the 10 best weeks (in the effect's direction) n=237 IC +0.0035 CI95 [-0.0140; +0.0211] t +0.40 p 0.6912
    (a) without 102 symbols with any |weekly r| > 100 % n=247 IC -0.0115 CI95 [-0.0279; +0.0048] t -1.38 p 0.1674
    (b) without symbol-weeks with |r| > 100 %    n=247 IC -0.0109 CI95 [-0.0276; +0.0059] t -1.27 p 0.2045
    entry lag 1 day (open t+1 -> close t+7)      n=247 IC -0.0007 CI95 [-0.0181; +0.0167] t -0.08 p 0.9357
    TRADING-only universe (survivorship, descriptive) n=247 IC -0.0097 CI95 [-0.0283; +0.0090] t -1.01 p 0.3106
    placebo (1000 within-week shuffles): SD of mean IC 0.0048, |obs| 0.0104, p 0.0290 (2s)
    per asset: k=487 (>= 20 weeks) Q=680.1 df=486 p=0.0000 I2=0.29; within-week label permutation p=0.0020 (Q null mean 487.3)
    persistence top 10: IS top +0.4680 rest -0.0149 -> OOS top -0.0248 (n 419) rest -0.0202; diff -0.0046 CI [-0.1267; +0.1243] p 0.534; rho IS-OOS +0.194 (k 231)
    persistence top quarter: IS top +0.1582 rest -0.0497 -> OOS top +0.0087 (n 2539) rest -0.0323; diff +0.0410 CI [-0.0124; +0.0924] p 0.058; rho IS-OOS +0.194 (k 231)
    largest IS contributors (direction of the effect): [('GRASSUSDT', 32, -0.529), ('COOKIEUSDT', 23, -0.513), ('SAFEUSDT', 26, -0.484), ('FRONTUSDT', 44, -0.438), ('AEROUSDT', 28, -0.423)]
    VERDICT INPUTS: Holm p 0.6878; |IC| 0.0104 (>= 0.03); IS/OOS/no-top10 same sign False; neutralised same sign & CI excl. 0 False -> NOT CONFIRMED
    BASKET 20/20 (descriptive, direction -1 chosen in-sample): n=247 net -0.188%/wk (-9.8%/yr on gross notional) CI95 [-0.696; +0.411] MDE 0.80 pp | price +0.075 funding -0.113 cost 0.150 turnover 1.15; worst week -18.1%
    (peak RSS 203 MB)

=== ROBUSTNESS MAX30 (registered; sign of the full-sample IC -1) ===
    IS                                           n=185 IC -0.0843 CI95 [-0.1046; -0.0640] t -8.14 p 0.0000
    OOS (last 25 %)                              n= 62 IC -0.1115 CI95 [-0.1374; -0.0856] t -8.44 p 0.0000
    neutralised IS                               n=185 IC -0.0767 CI95 [-0.0973; -0.0560] t -7.29 p 0.0000
    neutralised OOS                              n= 62 IC -0.1132 CI95 [-0.1385; -0.0880] t -8.78 p 0.0000
    year 2022                                    n= 52 IC -0.0749 CI95 [-0.1045; -0.0453] t -4.96 p 0.0000
    year 2023                                    n= 52 IC -0.0689 CI95 [-0.1102; -0.0275] t -3.26 p 0.0011
    year 2024                                    n= 53 IC -0.0843 CI95 [-0.1195; -0.0491] t -4.69 p 0.0000
    year 2025                                    n= 52 IC -0.1221 CI95 [-0.1554; -0.0887] t -7.18 p 0.0000
    year 2026                                    n= 38 IC -0.1109 CI95 [-0.1444; -0.0775] t -6.49 p 0.0000
    rolling 52w: min -0.1238 max -0.0516 share with the full-sample sign 100%
    without the 10 best weeks (in the effect's direction) n=237 IC -0.0777 CI95 [-0.0952; -0.0602] t -8.72 p 0.0000
    (a) without 102 symbols with any |weekly r| > 100 % n=247 IC -0.0940 CI95 [-0.1126; -0.0755] t -9.95 p 0.0000
    (b) without symbol-weeks with |r| > 100 %    n=247 IC -0.0948 CI95 [-0.1118; -0.0778] t -10.95 p 0.0000
    entry lag 1 day (open t+1 -> close t+7)      n=247 IC -0.0834 CI95 [-0.1006; -0.0662] t -9.51 p 0.0000
    TRADING-only universe (survivorship, descriptive) n=247 IC -0.0926 CI95 [-0.1105; -0.0747] t -10.15 p 0.0000
    placebo (1000 within-week shuffles): SD of mean IC 0.0048, |obs| 0.0911, p 0.0010 (3s)
    per asset: k=487 (>= 20 weeks) Q=668.8 df=486 p=0.0000 I2=0.27; within-week label permutation p=0.0020 (Q null mean 487.9)
    persistence top 10: IS top +0.2517 rest -0.0926 -> OOS top -0.1464 (n 181) rest -0.1180; diff -0.0283 CI [-0.1729; +0.1146] p 0.654; rho IS-OOS +0.127 (k 231)
    persistence top quarter: IS top +0.0563 rest -0.1241 -> OOS top -0.0718 (n 2144) rest -0.1339; diff +0.0621 CI [+0.0206; +0.1027] p 0.002; rho IS-OOS +0.127 (k 231)
    largest IS contributors (direction of the effect): [('ARCUSDT', 22, -0.924), ('TROYUSDT', 20, -0.862), ('GOATUSDT', 34, -0.704), ('USUALUSDT', 26, -0.681), ('SWARMSUSDT', 23, -0.608)]
    VERDICT INPUTS: Holm p 0.0000; |IC| 0.0911 (>= 0.03); IS/OOS/no-top10 same sign True; neutralised same sign & CI excl. 0 True -> CONFIRMED
    BASKET 20/20 (descriptive, direction -1 chosen in-sample): n=247 net -0.121%/wk (-6.3%/yr on gross notional) CI95 [-0.709; +0.415] MDE 0.80 pp | price +0.365 funding -0.378 cost 0.108 turnover 0.81; worst week -40.3%
    (peak RSS 208 MB)

=== REGISTERED VERDICT ===
  R7      UNVERIFIABLE (gate)
  M28     NOT CONFIRMED
  MAX30   CONFIRMED
  IVOL30  UNVERIFIABLE (gate)
  card status: confirmed
```

Примітка: у M28 плацебо дає p 0.029 (|IC| 0.010 більша за шум перемішування). Але плацебо не враховує автокореляції тижнів, а NW-тест (p 0.23) враховує, і вердикт іде за ним.

### `extra` (розвідувальне, після `full`)

```
loaded 678 symbols; peak RSS 969 MB

=== EXTRA MAX30 (exploratory, after the full run) ===
  Fama-MacBeth slope on rank z: -0.050 pp/week per 1 SD rank, SE_NW 0.106, t -0.47
  quintile 1 (low -> high MAX30): mean weekly r -0.147% CI [-1.360; +1.077] median-of-quintile r -1.151% funding to a long -0.008%
  quintile 2 (low -> high MAX30): mean weekly r -0.170% CI [-1.536; +1.254] median-of-quintile r -1.484% funding to a long -0.000%
  quintile 3 (low -> high MAX30): mean weekly r -0.223% CI [-1.615; +1.224] median-of-quintile r -1.700% funding to a long -0.041%
  quintile 4 (low -> high MAX30): mean weekly r -0.004% CI [-1.321; +1.442] median-of-quintile r -2.173% funding to a long +0.001%
  quintile 5 (low -> high MAX30): mean weekly r -0.461% CI [-1.903; +1.054] median-of-quintile r -3.122% funding to a long +0.386%
  Q1 - Q5 mean price spread +0.314%/wk CI [-0.316; +0.911] (before costs and funding)
  Q1 - Q5 funding (long Q1, short Q5) -0.394%/wk
  null (equal means, lognormal with own trailing vol, 20 draws): mean IC -0.0492 (range -0.0618..-0.0397) vs observed -0.0911
    IC of trailing 30d total vol (SD of daily r) n=247 IC -0.1051 CI95 [-0.1284; -0.0818] t -8.85 p 0.0000
    MAX30 IC after neutralising trailing 30d total vol n=247 IC +0.0024 CI95 [-0.0085; +0.0134] t +0.44 p 0.6613

=== EXTRA M28 (exploratory, after the full run) ===
  Fama-MacBeth slope on rank z: -0.019 pp/week per 1 SD rank, SE_NW 0.102, t -0.19
  quintile 1 (low -> high M28): mean weekly r -0.293% CI [-1.650; +1.219] median-of-quintile r -2.241% funding to a long +0.142%
  quintile 2 (low -> high M28): mean weekly r -0.196% CI [-1.546; +1.188] median-of-quintile r -1.694% funding to a long +0.005%
  quintile 3 (low -> high M28): mean weekly r +0.013% CI [-1.389; +1.447] median-of-quintile r -1.430% funding to a long -0.017%
  quintile 4 (low -> high M28): mean weekly r -0.080% CI [-1.520; +1.409] median-of-quintile r -1.498% funding to a long -0.015%
  quintile 5 (low -> high M28): mean weekly r -0.447% CI [-1.639; +0.807] median-of-quintile r -2.574% funding to a long +0.225%
  Q1 - Q5 mean price spread +0.154%/wk CI [-0.453; +0.846] (before costs and funding)
  Q1 - Q5 funding (long Q1, short Q5) -0.082%/wk
  null (equal means, lognormal with own trailing vol, 20 draws): mean IC -0.0147 (range -0.0222..-0.0078) vs observed -0.0104
    IC of trailing 30d total vol (SD of daily r) n=247 IC -0.1051 CI95 [-0.1284; -0.0818] t -8.85 p 0.0000
    M28 IC after neutralising trailing 30d total vol n=247 IC +0.0073 CI95 [-0.0092; +0.0238] t +0.86 p 0.3882
done; peak RSS 979 MB
```
