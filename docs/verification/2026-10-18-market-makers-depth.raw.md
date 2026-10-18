# Сирий вивід: scripts/verify_market_makers_depth.py (2026-10-18)

## prereg

```
2026-10-18T01:47:16+00:00 sha256=47ad27d0d5e1ad811e861548c467f64d8a3a633e6b4a373eb33880bea5990019 bytes=6989
```

## universe

```
eligible USD-M COIN perps (incl. delisted): 680
universe months 45; sizes 150..200; distinct members 648
member-months by today's status {'TRADING': 7367, 'absent': 224, 'SETTLING': 1183}
depth symbol-months to fetch 10239, symbol-days ~311605 (~0.45 MB per zip)
```

## fetch (таймінг вибірки і хід; 6 процесів, зупинені лімітом 2 год)

```
symbol-months missing 11226; concurrency 16
  20/11226 symbol-months, 600 files in 92s (6.5/s), eta 14.3 h, errors {}, rss 208 MB
...
shard 0/4: 1500/2550 symbol-months, 44935 files in 7109s (6.3/s), errors {}, rss 223 MB   (останній рядок перед зупинкою)
reverse 0/2: 1480/4719 symbol-months, 43521 files in 6871s (6.3/s), errors {}, rss 219 MB  (останній рядок перед зупинкою)
```

## count (без результатів подій)

```
panel 648 symbols x 1430 days loaded in 111s, rss 167 MB
months fully fetched (contiguous to 2026-09): 2024-10 .. 2026-09 (24)
event months: 2024-11 .. 2026-09
  2023: symbol-months cached 2275 / planned 2275
  2024: symbol-months cached 1637 / planned 2701
  2025: symbol-months cached 2981 / planned 2981
  2026: symbol-months cached 2279 / planned 2279
  2023: member-days 66187, with a depth file 63213, valid (>= 1440 snapshots) 62352
  2024: member-days 73200, with a depth file 43427, valid (>= 1440 snapshots) 43423
  2025: member-days 73000, with a depth file 72297, valid (>= 1440 snapshots) 72281
  2026: member-days 54600, with a depth file 54008, valid (>= 1440 snapshots) 53995
exit events ratio 0.5: n=545, dates=323, symbols=321, by year {'2024': 51, '2025': 209, '2026': 285}
exit events ratio 0.6: n=829, dates=394, symbols=410, by year {'2024': 86, '2025': 331, '2026': 412}
placebo-day sd of dr3 (vs balanced controls): 12.38 pp from 3000 random days
MDE (one-sided 5 %, power 80 %) at n=545: 1.32 pp (iid; date clustering makes it larger)
gate: n >= 100: pass
rss 176 MB
```

## report: зареєстрований зріз (події 2024-11 … 2026-09)

```
# market makers part A: book-depth liquidity exit, run 2026-10-18T04:06:54+00:00
panel 648 symbols x 1430 days loaded in 91s, rss 167 MB
months fully fetched (contiguous to 2026-09): 2024-10 .. 2026-09 (24)
event months: 2024-11 .. 2026-09
  2023: symbol-months cached 2275 / planned 2275
  2024: symbol-months cached 1637 / planned 2701
  2025: symbol-months cached 2981 / planned 2981
  2026: symbol-months cached 2279 / planned 2279
  2023: member-days 66187, with a depth file 63213, valid (>= 1440 snapshots) 62352
  2024: member-days 73200, with a depth file 43427, valid (>= 1440 snapshots) 43423
  2025: member-days 73000, with a depth file 72297, valid (>= 1440 snapshots) 72281
  2026: member-days 54600, with a depth file 54008, valid (>= 1440 snapshots) 53995

## sanity
  BTCUSDT 2023: median d1 (bid+ask within 1 %) $202.727 M, d1/30d-volume 0.0181
  BTCUSDT 2024: median d1 (bid+ask within 1 %) $251.914 M, d1/30d-volume 0.0130
  BTCUSDT 2025: median d1 (bid+ask within 1 %) $388.025 M, d1/30d-volume 0.0243
  BTCUSDT 2026: median d1 (bid+ask within 1 %) $348.396 M, d1/30d-volume 0.0321
  ETHUSDT 2023: median d1 (bid+ask within 1 %) $96.707 M, d1/30d-volume 0.0181
  ETHUSDT 2024: median d1 (bid+ask within 1 %) $103.397 M, d1/30d-volume 0.0117
  ETHUSDT 2025: median d1 (bid+ask within 1 %) $174.092 M, d1/30d-volume 0.0116
  ETHUSDT 2026: median d1 (bid+ask within 1 %) $160.856 M, d1/30d-volume 0.0181
  BLURUSDT 2023: median d1 (bid+ask within 1 %) $0.476 M, d1/30d-volume 0.0107
  BLURUSDT 2024: median d1 (bid+ask within 1 %) $1.093 M, d1/30d-volume 0.0131
  BLURUSDT 2025: median d1 (bid+ask within 1 %) $0.410 M, d1/30d-volume 0.0266
  BLURUSDT 2026: median d1 (bid+ask within 1 %) $0.194 M, d1/30d-volume 0.0190
  pooled corr(daily dln D_norm, daily return) +0.069; corr(|return|, dln D_norm) -0.118
  pooled corr(daily dln d1 raw, daily return) +0.167
  partial depth days (< 1440 snapshots) among member-days: 894

## H1 main: ratio 0.5, run 2, z_max 1, cooldown 14, metric d1 / 30d volume
events 545; analysed 545 on 323 dates, 321 symbols; dropped (no outcome / no controls) [0, 0]
mean distance to balanced controls (scaled): 1.184; event raw r3 mean -1.48 pp, controls -0.85 pp
vs balanced placebo (same date, matched on 2d z, 7d return, volume ratio):
  dr r1: mean -0.09 pp, CI95 [-1.32; +0.94], p(<0) 0.4631, Holm 0.4631
  dr r3: mean -0.63 pp, CI95 [-2.26; +0.88], p(<0) 0.2087, Holm 0.4174
  dr r7: mean -1.71 pp, CI95 [-4.02; +0.47], p(<0) 0.0671, Holm 0.2013
  dlogRV rv1: mean -13.44 pp, CI95 [-23.80; -3.00], p(>0) 0.9934, Holm 0.9934
  dlogRV rv3: mean +2.15 pp, CI95 [-4.64; +9.03], p(>0) 0.2665, Holm 0.5329
  dlogRV rv7: mean +7.35 pp, CI95 [+1.85; +12.74], p(>0) 0.0037, Holm 0.0111
vs random placebo (same symbol, random days):
  dr r1: mean +0.14 pp, CI95 [-1.20; +1.26], p(<0) 0.6086, Holm 0.6086
  dr r3: mean -0.66 pp, CI95 [-2.55; +1.14], p(<0) 0.2362, Holm 0.4724
  dr r7: mean -1.51 pp, CI95 [-4.31; +1.21], p(<0) 0.1399, Holm 0.4197
  dlogRV rv1: mean -22.92 pp, CI95 [-33.93; -11.83], p(>0) 0.9998, Holm 1.0000
  dlogRV rv3: mean -1.45 pp, CI95 [-8.91; +6.35], p(>0) 0.6418, Holm 1.0000
  dlogRV rv7: mean +8.67 pp, CI95 [+2.86; +14.95], p(>0) 0.0026, Holm 0.0078
IS (< 2026-05-16): n=408 dr3 -1.06 [-3.11; +0.77]; OOS: n=137 dr3 +0.64 [-1.53; +2.99]
economics on notional of the short leg (3 d hold):
  costs x1: hedged pair (short event, long controls) +0.23 pp; naked short +1.28 pp (funding not included)
  costs x2: hedged pair (short event, long controls) -0.17 pp; naked short +1.08 pp (funding not included)
registered decision: confirm=False; kill=True

## robustness: without top dates, by year
  without the 1 dates contributing most to a negative dr3: dr3 -0.19 [-1.49; +1.17] (dropped ['2026-04-23'])
  without the 5 dates contributing most to a negative dr3: dr3 +0.34 [-0.82; +1.57] (dropped ['2026-04-23', '2026-02-10', '2025-11-07', '2025-09-14', '2026-04-24'])
  without the 10 dates contributing most to a negative dr3: dr3 +0.75 [-0.42; +1.95] (dropped ['2026-04-23', '2026-02-10', '2025-11-07', '2025-09-14', '2026-04-24'])
  2024: n=51 dr3 -1.73 [-4.82; +0.73]
  2025: n=209 dr3 -0.37 [-2.67; +2.00]
  2026: n=285 dr3 -0.62 [-3.11; +1.63]
  trading today: n=491 dr3 -0.55 [-2.27; +1.06]
  delisted/settling today: n=54 dr3 -1.38 [-5.82; +2.60]

## sensitivity grid (all variants; dr3 vs balanced placebo, CI95 date-cluster, one-sided p<0; dlogRV3)
| variant | n | dates | dr3 pp [CI] | p | dlogRV3 [CI] |
|---|---|---|---|---|---|
| d1/vol ratio 0.4 run 2 z 0.5 | 265 | 195 | -1.87 [-5.27; +0.87] | 0.105 | +0.026 [-0.082; +0.134] |
| d1/vol ratio 0.4 run 2 z 1.0 | 343 | 229 | -1.13 [-3.86; +1.17] | 0.199 | +0.013 [-0.078; +0.103] |
| d1/vol ratio 0.4 run 2 z inf | 438 | 269 | -1.63 [-4.02; +0.59] | 0.070 | +0.066 [-0.016; +0.148] |
| d1/vol ratio 0.4 run 3 z 0.5 | 219 | 165 | +1.18 [-0.78; +3.25] | 0.883 | -0.079 [-0.185; +0.030] |
| d1/vol ratio 0.4 run 3 z 1.0 | 306 | 221 | -1.23 [-4.48; +1.62] | 0.226 | -0.049 [-0.143; +0.043] |
| d1/vol ratio 0.4 run 3 z inf | 398 | 264 | -1.53 [-4.25; +1.00] | 0.124 | +0.031 [-0.058; +0.113] |
| d1/vol ratio 0.5 run 2 z 0.5 | 388 | 259 | -0.27 [-2.56; +1.83] | 0.420 | +0.063 [-0.022; +0.146] |
| d1/vol ratio 0.5 run 2 z 1.0 | 545 | 323 | -0.63 [-2.26; +0.88] | 0.210 | +0.021 [-0.047; +0.091] |
| d1/vol ratio 0.5 run 2 z inf | 714 | 378 | +0.53 [-0.82; +1.89] | 0.777 | +0.093 [+0.035; +0.153] |
| d1/vol ratio 0.5 run 3 z 0.5 | 365 | 249 | +1.36 [-0.26; +2.96] | 0.949 | +0.043 [-0.038; +0.125] |
| d1/vol ratio 0.5 run 3 z 1.0 | 483 | 302 | +0.44 [-0.96; +1.79] | 0.728 | +0.023 [-0.050; +0.096] |
| d1/vol ratio 0.5 run 3 z inf | 647 | 361 | +0.87 [-0.49; +2.33] | 0.899 | +0.051 [-0.014; +0.113] |
| d1/vol ratio 0.6 run 2 z 0.5 | 587 | 336 | +0.42 [-0.76; +1.66] | 0.759 | +0.002 [-0.062; +0.067] |
| d1/vol ratio 0.6 run 2 z 1.0 | 829 | 394 | +0.69 [-0.23; +1.66] | 0.931 | +0.003 [-0.052; +0.057] |
| d1/vol ratio 0.6 run 2 z inf | 1092 | 444 | +0.07 [-0.95; +1.11] | 0.551 | +0.056 [+0.006; +0.105] |
| d1/vol ratio 0.6 run 3 z 0.5 | 481 | 300 | +0.34 [-1.00; +1.74] | 0.695 | +0.019 [-0.054; +0.095] |
| d1/vol ratio 0.6 run 3 z 1.0 | 705 | 382 | -0.46 [-1.57; +0.72] | 0.220 | +0.013 [-0.044; +0.074] |
| d1/vol ratio 0.6 run 3 z inf | 941 | 424 | +0.57 [-0.74; +1.81] | 0.800 | +0.071 [+0.016; +0.126] |
| d2/vol ratio 0.5 run 2 z 1 | 505 | 306 | -0.97 [-2.76; +0.72] | 0.142 | +0.004 [-0.060; +0.073] |
| d1 median/vol ratio 0.5 run 2 z 1 | 546 | 322 | -0.17 [-1.58; +1.25] | 0.398 | +0.008 [-0.061; +0.081] |
| d1 raw USD ratio 0.5 run 2 z 1 | 243 | 150 | +1.63 [-0.52; +3.89] | 0.932 | -0.029 [-0.132; +0.074] |

## per asset (5a), main config, dr3
assets with >= 3 events: 58 (of 321); events per asset max 7
| asset | n | dr3 pp | CI95 (normal) |
|---|---|---|---|
| DASH | 3 | -14.08 | [-37.92; +9.75] |
| ZEN | 4 | -13.82 | [-40.52; +12.89] |
| NAORIS | 3 | -12.94 | [-55.55; +29.66] |
| BEAT | 3 | -12.57 | [-27.17; +2.04] |
| PORTAL | 3 | -10.98 | [-33.54; +11.58] |
| TAC | 3 | -9.42 | [-19.37; +0.54] |
| MEME | 4 | -8.80 | [-16.33; -1.28] |
| OM | 4 | -7.95 | [-39.66; +23.75] |
| Q | 3 | -7.82 | [-18.23; +2.59] |
| USELESS | 3 | -5.72 | [-10.78; -0.66] |
| CAKE | 3 | -5.45 | [-11.17; +0.28] |
| BIO | 6 | -4.96 | [-15.13; +5.20] |
| ARC | 3 | -4.89 | [-10.06; +0.28] |
| STRK | 3 | -4.84 | [-9.16; -0.53] |
| HMSTR | 3 | -4.73 | [-16.29; +6.83] |
| H | 4 | -4.61 | [-27.01; +17.79] |
| FET | 4 | -4.46 | [-17.79; +8.88] |
| IN | 3 | -3.99 | [-29.58; +21.60] |
| RESOLV | 4 | -3.87 | [-11.88; +4.14] |
| UXLINK | 3 | -3.64 | [-11.91; +4.64] |
| JELLYJELLY | 7 | -3.54 | [-21.23; +14.16] |
| SHIB | 3 | -2.85 | [-5.58; -0.13] |
| POWR | 3 | -2.33 | [-6.47; +1.80] |
| TA | 4 | -0.99 | [-13.37; +11.40] |
| MELANIA | 3 | -0.56 | [-6.13; +5.01] |
| AUCTION | 3 | -0.48 | [-21.92; +20.96] |
| STG | 3 | -0.45 | [-11.13; +10.24] |
| WCT | 3 | -0.35 | [-5.29; +4.59] |
| ICP | 3 | -0.30 | [-9.58; +8.99] |
| SQD | 3 | -0.25 | [-6.04; +5.55] |
| PIPPIN | 3 | -0.12 | [-6.92; +6.67] |
| BLUAI | 3 | +0.14 | [-5.18; +5.45] |
| TAKE | 4 | +0.26 | [-7.85; +8.38] |
| MASK | 3 | +0.47 | [-10.35; +11.29] |
| ALGO | 3 | +0.83 | [-12.18; +13.83] |
| W | 3 | +0.83 | [-3.48; +5.15] |
| CRV | 3 | +0.96 | [+0.50; +1.41] |
| ONDO | 3 | +1.25 | [-5.56; +8.06] |
| PENGU | 3 | +1.31 | [-12.40; +15.02] |
| ALPINE | 3 | +1.46 | [-16.29; +19.22] |
| BONK | 4 | +2.10 | [-1.50; +5.69] |
| TON | 3 | +2.36 | [-7.87; +12.59] |
| LIGHT | 3 | +3.20 | [-9.47; +15.88] |
| ZRO | 5 | +3.43 | [-4.02; +10.88] |
| BANANA | 3 | +4.40 | [-22.44; +31.23] |
| APR | 3 | +4.53 | [-12.46; +21.52] |
| INJ | 3 | +4.76 | [-1.76; +11.28] |
| ATOM | 3 | +5.05 | [-7.42; +17.52] |
| 4 | 3 | +5.53 | [-30.04; +41.10] |
| ALCH | 3 | +5.83 | [-31.30; +42.97] |
| BR | 4 | +6.26 | [+0.80; +11.73] |
| AVAX | 3 | +6.74 | [-2.94; +16.42] |
| XMR | 3 | +8.67 | [-8.64; +25.98] |
| BAS | 5 | +10.14 | [-0.22; +20.51] |
| SOON | 4 | +11.99 | [-18.07; +42.05] |
| XPL | 3 | +12.41 | [-15.20; +40.01] |
| BERA | 3 | +15.09 | [-15.38; +45.55] |
| LAB | 3 | +28.82 | [-50.42; +108.05] |
Cochran Q (pooled var) 31.99 df 57 p 0.9969980465976706 I2 0.00; tau 0.0154
permutation within dates: Q 31.99, null mean 43.37, p95 54.89, p 0.9630184907546226
persistence (value = -dr3, short edge): ranked 100 assets, top 25: IS top +15.60 vs rest -1.80; OOS top -4.56 (n=5) vs rest +3.68 (n=25); diff -8.25 [-15.37; -1.28]; Spearman IS/OOS nan (n=0)

## H2 (secondary): liquidity entry, D > 2 x median two days, |move| < 1 sigma
events 1358; analysed 1358
  d r7: mean +0.14 pp, CI95 [-0.87; +1.15], p(>0) 0.3898, Holm 0.3898
  d vq7: mean +14.21 pp, CI95 [+9.32; +19.17], p(>0) 0.0000, Holm 0.0000

## descriptive: depth before delisting (last daily candle before 2026-09-20)
  n=1: median ratio D(last 30 d) / D(30-60 d before) 0.93; share < 0.5: 0.00; share > 1: 0.00
  reference (random member days of trading symbols) n=777: median 1.03; share < 0.5: 0.13; share > 1: 0.52
    EOSUSDT last candle 2025-05-21: ratio 0.93
```

## report --event-months 2023-02:2023-12 (додатково, поза зареєстрованим зрізом; ті самі параметри)

Розділи coverage і sanity ідентичні вище.

```
# market makers part A: book-depth liquidity exit, run 2026-10-18T04:13:41+00:00
SUPPLEMENTARY (not the registered slice): events restricted to months 2023-02:2023-12
event months: 2023-02 .. 2023-12

## H1 main: ratio 0.5, run 2, z_max 1, cooldown 14, metric d1 / 30d volume
events 140; analysed 140 on 100 dates, 103 symbols; dropped (no outcome / no controls) [0, 0]
mean distance to balanced controls (scaled): 0.963; event raw r3 mean -0.21 pp, controls +0.02 pp
vs balanced placebo (same date, matched on 2d z, 7d return, volume ratio):
  dr r1: mean +0.12 pp, CI95 [-0.81; +1.10], p(<0) 0.5946, Holm 1.0000
  dr r3: mean -0.24 pp, CI95 [-1.62; +1.20], p(<0) 0.3795, Holm 1.0000
  dr r7: mean +0.62 pp, CI95 [-1.82; +3.29], p(<0) 0.6832, Holm 1.0000
  dlogRV rv1: mean -20.15 pp, CI95 [-39.01; -1.85], p(>0) 0.9860, Holm 0.9860
  dlogRV rv3: mean +4.46 pp, CI95 [-6.22; +15.22], p(>0) 0.2050, Holm 0.4100
  dlogRV rv7: mean +8.68 pp, CI95 [-0.07; +17.90], p(>0) 0.0257, Holm 0.0771
vs random placebo (same symbol, random days):
  dr r1: mean -0.16 pp, CI95 [-1.41; +1.04], p(<0) 0.4028, Holm 0.8055
  dr r3: mean -1.19 pp, CI95 [-3.44; +1.17], p(<0) 0.1601, Holm 0.4803
  dr r7: mean -0.11 pp, CI95 [-3.85; +3.66], p(<0) 0.4842, Holm 0.8055
  dlogRV rv1: mean -24.77 pp, CI95 [-47.61; -1.87], p(>0) 0.9842, Holm 1.0000
  dlogRV rv3: mean -6.46 pp, CI95 [-18.94; +5.86], p(>0) 0.8485, Holm 1.0000
  dlogRV rv7: mean +2.68 pp, CI95 [-6.75; +12.13], p(>0) 0.2911, Holm 0.8732
IS (< 2023-10-28): n=105 dr3 +0.20 [-1.39; +1.91]; OOS: n=35 dr3 -1.55 [-4.43; +1.34]
economics on notional of the short leg (3 d hold):
  costs x1: hedged pair (short event, long controls) -0.16 pp; naked short +0.01 pp (funding not included)
  costs x2: hedged pair (short event, long controls) -0.56 pp; naked short -0.19 pp (funding not included)
registered decision: confirm=False; kill=True

## robustness: without top dates, by year
  without the 1 dates contributing most to a negative dr3: dr3 +0.08 [-1.20; +1.49] (dropped ['2023-12-27'])
  without the 5 dates contributing most to a negative dr3: dr3 +0.68 [-0.58; +2.07] (dropped ['2023-12-27', '2023-06-07', '2023-11-03', '2023-09-05', '2023-04-07'])
  without the 10 dates contributing most to a negative dr3: dr3 +1.24 [-0.06; +2.63] (dropped ['2023-12-27', '2023-06-07', '2023-11-03', '2023-09-05', '2023-04-07'])
  2023: n=140 dr3 -0.24 [-1.67; +1.22]
  trading today: n=91 dr3 +0.92 [-0.52; +2.58]
  delisted/settling today: n=49 dr3 -2.38 [-5.31; +0.49]

## sensitivity grid (all variants; dr3 vs balanced placebo, CI95 date-cluster, one-sided p<0; dlogRV3)
| variant | n | dates | dr3 pp [CI] | p | dlogRV3 [CI] |
|---|---|---|---|---|---|
| d1/vol ratio 0.4 run 2 z 0.5 | 52 | 44 | +0.49 [-2.11; +3.16] | 0.622 | -0.068 [-0.251; +0.110] |
| d1/vol ratio 0.4 run 2 z 1.0 | 78 | 62 | -1.03 [-3.00; +1.07] | 0.151 | -0.060 [-0.192; +0.080] |
| d1/vol ratio 0.4 run 2 z inf | 116 | 81 | -1.32 [-3.50; +0.97] | 0.126 | +0.032 [-0.089; +0.165] |
| d1/vol ratio 0.4 run 3 z 0.5 | 41 | 36 | +1.22 [-1.43; +4.06] | 0.815 | -0.071 [-0.248; +0.106] |
| d1/vol ratio 0.4 run 3 z 1.0 | 70 | 57 | -0.27 [-2.25; +1.61] | 0.388 | +0.044 [-0.096; +0.186] |
| d1/vol ratio 0.4 run 3 z inf | 107 | 81 | -0.98 [-2.69; +0.72] | 0.120 | +0.103 [-0.012; +0.220] |
| d1/vol ratio 0.5 run 2 z 0.5 | 94 | 72 | +0.77 [-0.56; +2.31] | 0.860 | +0.005 [-0.115; +0.131] |
| d1/vol ratio 0.5 run 2 z 1.0 | 140 | 100 | -0.24 [-1.67; +1.22] | 0.383 | +0.045 [-0.059; +0.154] |
| d1/vol ratio 0.5 run 2 z inf | 210 | 123 | -0.53 [-1.69; +0.74] | 0.207 | +0.084 [-0.009; +0.179] |
| d1/vol ratio 0.5 run 3 z 0.5 | 82 | 64 | -1.89 [-3.75; -0.11] | 0.019 | +0.044 [-0.100; +0.187] |
| d1/vol ratio 0.5 run 3 z 1.0 | 127 | 93 | -1.55 [-2.98; -0.23] | 0.013 | +0.007 [-0.108; +0.118] |
| d1/vol ratio 0.5 run 3 z inf | 188 | 119 | -0.91 [-2.46; +0.58] | 0.128 | +0.107 [+0.017; +0.196] |
| d1/vol ratio 0.6 run 2 z 0.5 | 196 | 117 | -0.95 [-2.08; +0.12] | 0.040 | +0.011 [-0.066; +0.091] |
| d1/vol ratio 0.6 run 2 z 1.0 | 286 | 151 | -0.57 [-1.48; +0.32] | 0.107 | -0.006 [-0.077; +0.066] |
| d1/vol ratio 0.6 run 2 z inf | 404 | 177 | -0.24 [-1.06; +0.60] | 0.285 | +0.056 [-0.007; +0.122] |
| d1/vol ratio 0.6 run 3 z 0.5 | 163 | 96 | -0.55 [-1.62; +0.45] | 0.133 | -0.017 [-0.096; +0.061] |
| d1/vol ratio 0.6 run 3 z 1.0 | 256 | 141 | -0.94 [-1.75; -0.11] | 0.015 | -0.054 [-0.130; +0.021] |
| d1/vol ratio 0.6 run 3 z inf | 370 | 169 | -0.48 [-1.41; +0.54] | 0.174 | +0.029 [-0.034; +0.093] |
| d2/vol ratio 0.5 run 2 z 1 | 161 | 106 | -1.14 [-2.39; +0.21] | 0.047 | +0.048 [-0.038; +0.141] |
| d1 median/vol ratio 0.5 run 2 z 1 | 147 | 108 | -0.43 [-1.72; +0.90] | 0.256 | -0.002 [-0.106; +0.099] |
| d1 raw USD ratio 0.5 run 2 z 1 | 11 | 11 | +1.63 [-0.85; +4.81] | 0.867 | +0.049 [-0.177; +0.288] |

## per asset (5a), main config, dr3
assets with >= 3 events: 5 (of 103); events per asset max 3
| asset | n | dr3 pp | CI95 (normal) |
|---|---|---|---|
| BAKE | 3 | -11.79 | [-38.76; +15.17] |
| REN | 3 | -5.26 | [-10.99; +0.48] |
| INJ | 3 | +0.33 | [-4.70; +5.35] |
| BEL | 3 | +3.46 | [+2.74; +4.18] |
| ARPA | 3 | +6.07 | [-4.92; +17.07] |
Cochran Q (pooled var) 4.35 df 4 p 0.36072374932409623 I2 0.08; tau 0.0355
permutation within dates: Q 4.35, null mean 4.45, p95 9.09, p 0.43478260869565216
persistence (value = -dr3, short edge): ranked 22 assets, top 6: IS top +6.29 vs rest -2.50; OOS top nan (n=0) vs rest +33.08 (n=1); diff -33.08 [n/a]; Spearman IS/OOS nan (n=0)

## H2 (secondary): liquidity entry, D > 2 x median two days, |move| < 1 sigma
events 205; analysed 205
  d r7: mean -0.51 pp, CI95 [-1.64; +0.66], p(>0) 0.8071, Holm 0.8071
  d vq7: mean +13.45 pp, CI95 [+3.85; +23.55], p(>0) 0.0023, Holm 0.0046
```
