# Сирий вивід: news events (IDEA-20261017-news-events)

Скрипт: `scripts/verify_news_events.py`, кеш `~/.cache/tradedesk-agents/news_events/`. Порядок: `ann` → `bodies` → `events` → `prices` → `stage0` → `sanity` → `report`.

**Два прогони.** Прогін 1 (нижче, перший) містить помилку даних: мертві (розраховані) перпи Binance вважались живими; його числа **не є результатом**, лишені для прозорості. Прогін 2 — після «Поправки 1» реєстрації (перп живий на t0, спот біржі анонсу), файли `*_live` у кеші; він у кінці файлу і є основою звіту.

# Прогін 1 (з помилкою даних; не використовувати)

## events (лише кількість, до реєстрації)

```
bodies to fetch: 19   (Binance "Multiple" — 12 тіл із тікерами; Bybit 7 — сторінки рендеряться JS, тікерів 0)
perp_delist: 343 raw, 334 after 72h dedupe (9 dropped); clusters 281; by source {'bybit': 257, 'binance': 77}; by year {'2023': 6, '2024': 61, '2025': 146, '2026': 121}
spot_delist: 547 raw, 547 after 72h dedupe (0 dropped); clusters 151; by source {'binance': 129, 'bybit': 366, 'okx': 52}; by year {'2023': 29, '2024': 124, '2025': 239, '2026': 155}
listing: 78 raw, 78 after 72h dedupe (0 dropped); clusters 66; by source {'binance': 78}; by year {'2023': 17, '2024': 28, '2025': 15, '2026': 18}
hack: 130 raw, 130 after 72h dedupe (0 dropped); clusters 130; by source {'defillama': 130}; by year {'2023': 35, '2024': 33, '2025': 29, '2026': 33}
shutdown: 2 raw, 2 after 72h dedupe (0 dropped); clusters 2; by source {'curated': 2}; by year {'2026': 2}
```

## prices (20-event sample for timing, then the full run)

```
sample: prices 20/20 in 24s; 11/20 events have a perp            -> estimate ~20 min for 1091 events
full:   prices 1091/1091 in 1214s; files fetched 13382; errors {}; rss=237MB
prices done: 555/1091 events have a perp; 1214s
```

## stage0 (ворота потужності; середніх нема)

```
spot_delist: events 547; with a Binance/Bybit perp at t0 138; priced 138; collisions 9; usable 129 in 65 clusters; SD(event) 0.226; SD(cluster means) 0.217; MDE 0.075
perp_delist: events 334; with a Binance/Bybit perp at t0 327; priced 327; collisions 10; usable 317 in 268 clusters; SD(event) 0.177; SD(cluster means) 0.176; MDE 0.030
hack: events 130; with a Binance/Bybit perp at t0 43; priced 43; collisions 0; usable 43 in 43 clusters; SD(event) 0.132; SD(cluster means) 0.132; MDE 0.057
listing: events 78; with a Binance/Bybit perp at t0 45; priced 45; collisions 0; usable 45 in 40 clusters; SD(event) 0.269; SD(cluster means) 0.264; MDE 0.117
GATE (primary pool spot_delist, d15_h24_base): MDE 0.075 vs 0.10 -> PASS
```

## sanity

```
curated BLAST 2026-10-02 14:45: venue bybit BLASTUSDT p0 0.0004076 react% {'+5m': -13.0, '+15m': -9.8, '+60m': -17.7, '+4h': -36.3, '+24h': -48.0, '+72h': None} spot_ratio None
curated BLUR 2026-10-12 19:59: venue binance BLURUSDT p0 0.01799 react% {'+5m': -15.4, '+15m': -16.5, '+60m': -17.1, '+4h': -23.3, '+24h': -35.0, '+72h': -39.2} spot_ratio 0.9983351831298556
collision spot_delist GFT GFTUSDT via okx 2024-12-02: perp/spot 1.257
collision spot_delist SNT SNTUSDT via binance 2025-04-08: perp/spot 1.860
collision spot_delist MDT MDTUSDT via okx 2025-06-16: perp/spot 3.737
collision spot_delist GLMR GLMRUSDT via okx 2025-09-01: perp/spot 3.585
collision spot_delist IDEX IDEXUSDT via binance 2026-03-18: perp/spot 7.450
collision spot_delist SXP SXPUSDT via binance 2026-03-18: perp/spot 6.018
collision spot_delist MDT MDTUSDT via binance 2026-04-09: perp/spot 7.475
collision spot_delist DGB DGBUSDT via bybit 2026-05-05: perp/spot 4.866
collision spot_delist VANRY VANRYUSDT via bybit 2026-08-12: perp/spot 1.360
collision perp_delist BNX BNXUSDT via bybit 2023-02-14: perp/spot 0.777
collision perp_delist LIT LITUSDT via bybit 2025-02-04: perp/spot 0.700
collision perp_delist STPT STPTUSDT via bybit 2025-05-15: perp/spot 0.751
collision perp_delist MDT MDTUSDT via bybit 2025-07-01: perp/spot 4.844
collision perp_delist IDEX IDEXUSDT via bybit 2026-02-25: perp/spot 8.631
collision perp_delist DGB DGBUSDT via bybit 2026-02-25: perp/spot 4.544
collision perp_delist NTRN NTRNUSDT via bybit 2026-04-01: perp/spot 3.262
collision perp_delist HOOK HOOKUSDT via bybit 2026-04-01: perp/spot 1.692
collision perp_delist SXP SXPUSDT via bybit 2026-04-01: perp/spot 29.818
collision perp_delist SC SCUSDT via bybit 2026-06-09: perp/spot 5.196
priced by venue: {('spot_delist', 'binance'): 126, ('spot_delist', 'bybit'): 12, ('perp_delist', 'binance'): 199, ('perp_delist', 'bybit'): 128, ('hack', 'bybit'): 5, ('hack', 'binance'): 38, ('shutdown', 'bybit'): 1, ('shutdown', 'binance'): 1, ('listing', 'bybit'): 12, ('listing', 'binance'): 33}
funding missing: {'spot_delist': 27, 'perp_delist': 24, 'hack': 2}
no spot check: {'spot_delist': 34, 'perp_delist': 223, 'hack': 43, 'shutdown': 1, 'listing': 45}
perp/spot ratio quantiles (5/50/95%): [0.9835, 0.9993, 3.456]
priced but no primary trade (gap after t0): 0 []
```

## report

```
# news events report 2026-10-17 16:33 UTC; period 2023-01-01 .. 2026-10-13 (t0 < END)
costs: fee 0.0550%/side, slippage 0.1500%/side (stress x3); stop variant 25%; placebo 20/event

## coverage
spot_delist: 547 events; no perp 409; priced 138 {'binance': 126, 'bybit': 12}; collisions 9 ['GFT(1.26)', 'SNT(1.86)', 'MDT(3.74)', 'GLMR(3.59)', 'IDEX(7.45)', 'SXP(6.02)', 'MDT(7.47)', 'DGB(4.87)', 'VANRY(1.36)']; truncated (primary) 1; funding missing 27
perp_delist: 334 events; no perp 7; priced 327 {'binance': 199, 'bybit': 128}; collisions 10 ['BNX(0.78)', 'LIT(0.70)', 'STPT(0.75)', 'MDT(4.84)', 'IDEX(8.63)', 'DGB(4.54)', 'NTRN(3.26)', 'HOOK(1.69)', 'SXP(29.82)', 'SC(5.20)']; truncated (primary) 1; funding missing 24
hack: 130 events; no perp 87; priced 43 {'bybit': 5, 'binance': 38}; collisions 0 []; truncated (primary) 0; funding missing 2
shutdown: 2 events; no perp 0; priced 2 {'bybit': 1, 'binance': 1}; collisions 0 []; truncated (primary) 0; funding missing 0
listing: 78 events; no perp 33; priced 45 {'bybit': 12, 'binance': 33}; collisions 0 []; truncated (primary) 0; funding missing 0

## reactions from t0 (mean / median), price change of the token
spot_delist (n 129): pre60 -0.3%/+0.0%; +5m -7.5%/-2.7%; +15m -7.6%/-1.5%; +60m -8.3%/-3.5%; +4h -11.9%/-5.7%; +24h -11.6%/-7.2%; +72h -8.9%/-7.9%
perp_delist (n 317): pre60 -1.0%/-0.1%; +5m -0.6%/-0.1%; +15m -0.9%/-0.2%; +60m -1.9%/-0.3%; +4h -2.0%/-0.6%; +24h -3.7%/-1.5%; +72h -4.7%/-2.2%
hack (n 43): pre60 -1.1%/+0.0%; +5m -0.1%/-0.1%; +15m -1.1%/-0.4%; +60m -0.9%/-0.5%; +4h -0.9%/+0.0%; +24h -2.7%/-2.3%; +72h -1.0%/-3.6%; day -9.0%/-5.1%
listing (n 45): pre60 +0.8%/+0.2%; +5m +17.2%/+11.3%; +15m +18.7%/+10.9%; +60m +20.9%/+15.1%; +4h +24.4%/+10.8%; +24h +18.8%/+10.7%; +72h +14.5%/-0.2%
curated BLAST 2026-10-02 14:45 venue bybit BLASTUSDT: +5m -13.0%; +15m -9.8%; +60m -17.7%; +4h -36.3%; +24h -48.0%; +72h —; short d15 h24 net +40.8%
curated BLUR 2026-10-12 19:59 venue binance BLURUSDT: +5m -15.4%; +15m -16.5%; +60m -17.1%; +4h -23.3%; +24h -35.0%; +72h -39.2%; short d15 h24 net +18.9%

## H1: short entered t0+15m, exit +24h, base costs + funding (net, on notional)
PRIMARY spot_delist: n 129 / 65 cl; mean +3.1% [-2.3%; +7.3%]; median +0.9%; win 54%
  placebo (same perps, 20 random hours before t0): n 129 / 65 cl; mean -0.3% [-0.6%; -0.0%]; median -0.1%; win 45%
  event - placebo: +3.3% [-2.2%; +7.8%] (n 129)
  MDE +7.5% (clusters 65)
  H1 criterion (mean >= 10% and CI low > 0): NOT CONFIRMED; median +0.9%

## grid (net mean [CI]) per category; delays x holds x cost/stop
spot_delist d5 h24 (n 129): base +3.5% [-1.9%; +8.0%]; stress +2.9% [-2.5%; +7.4%]; stop +4.3% [+1.1%; +7.3%]
spot_delist d5 h72 (n 129): base -0.1% [-16.2%; +9.4%]; stress -0.7% [-16.8%; +8.8%]; stop +4.9% [+1.1%; +8.7%]
spot_delist d15 h24 (n 129): base +3.1% [-2.3%; +7.3%]; stress +2.5% [-2.9%; +6.7%]; stop +4.4% [+1.3%; +7.4%]
spot_delist d15 h72 (n 129): base -0.7% [-17.2%; +9.3%]; stress -1.3% [-17.8%; +8.7%]; stop +5.3% [+1.5%; +9.0%]
spot_delist d30 h24 (n 129): base +2.6% [-1.8%; +6.2%]; stress +2.0% [-2.4%; +5.6%]; stop +3.5% [+0.8%; +6.0%]
spot_delist d30 h72 (n 129): base -0.6% [-16.4%; +8.6%]; stress -1.2% [-17.0%; +8.0%]; stop +4.5% [+1.1%; +7.9%]
perp_delist d5 h24 (n 317): base +2.3% [-0.1%; +4.6%]; stress +1.7% [-0.7%; +4.0%]; stop +2.7% [+1.0%; +4.5%]
perp_delist d5 h72 (n 317): base +3.6% [-1.5%; +7.2%]; stress +3.0% [-2.1%; +6.6%]; stop +3.5% [+1.5%; +5.7%]
perp_delist d15 h24 (n 317): base +2.2% [-0.1%; +4.3%]; stress +1.6% [-0.7%; +3.7%]; stop +2.4% [+0.7%; +4.1%]
perp_delist d15 h72 (n 317): base +3.6% [-1.0%; +6.9%]; stress +3.0% [-1.6%; +6.3%]; stop +3.4% [+1.4%; +5.4%]
perp_delist d30 h24 (n 317): base +1.5% [-0.7%; +3.6%]; stress +0.9% [-1.3%; +3.0%]; stop +1.9% [+0.3%; +3.4%]
perp_delist d30 h72 (n 317): base +3.1% [-1.6%; +6.4%]; stress +2.5% [-2.2%; +5.8%]; stop +2.9% [+1.0%; +4.8%]
hack d5 h24 (n 43): base +1.5% [-1.6%; +4.3%]; stress +0.9% [-2.2%; +3.7%]; stop +0.6% [-2.1%; +3.1%]
hack d5 h72 (n 43): base -1.0% [-12.0%; +6.2%]; stress -1.6% [-12.6%; +5.6%]; stop +1.2% [-2.5%; +4.7%]
hack d15 h24 (n 43): base +0.4% [-4.0%; +3.8%]; stress -0.2% [-4.6%; +3.2%]; stop +0.4% [-2.2%; +2.8%]
hack d15 h72 (n 43): base -3.7% [-19.5%; +5.7%]; stress -4.3% [-20.1%; +5.1%]; stop +1.0% [-2.6%; +4.5%]
hack d30 h24 (n 43): base +0.4% [-3.9%; +3.8%]; stress -0.2% [-4.5%; +3.2%]; stop -0.5% [-3.6%; +2.3%]
hack d30 h72 (n 43): base -3.3% [-18.1%; +5.6%]; stress -3.9% [-18.7%; +5.0%]; stop +0.8% [-2.9%; +4.3%]

## primary trade components (spot_delist)
gross +4.0%; funding -0.51%; cost +0.41%; MAE median +2.5%, p90 +17.5%; share MAE >= 25% 7%
BTC-hedged (short token, long BTC): n 129 / 65 cl; mean +3.2% [-2.0%; +7.3%]; median +2.2%; win 69%

## robustness (spot_delist, primary trade)
source binance: n 72 / 26 cl; mean +4.6% [-5.2%; +12.0%]; median +9.9%; win 65%
source bybit: n 43 / 32 cl; mean -0.3% [-1.9%; +1.1%]; median -0.4%; win 28%
source okx: n 14 / 7 cl; mean +5.5% [+0.8%; +10.2%]; median +2.2%; win 79%
period 2023-2024: n 25 / 15 cl; mean +7.3% [+2.0%; +12.0%]; median +3.1%; win 64%
period 2025-2026: n 104 / 50 cl; mean +2.0% [-4.5%; +7.2%]; median +0.3%; win 52%
without top 3 events: n 126 / 65 cl; mean +2.1% [-3.2%; +6.3%]; median +0.6%; win 53%
without top 5 events: n 124 / 65 cl; mean +1.6% [-3.7%; +5.8%]; median +0.5%; win 52%
without top 10 events: n 119 / 65 cl; mean +0.5% [-4.9%; +4.7%]; median +0.1%; win 50%
without top 3 announcements: n 117 / 62 cl; mean +1.5% [-4.2%; +6.0%]; median +0.1%; win 51%
top 10 events: LINA 2025-03-21 +51.6%, PROS 2025-04-08 +44.5%, FLM 2025-10-29 +35.4%, AMB 2025-02-17 +32.3%, HIFI 2025-09-03 +30.4%, KDA 2025-10-29 +29.9%, BLZ 2024-12-18 +29.2%, BAKE 2025-09-03 +27.4%, MULTI 2024-02-06 +27.0%, GFT 2024-11-26 +25.0%
bottom 5 events: VIDT 2025-04-08 -29.1%, CHESS 2026-02-02 -31.2%, HFT 2026-08-03 -53.7%, VIC 2026-08-03 -127.7%, ALPACA 2025-04-24 -149.9%

## heterogeneity (5a)
announcements with >= 2 tokens: 33; Cochran Q 2529.1 df 30 I2 0.99 p 0.000
tokens with >= 2 negative events: 120 of 339 (per-asset persistence not testable)
  by source binance: n 72 mean +4.6%
  by source bybit: n 43 mean -0.3%
  by source okx: n 14 mean +5.5%

## EXPLORATORY (after seeing results): stop fill and level, Binance-only subgroup
spot_delist all d15 h24 (n 129): base +3.1% [-2.3%; +7.3%]; stop +4.4% [+1.3%; +7.4%]; stopx +4.2% [+1.0%; +7.1%]; stop15 +4.6% [+1.7%; +7.4%]; stop15x +4.2% [+1.1%; +7.1%]; stop35 +3.9% [+0.6%; +7.0%]; stop35x +3.8% [+0.4%; +6.8%]
spot_delist all d15 h72 (n 129): base -0.7% [-17.2%; +9.3%]; stop +5.3% [+1.5%; +9.0%]; stopx +4.9% [+1.1%; +8.8%]; stop15 +4.8% [+1.2%; +8.5%]; stop15x +4.3% [+0.5%; +8.1%]; stop35 +5.2% [+1.2%; +9.1%]; stop35x +4.8% [+0.5%; +8.9%]
spot_delist binance d15 h24 (n 72): base +4.6% [-5.2%; +12.0%]; stop +7.1% [+1.8%; +11.8%]; stopx +6.6% [+1.2%; +11.4%]; stop15 +7.7% [+3.0%; +11.9%]; stop15x +7.0% [+1.9%; +11.6%]; stop35 +6.2% [+0.6%; +11.2%]; stop35x +5.9% [+0.1%; +11.0%]
spot_delist binance d15 h72 (n 72): base -1.4% [-31.6%; +16.0%]; stop +9.5% [+3.4%; +15.5%]; stopx +8.9% [+2.6%; +15.1%]; stop15 +9.1% [+3.3%; +14.9%]; stop15x +8.2% [+1.9%; +14.4%]; stop35 +9.1% [+2.5%; +15.4%]; stop35x +8.4% [+1.2%; +15.2%]
perp_delist all d15 h24 (n 317): base +2.2% [-0.1%; +4.3%]; stop +2.4% [+0.7%; +4.1%]; stopx +2.3% [+0.5%; +4.0%]; stop15 +2.4% [+0.8%; +4.0%]; stop15x +2.1% [+0.5%; +3.7%]; stop35 +2.3% [+0.5%; +4.1%]; stop35x +2.1% [+0.3%; +4.0%]
perp_delist all d15 h72 (n 317): base +3.6% [-1.0%; +6.9%]; stop +3.4% [+1.4%; +5.4%]; stopx +3.0% [+0.8%; +5.1%]; stop15 +2.7% [+0.9%; +4.5%]; stop15x +2.1% [+0.2%; +4.0%]; stop35 +3.6% [+1.4%; +5.7%]; stop35x +3.2% [+1.0%; +5.4%]
perp_delist binance d15 h24 (n 77): base +6.7% [+1.1%; +12.4%]; stop +5.6% [+0.6%; +10.8%]; stopx +5.2% [+0.1%; +10.6%]; stop15 +6.0% [+1.6%; +10.9%]; stop15x +5.5% [+0.8%; +10.5%]; stop35 +5.2% [-0.2%; +10.6%]; stop35x +4.9% [-0.6%; +10.5%]
perp_delist binance d15 h72 (n 77): base +9.0% [+3.7%; +14.4%]; stop +6.3% [+0.7%; +11.9%]; stopx +5.9% [+0.0%; +11.7%]; stop15 +5.8% [+1.3%; +10.7%]; stop15x +5.1% [+0.3%; +10.0%]; stop35 +6.7% [+1.3%; +12.3%]; stop35x +6.4% [+0.8%; +12.1%]
spot_delist binance base 2023-2024: n 19 / 10 cl; mean +8.7% [+2.1%; +14.5%]; median +10.6%; win 63%
spot_delist binance stopx 2023-2024: n 19 / 10 cl; mean +7.8% [-0.2%; +14.2%]; median +10.6%; win 63%
spot_delist binance base 2025-2026: n 53 / 16 cl; mean +3.1% [-10.2%; +12.8%]; median +9.3%; win 66%
spot_delist binance stopx 2025-2026: n 53 / 16 cl; mean +6.1% [-0.6%; +12.2%]; median +7.3%; win 64%
spot_delist all stopx d15 h24 without top 5: n 124 / 65 cl; mean +2.8% [-0.1%; +5.4%]; median +0.3%; win 52%
spot_delist all stopx d15 h24 without top 10: n 119 / 65 cl; mean +1.7% [-1.0%; +4.3%]; median -0.1%; win 50%
spot_delist binance stopx d15 h24 without top 5: n 67 / 26 cl; mean +4.2% [-0.8%; +8.8%]; median +7.2%; win 61%
spot_delist binance stopx d15 h24 without top 10: n 62 / 26 cl; mean +2.3% [-2.6%; +6.7%]; median +6.7%; win 58%
primary stopped at 25%: 9 of 129; worst-bar fill adds +3.8% loss per stopped trade

## H2 (descriptive): Binance spot listing of a token that already had a perp; LONG entered t0+15m
listing long h24: n 45 / 40 cl; mean +0.9% [-6.2%; +9.4%]; median -1.4%; win 44%
  placebo long h24: mean -0.8%
listing long h72: n 45 / 40 cl; mean -2.2% [-11.3%; +8.8%]; median -9.6%; win 33%
  placebo long h72: mean -0.6%
```

# Прогін 2 (Поправка 1: перп живий на t0, спот біржі анонсу) — основа звіту

Хеш реєстрації з поправкою (3) записано до цього прогону. Нові файли кешу: `resolve_live.json`, `px/*/1mv/` (1m з обсягом), `px/bybit_spot/`, `px/okx_spot/`, `measured_live.json`, `stage0_live.txt`, `report_live.txt`.

## prices (amended)

```
sample: prices 20/20 in 3s; files fetched 25; 11/20 events have a perp
full:   prices 1091/1091 in 373s; files fetched 2661; errors {}; rss=209MB
prices done: 512/1091 events have a perp; 373s      (first run: 555)
```

## stage0 (amended)

```
spot_delist: events 547; with a live Binance/Bybit perp at t0 109 (first-run rule had a perp but none live now: 29); priced 109; spot check {'none': 8, 'ann': 84, 'binance': 14, 'fail': 3}; usable 106 in 46 clusters; SD(event) 0.249; SD(cluster means) 0.258; MDE 0.107
perp_delist: events 334; with a live Binance/Bybit perp at t0 315 (first-run rule had a perp but none live now: 12); priced 313; spot check {'binance': 59, 'none': 144, 'ann': 109, 'fail': 1}; usable 312 in 263 clusters; SD(event) 0.192; SD(cluster means) 0.195; MDE 0.034
hack: events 130; with a live Binance/Bybit perp at t0 41 (first-run rule had a perp but none live now: 2); priced 41; spot check {'none': 41}; usable 41 in 41 clusters; SD(event) 0.136; SD(cluster means) 0.136; MDE 0.059
listing: events 78; with a live Binance/Bybit perp at t0 45 (first-run rule had a perp but none live now: 0); priced 45; spot check {'none': 45}; usable 45 in 40 clusters; SD(event) 0.269; SD(cluster means) 0.264; MDE 0.117
GATE (primary pool spot_delist, d15_h24_base): MDE 0.107 vs 0.10 -> FAIL
```

## sanity (amended)

```
curated BLAST 2026-10-02 14:45: venue bybit BLASTUSDT p0 0.0004076 react% {'+5m': -13.0, '+15m': -9.8, '+60m': -17.7, '+4h': -36.3, '+24h': -48.0, '+72h': None} spot_ratio None
curated BLUR 2026-10-12 19:59: venue binance BLURUSDT p0 0.01799 react% {'+5m': -15.4, '+15m': -16.5, '+60m': -17.1, '+4h': -23.3, '+24h': -35.0, '+72h': -39.2} spot_ratio 0.9983351831298556
collision spot_delist GFT GFTUSDT via okx 2024-12-02: perp/spot binance 1.257 announcer None
collision spot_delist TST TSTUSDT via bybit 2025-07-18: perp/spot binance 1.002 announcer 18.126
collision spot_delist NEIRO NEIROUSDT via bybit 2025-10-03: perp/spot binance 1.0 announcer 0.051
collision perp_delist ELON 10000ELONUSDT via bybit 2026-02-04: perp/spot binance None announcer 0.0
liveness vs first run: {('spot_delist', 'dropped (not live)'): 29, ('spot_delist', 'moved to another live perp'): 5, ('perp_delist', 'dropped (not live)'): 12, ('perp_delist', 'moved to another live perp'): 44, ('hack', 'dropped (not live)'): 2}
spot_delist dropped as not live (first 20): ["SRM 2023-08-15 binance ['binance:SRMUSDT']", "BTS 2023-11-27 binance ['binance:BTSUSDT']", "NULS 2025-06-16 okx ['binance:NULSUSDT']", "REN 2025-07-08 bybit ['binance:RENUSDT']", "OMG 2025-09-30 bybit ['binance:OMGUSDT']", "MEMEFI 2026-01-14 bybit ['binance:MEMEFIUSDT']", "MYRO 2026-01-14 bybit ['binance:MYROUSDT']", "PORT3 2026-02-03 bybit ['binance:PORT3USDT']", "MILK 2026-02-06 bybit ['binance:MILKUSDT']", "PERP 2026-02-19 bybit ['binance:PERPUSDT']", "X 2026-03-03 bybit ['binance:1000XUSDT']", "EPT 2026-03-17 bybit ['binance:EPTUSDT']", "SKATE 2026-03-17 bybit ['binance:SKATEUSDT']", "IDEX 2026-03-18 binance ['binance:IDEXUSDT']", "MDT 2026-04-09 binance ['binance:MDTUSDT']", "COMMON 2026-04-13 bybit ['binance:COMMONUSDT']", "UXLINK 2026-04-21 bybit ['binance:UXLINKUSDT']", "DGB 2026-05-05 bybit ['binance:DGBUSDT']", "HOOK 2026-05-05 bybit ['binance:HOOKUSDT']", "RDNT 2026-05-05 bybit ['binance:RDNTUSDT']"]
priced by venue: {('spot_delist', 'binance'): 92, ('spot_delist', 'bybit'): 17, ('perp_delist', 'binance'): 150, ('perp_delist', 'bybit'): 163, ('hack', 'bybit'): 5, ('hack', 'binance'): 36, ('shutdown', 'bybit'): 1, ('shutdown', 'binance'): 1, ('listing', 'bybit'): 12, ('listing', 'binance'): 33}
funding missing: {'perp_delist': 1}
funding partial: {'perp_delist': 16}
spot check status: {('spot_delist', 'none'): 8, ('spot_delist', 'ann'): 84, ('spot_delist', 'binance'): 14, ('spot_delist', 'fail'): 3, ('perp_delist', 'binance'): 59, ('perp_delist', 'none'): 144, ('perp_delist', 'ann'): 109, ('perp_delist', 'fail'): 1, ('hack', 'none'): 41, ('shutdown', 'none'): 1, ('shutdown', 'binance'): 1, ('listing', 'none'): 45}
both Binance and announcer spot: 36; disagree on collision: 2
no spot check: {'spot_delist': 8, 'perp_delist': 144, 'hack': 41, 'shutdown': 1, 'listing': 45}
perp/spot ratio quantiles (5/50/95%): [0.9854, 0.9991, 1.0034]
priced but no primary trade (gap after t0): 0 []
```

## report (amended)

```
# news events report 2026-10-17 17:06 UTC; period 2023-01-01 .. 2026-10-13 (t0 < END)
costs: fee 0.0550%/side, slippage 0.1500%/side (stress x3); stop variant 25%; placebo 20/event

## coverage
spot_delist: 547 events; no live perp 438 (of them settled/dead at t0 but counted in the first run: 29); priced 109 {'binance': 92, 'bybit': 17}; collisions 3 ['GFT', 'TST', 'NEIRO']; truncated (primary) 1; funding missing 0, partial 0
perp_delist: 334 events; no live perp 19 (of them settled/dead at t0 but counted in the first run: 12); priced 313 {'binance': 150, 'bybit': 163}; collisions 1 ['ELON']; truncated (primary) 3; funding missing 1, partial 16
hack: 130 events; no live perp 89 (of them settled/dead at t0 but counted in the first run: 2); priced 41 {'bybit': 5, 'binance': 36}; collisions 0 []; truncated (primary) 0; funding missing 0, partial 0
shutdown: 2 events; no live perp 0 (of them settled/dead at t0 but counted in the first run: 0); priced 2 {'bybit': 1, 'binance': 1}; collisions 0 []; truncated (primary) 0; funding missing 0, partial 0
listing: 78 events; no live perp 33 (of them settled/dead at t0 but counted in the first run: 0); priced 45 {'bybit': 12, 'binance': 33}; collisions 0 []; truncated (primary) 0; funding missing 0, partial 0

## reactions from t0 (mean / median), price change of the token
spot_delist (n 106): pre60 -0.3%/-0.2%; +5m -9.2%/-10.1%; +15m -9.4%/-10.4%; +60m -10.3%/-10.9%; +4h -15.0%/-14.9%; +24h -14.7%/-14.2%; +72h -11.2%/-19.5%
perp_delist (n 312): pre60 -1.3%/-0.5%; +5m -0.8%/-0.4%; +15m -1.1%/-0.3%; +60m -2.2%/-0.5%; +4h -2.3%/-1.2%; +24h -3.7%/-2.4%; +72h -5.3%/-5.4%
hack (n 41): pre60 -1.1%/-0.1%; +5m -0.1%/-0.1%; +15m -1.1%/-0.4%; +60m -1.0%/-0.5%; +4h -0.9%/-0.5%; +24h -2.8%/-2.4%; +72h -1.0%/-4.4%; day -9.5%/-5.3%
listing (n 45): pre60 +0.8%/+0.2%; +5m +17.2%/+11.3%; +15m +18.7%/+10.9%; +60m +20.9%/+15.1%; +4h +24.4%/+10.8%; +24h +18.8%/+10.7%; +72h +14.5%/-0.2%
curated BLAST 2026-10-02 14:45 venue bybit BLASTUSDT: +5m -13.0%; +15m -9.8%; +60m -17.7%; +4h -36.3%; +24h -48.0%; +72h —; short d15 h24 net +40.8%
curated BLUR 2026-10-12 19:59 venue binance BLURUSDT: +5m -15.4%; +15m -16.5%; +60m -17.1%; +4h -23.3%; +24h -35.0%; +72h -39.2%; short d15 h24 net +18.9%

## H1: short entered t0+15m, exit +24h, base costs + funding (net, on notional)
PRIMARY spot_delist: n 106 / 46 cl; mean +4.2% [-2.7%; +9.3%]; median +5.2%; win 70%
  placebo (same perps, 20 random hours before t0): n 106 / 46 cl; mean -0.3% [-0.6%; +0.0%]; median +0.0%; win 50%
  event - placebo: +4.4% [-2.2%; +9.6%] (n 106)
  MDE +10.7% (clusters 46)
  H1 criterion (mean >= 10% and CI low > 0): NOT CONFIRMED; median +5.2%

## grid (net mean [CI]) per category; delays x holds x cost/stop
spot_delist d5 h24 (n 106): base +4.9% [-2.2%; +10.2%]; stress +4.3% [-2.8%; +9.6%]; stop +5.8% [+1.7%; +9.3%]
spot_delist d5 h72 (n 106): base +0.2% [-20.0%; +11.6%]; stress -0.4% [-20.6%; +11.0%]; stop +6.3% [+1.8%; +10.6%]
spot_delist d15 h24 (n 106): base +4.2% [-2.7%; +9.3%]; stress +3.6% [-3.3%; +8.7%]; stop +5.8% [+2.0%; +9.2%]
spot_delist d15 h72 (n 106): base -0.6% [-21.1%; +11.5%]; stress -1.2% [-21.7%; +10.9%]; stop +6.7% [+2.1%; +11.0%]
spot_delist d30 h24 (n 106): base +3.6% [-2.0%; +7.9%]; stress +3.0% [-2.6%; +7.3%]; stop +4.7% [+1.5%; +7.6%]
spot_delist d30 h72 (n 106): base -0.4% [-19.9%; +10.7%]; stress -1.0% [-20.5%; +10.1%]; stop +5.8% [+1.7%; +9.6%]
perp_delist d5 h24 (n 312): base +2.0% [-0.5%; +4.4%]; stress +1.4% [-1.1%; +3.8%]; stop +2.9% [+1.1%; +4.7%]
perp_delist d5 h72 (n 312): base +3.6% [-1.6%; +7.3%]; stress +3.0% [-2.2%; +6.7%]; stop +3.7% [+1.4%; +5.9%]
perp_delist d15 h24 (n 312): base +1.8% [-0.6%; +4.1%]; stress +1.2% [-1.2%; +3.5%]; stop +2.6% [+0.9%; +4.3%]
perp_delist d15 h72 (n 312): base +3.6% [-1.1%; +7.0%]; stress +3.0% [-1.7%; +6.4%]; stop +3.6% [+1.4%; +5.6%]
perp_delist d30 h24 (n 312): base +1.1% [-1.2%; +3.3%]; stress +0.5% [-1.8%; +2.7%]; stop +1.9% [+0.3%; +3.4%]
perp_delist d30 h72 (n 312): base +3.0% [-1.8%; +6.4%]; stress +2.4% [-2.4%; +5.8%]; stop +2.9% [+0.8%; +5.0%]
hack d5 h24 (n 41): base +1.6% [-1.6%; +4.5%]; stress +1.0% [-2.2%; +3.9%]; stop +0.6% [-2.1%; +3.3%]
hack d5 h72 (n 41): base -1.0% [-12.6%; +6.6%]; stress -1.6% [-13.2%; +6.0%]; stop +1.3% [-2.5%; +5.0%]
hack d15 h24 (n 41): base +0.5% [-4.1%; +4.0%]; stress -0.1% [-4.7%; +3.4%]; stop +0.4% [-2.2%; +3.0%]
hack d15 h72 (n 41): base -3.9% [-20.4%; +6.1%]; stress -4.5% [-21.0%; +5.5%]; stop +1.1% [-2.7%; +4.8%]
hack d30 h24 (n 41): base +0.4% [-4.0%; +4.0%]; stress -0.2% [-4.6%; +3.4%]; stop -0.5% [-3.6%; +2.4%]
hack d30 h72 (n 41): base -3.5% [-18.9%; +6.0%]; stress -4.1% [-19.5%; +5.4%]; stop +0.9% [-2.9%; +4.5%]

## primary trade components (spot_delist)
gross +5.2%; funding -0.63%; cost +0.41%; MAE median +3.8%, p90 +21.6%; share MAE >= 25% 8%
BTC-hedged (short token, long BTC): n 106 / 46 cl; mean +4.0% [-2.6%; +8.9%]; median +4.3%; win 73%

## robustness (spot_delist, primary trade)
source binance: n 72 / 24 cl; mean +4.9% [-5.1%; +12.4%]; median +10.7%; win 68%
source bybit: n 19 / 15 cl; mean -0.1% [-3.4%; +3.1%]; median +0.6%; win 63%
source okx: n 15 / 7 cl; mean +6.0% [+1.5%; +10.5%]; median +2.9%; win 87%
only spot-checked events (announcer or Binance spot): n 98 / 43 cl; mean +3.8% [-3.5%; +9.2%]; median +4.9%; win 68%
only events checked on the announcing exchange's spot: n 84 / 35 cl; mean +4.1% [-4.6%; +10.4%]; median +6.7%; win 67%
unchecked events: HNT binance 2023-03-17 +8.1%, YFII okx 2024-02-22 +13.9%, OMG okx 2024-07-17 -2.6%, BSV okx 2025-06-30 +22.3%, X okx 2025-06-30 +21.6%, CAT bybit 2025-07-03 +7.4%, FB bybit 2025-07-03 +2.1%, BTT okx 2025-09-01 +0.7%
without partial-funding events: n 106 / 46 cl; mean +4.2% [-2.7%; +9.3%]; median +5.2%; win 70%
period 2023-2024: n 23 / 13 cl; mean +7.9% [+2.3%; +12.9%]; median +8.1%; win 70%
period 2025-2026: n 83 / 33 cl; mean +3.1% [-5.3%; +9.5%]; median +4.9%; win 70%
without top 3 events: n 103 / 46 cl; mean +3.0% [-3.8%; +8.1%]; median +4.9%; win 69%
without top 5 events: n 101 / 46 cl; mean +2.4% [-4.4%; +7.5%]; median +4.7%; win 68%
without top 10 events: n 96 / 46 cl; mean +1.1% [-5.9%; +6.2%]; median +3.5%; win 67%
without top 3 announcements: n 93 / 43 cl; mean +2.2% [-5.0%; +7.8%]; median +3.5%; win 67%
top 10 events: LINA 2025-03-21 +51.6%, PROS 2025-04-08 +44.5%, FLM 2025-10-29 +35.4%, AMB 2025-02-17 +32.3%, HIFI 2025-09-03 +30.4%, KDA 2025-10-29 +29.9%, BLZ 2024-12-18 +29.2%, BAKE 2025-09-03 +27.4%, MULTI 2024-02-06 +27.0%, GFT 2024-11-26 +25.0%
bottom 5 events: VIDT 2025-04-08 -29.1%, CHESS 2026-02-02 -31.2%, HFT 2026-08-03 -53.7%, VIC 2026-08-03 -127.7%, ALPACA 2025-04-24 -149.9%

## heterogeneity (5a)
announcements with >= 2 tokens: 27; Cochran Q 1887.4 df 26 I2 0.99 p 0.000
tokens with >= 2 negative events: 108 of 330 (per-asset persistence not testable)
  by source binance: n 72 mean +4.9%
  by source bybit: n 19 mean -0.1%
  by source okx: n 15 mean +6.0%

## EXPLORATORY (after seeing results): stop fill and level, Binance-only subgroup
spot_delist all d15 h24 (n 106): base +4.2% [-2.7%; +9.3%]; stop +5.8% [+2.0%; +9.2%]; stopx +5.5% [+1.6%; +8.9%]; stop15 +6.1% [+2.6%; +9.2%]; stop15x +5.6% [+1.8%; +8.9%]; stop35 +5.3% [+1.2%; +8.7%]; stop35x +5.0% [+0.8%; +8.6%]
spot_delist all d15 h72 (n 106): base -0.6% [-21.1%; +11.5%]; stop +6.7% [+2.1%; +11.0%]; stopx +6.1% [+1.4%; +10.7%]; stop15 +5.9% [+1.6%; +10.2%]; stop15x +5.3% [+0.6%; +9.7%]; stop35 +6.5% [+1.7%; +11.0%]; stop35x +6.0% [+0.8%; +10.8%]
spot_delist binance d15 h24 (n 72): base +4.9% [-5.1%; +12.4%]; stop +7.4% [+1.9%; +12.2%]; stopx +6.9% [+1.3%; +11.9%]; stop15 +8.0% [+3.1%; +12.3%]; stop15x +7.3% [+2.0%; +12.0%]; stop35 +6.5% [+0.6%; +11.7%]; stop35x +6.2% [+0.1%; +11.5%]
spot_delist binance d15 h72 (n 72): base -1.6% [-31.8%; +16.5%]; stop +9.3% [+3.1%; +15.6%]; stopx +8.6% [+2.0%; +15.3%]; stop15 +9.0% [+3.1%; +15.1%]; stop15x +8.2% [+1.7%; +14.6%]; stop35 +8.8% [+2.0%; +15.4%]; stop35x +8.1% [+0.5%; +15.2%]
perp_delist all d15 h24 (n 312): base +1.8% [-0.6%; +4.1%]; stop +2.6% [+0.9%; +4.3%]; stopx +2.4% [+0.7%; +4.2%]; stop15 +2.1% [+0.5%; +3.8%]; stop15x +1.7% [+0.0%; +3.4%]; stop35 +2.2% [+0.3%; +4.0%]; stop35x +2.0% [+0.1%; +3.9%]
perp_delist all d15 h72 (n 312): base +3.6% [-1.1%; +7.0%]; stop +3.6% [+1.4%; +5.6%]; stopx +3.1% [+0.8%; +5.2%]; stop15 +2.3% [+0.4%; +4.2%]; stop15x +1.4% [-0.6%; +3.4%]; stop35 +3.6% [+1.3%; +5.7%]; stop35x +3.2% [+0.8%; +5.4%]
perp_delist binance d15 h24 (n 77): base +6.8% [+1.2%; +12.5%]; stop +5.7% [+0.9%; +10.9%]; stopx +5.4% [+0.3%; +10.7%]; stop15 +5.9% [+1.4%; +10.9%]; stop15x +5.3% [+0.5%; +10.4%]; stop35 +5.3% [-0.0%; +10.8%]; stop35x +5.1% [-0.4%; +10.6%]
perp_delist binance d15 h72 (n 77): base +9.3% [+4.0%; +14.8%]; stop +6.6% [+1.1%; +12.2%]; stopx +6.2% [+0.4%; +11.9%]; stop15 +5.7% [+1.0%; +10.6%]; stop15x +4.9% [-0.0%; +9.9%]; stop35 +7.0% [+1.6%; +12.6%]; stop35x +6.7% [+1.1%; +12.3%]
spot_delist binance base 2023-2024: n 17 / 8 cl; mean +9.8% [+2.8%; +16.1%]; median +10.8%; win 71%
spot_delist binance stopx 2023-2024: n 17 / 8 cl; mean +8.8% [-0.0%; +15.8%]; median +10.8%; win 71%
spot_delist binance base 2025-2026: n 55 / 16 cl; mean +3.4% [-9.8%; +12.6%]; median +9.3%; win 67%
spot_delist binance stopx 2025-2026: n 55 / 16 cl; mean +6.3% [-0.4%; +12.2%]; median +7.3%; win 65%
spot_delist all stopx d15 h24 without top 5: n 101 / 46 cl; mean +3.9% [+0.2%; +7.1%]; median +4.7%; win 67%
spot_delist all stopx d15 h24 without top 10: n 96 / 46 cl; mean +2.6% [-0.9%; +5.8%]; median +3.3%; win 66%
spot_delist binance stopx d15 h24 without top 5: n 67 / 24 cl; mean +4.5% [-0.7%; +9.3%]; median +7.3%; win 64%
spot_delist binance stopx d15 h24 without top 10: n 62 / 24 cl; mean +2.6% [-2.5%; +7.3%]; median +6.9%; win 61%
primary stopped at 25%: 9 of 106; worst-bar fill adds +3.8% loss per stopped trade

## H2 (descriptive): Binance spot listing of a token that already had a perp; LONG entered t0+15m
listing long h24: n 45 / 40 cl; mean +0.9% [-6.2%; +9.4%]; median -1.4%; win 44%
  placebo long h24: mean -0.8%
listing long h72: n 45 / 40 cl; mean -2.2% [-11.3%; +8.8%]; median -9.6%; win 33%
  placebo long h72: mean -0.6%
```
