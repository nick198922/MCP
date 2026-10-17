# Сирий вивід: Polymarket neg-risk, крок 0

Команда: `uv run python scripts/verify_negrisk_step0.py --stage report` (перезапуск 2026-10-16 ≈ 19:48 UTC по кешу `~/.cache/tradedesk-agents/negrisk/`; проходи 20261016T094900Z, 115800Z, 140728Z, 194340Z; пілот 094353Z — поза рішенням). Вивід скопійовано без змін.

```text
== PILOT 20261016T094353Z (exploratory, not in the decision): covered events=150
   full: unconfirmed first-snapshot net>0 [lenient K=100] events=8/138
   full: unconfirmed first-snapshot net>0 [primary K=100] events=0/138
   full: unconfirmed first-snapshot net>0 [gross K=100] events=14/138
   fuzzy: unconfirmed first-snapshot net>0 [lenient K=100] events=1/12
   fuzzy: unconfirmed first-snapshot net>0 [primary K=100] events=0/12
   fuzzy: unconfirmed first-snapshot net>0 [gross K=100] events=2/12
passes: 4

== pass 20261016T094900Z  (UTC segment 1), listing complete=True
   gamma status: {'not_negrisk': 12666, 'ok': 577, 'low_volume': 9630}
   events kept=577 errors=0 full coverage=150 simultaneous(<=5s)=150
   leg coverage 5459/12026 = 0.454
   group full: events=138 full coverage=138 legs with book 1006/1006 = 1.000
   group fuzzy: events=439 full coverage=12 legs with book 4453/11020 = 0.404
   book timestamp spread ms (last-change, reference): median=48008 p90=1761385
   batch read wall time ms: median=193 max=242
   books with neg_risk=true: 1180/1180
   group full: simultaneous snapshots=138 by category={'economy': 52, 'politics': 33, 'other': 38, 'crypto': 5, 'sports': 10}
     S_buy(100) feasible=132/138 p05=0.9916 median=1.0477 p95=1.3162
     S_sell(100) feasible=85/138 p05=0.6243 median=0.9337 p95=0.9933
     S_buy(500) feasible=132/138 p05=0.9992 median=1.1193 p95=1.9378
     S_sell(500) feasible=76/138 p05=0.3637 median=0.9271 p95=0.9930
   group fuzzy: simultaneous snapshots=12 by category={'politics': 7, 'crypto': 1, 'other': 1, 'sports': 2, 'economy': 1}
     S_buy(100) feasible=11/12 p05=0.9633 median=1.1052 p95=1.6730
     S_sell(100) feasible=8/12 p05=0.9032 median=0.9548 p95=1.0080
     S_buy(500) feasible=11/12 p05=0.9733 median=1.1592 p95=2.7564
     S_sell(500) feasible=8/12 p05=0.8440 median=0.9211 p95=1.0055
   sanity top-ask sum (books) - sum bestAsk (Gamma): median=+0.0000 p05=-0.0140 p95=+0.0180 n=144
   first-snapshot net>0 [primary K=100]: full=0 fuzzy=0
   first-snapshot net>0 [primary K=500]: full=0 fuzzy=0
   first-snapshot net>0 [lenient K=100]: full=7 fuzzy=1
   first-snapshot net>0 [lenient K=500]: full=4 fuzzy=1
   first-snapshot net>0 [upper_buf0 K=100]: full=5 fuzzy=1
   first-snapshot net>0 [upper_buf0 K=500]: full=1 fuzzy=0
   first-snapshot net>0 [upper_buf1c K=100]: full=0 fuzzy=0
   first-snapshot net>0 [upper_buf1c K=500]: full=0 fuzzy=0
   first-snapshot net>0 [gross K=100]: full=14 fuzzy=2
   first-snapshot net>0 [gross K=500]: full=9 fuzzy=2

== pass 20261016T115800Z  (UTC segment 1), listing complete=True
   gamma status: {'not_negrisk': 12618, 'ok': 581, 'low_volume': 9667, 'closed_leg_unclear': 1}
   events kept=581 errors=0 full coverage=154 simultaneous(<=5s)=154
   leg coverage 5460/12027 = 0.454
   group full: events=143 full coverage=143 legs with book 1024/1024 = 1.000
   group fuzzy: events=438 full coverage=11 legs with book 4436/11003 = 0.403
   book timestamp spread ms (last-change, reference): median=38693 p90=353411
   batch read wall time ms: median=193 max=256
   books with neg_risk=true: 1181/1181
   group full: simultaneous snapshots=143 by category={'economy': 53, 'politics': 33, 'other': 39, 'crypto': 5, 'sports': 13}
     S_buy(100) feasible=136/143 p05=0.9960 median=1.0526 p95=1.3881
     S_sell(100) feasible=85/143 p05=0.6190 median=0.9332 p95=0.9960
     S_buy(500) feasible=136/143 p05=0.9999 median=1.1236 p95=2.3974
     S_sell(500) feasible=74/143 p05=0.3474 median=0.9083 p95=0.9920
   group fuzzy: simultaneous snapshots=11 by category={'politics': 7, 'crypto': 1, 'other': 1, 'sports': 1, 'economy': 1}
     S_buy(100) feasible=11/11 p05=0.9640 median=1.1138 p95=1.6561
     S_sell(100) feasible=8/11 p05=0.8690 median=0.9432 p95=1.0080
     S_buy(500) feasible=11/11 p05=0.9760 median=1.1575 p95=2.7499
     S_sell(500) feasible=8/11 p05=0.8160 median=0.9113 p95=1.0055
   sanity top-ask sum (books) - sum bestAsk (Gamma): median=+0.0000 p05=-0.0110 p95=+0.0170 n=147
   first-snapshot net>0 [primary K=100]: full=0 fuzzy=0
   first-snapshot net>0 [primary K=500]: full=0 fuzzy=0
   first-snapshot net>0 [lenient K=100]: full=8 fuzzy=1
   first-snapshot net>0 [lenient K=500]: full=3 fuzzy=1
   first-snapshot net>0 [upper_buf0 K=100]: full=3 fuzzy=1
   first-snapshot net>0 [upper_buf0 K=500]: full=1 fuzzy=0
   first-snapshot net>0 [upper_buf1c K=100]: full=0 fuzzy=0
   first-snapshot net>0 [upper_buf1c K=500]: full=0 fuzzy=0
   first-snapshot net>0 [gross K=100]: full=14 fuzzy=2
   first-snapshot net>0 [gross K=500]: full=9 fuzzy=2

== pass 20261016T140728Z  (UTC segment 1), listing complete=True
   gamma status: {'not_negrisk': 12612, 'ok': 584, 'low_volume': 9703, 'closed_leg_unclear': 1}
   events kept=584 errors=0 full coverage=156 simultaneous(<=5s)=156
   leg coverage 5489/12072 = 0.455
   group full: events=145 full coverage=145 legs with book 1036/1036 = 1.000
   group fuzzy: events=439 full coverage=11 legs with book 4453/11036 = 0.403
   book timestamp spread ms (last-change, reference): median=31702 p90=276354
   batch read wall time ms: median=189 max=276
   books with neg_risk=true: 1193/1193
   group full: simultaneous snapshots=145 by category={'economy': 53, 'politics': 33, 'other': 40, 'crypto': 5, 'sports': 14}
     S_buy(100) feasible=138/145 p05=0.9961 median=1.0626 p95=1.4421
     S_sell(100) feasible=85/145 p05=0.5816 median=0.9222 p95=0.9911
     S_buy(500) feasible=138/145 p05=1.0044 median=1.1437 p95=2.4203
     S_sell(500) feasible=77/145 p05=0.2923 median=0.8809 p95=0.9900
   group fuzzy: simultaneous snapshots=11 by category={'politics': 7, 'crypto': 1, 'other': 1, 'sports': 1, 'economy': 1}
     S_buy(100) feasible=11/11 p05=0.9480 median=1.1228 p95=1.6548
     S_sell(100) feasible=8/11 p05=0.8627 median=0.9285 p95=1.0080
     S_buy(500) feasible=11/11 p05=0.9489 median=1.1515 p95=2.6065
     S_sell(500) feasible=8/11 p05=0.8152 median=0.9061 p95=1.0046
   sanity top-ask sum (books) - sum bestAsk (Gamma): median=+0.0000 p05=-0.0280 p95=+0.0160 n=149
   first-snapshot net>0 [primary K=100]: full=0 fuzzy=0
   first-snapshot net>0 [primary K=500]: full=0 fuzzy=0
   first-snapshot net>0 [lenient K=100]: full=8 fuzzy=1
   first-snapshot net>0 [lenient K=500]: full=6 fuzzy=1
   first-snapshot net>0 [upper_buf0 K=100]: full=5 fuzzy=1
   first-snapshot net>0 [upper_buf0 K=500]: full=2 fuzzy=1
   first-snapshot net>0 [upper_buf1c K=100]: full=0 fuzzy=0
   first-snapshot net>0 [upper_buf1c K=500]: full=0 fuzzy=0
   first-snapshot net>0 [gross K=100]: full=10 fuzzy=2
   first-snapshot net>0 [gross K=500]: full=7 fuzzy=2

== pass 20261016T194340Z  (UTC segment 2), listing complete=True
   gamma status: {'not_negrisk': 12652, 'ok': 589, 'low_volume': 9738}
   events kept=589 errors=0 full coverage=160 simultaneous(<=5s)=160
   leg coverage 5509/12098 = 0.455
   group full: events=148 full coverage=148 legs with book 1018/1018 = 1.000
   group fuzzy: events=441 full coverage=12 legs with book 4491/11080 = 0.405
   book timestamp spread ms (last-change, reference): median=42246 p90=674662
   batch read wall time ms: median=192 max=292
   books with neg_risk=true: 1212/1212
   group full: simultaneous snapshots=148 by category={'economy': 53, 'politics': 32, 'other': 37, 'crypto': 4, 'sports': 22}
     S_buy(100) feasible=135/148 p05=1.0042 median=1.0562 p95=1.4498
     S_sell(100) feasible=86/148 p05=0.6378 median=0.9433 p95=0.9960
     S_buy(500) feasible=134/148 p05=1.0070 median=1.1528 p95=2.6373
     S_sell(500) feasible=73/148 p05=0.3657 median=0.9139 p95=0.9941
   group fuzzy: simultaneous snapshots=12 by category={'politics': 7, 'crypto': 1, 'other': 1, 'sports': 2, 'economy': 1}
     S_buy(100) feasible=12/12 p05=1.0220 median=1.1852 p95=1.7079
     S_sell(100) feasible=8/12 p05=0.8514 median=0.9369 p95=1.0070
     S_buy(500) feasible=12/12 p05=1.0220 median=1.3947 p95=3.1113
     S_sell(500) feasible=8/12 p05=0.8112 median=0.9202 p95=1.0049
   sanity top-ask sum (books) - sum bestAsk (Gamma): median=+0.0000 p05=-0.0230 p95=+0.0100 n=147
   first-snapshot net>0 [primary K=100]: full=0 fuzzy=0
   first-snapshot net>0 [primary K=500]: full=0 fuzzy=0
   first-snapshot net>0 [lenient K=100]: full=4 fuzzy=1
   first-snapshot net>0 [lenient K=500]: full=4 fuzzy=1
   first-snapshot net>0 [upper_buf0 K=100]: full=2 fuzzy=1
   first-snapshot net>0 [upper_buf0 K=500]: full=2 fuzzy=1
   first-snapshot net>0 [upper_buf1c K=100]: full=0 fuzzy=0
   first-snapshot net>0 [upper_buf1c K=500]: full=0 fuzzy=0
   first-snapshot net>0 [gross K=100]: full=7 fuzzy=2
   first-snapshot net>0 [gross K=500]: full=6 fuzzy=2

== across passes
UTC segments covered: [1, 2]; pass gaps h: [2.15, 2.16, 5.6]
simultaneous full-group event-snapshots: 574
distinct events: full=161 fuzzy=442
events by label (distinct events): {'full:confirmed_lenient': 11, 'full:first_lenient': 11, 'fuzzy:confirmed_lenient': 1, 'fuzzy:first_lenient': 1}

confirmed deviations (rows): 31
  20261016T094900Z id=51456 [economy/full ] side=buy legs=13 S=0.99669 net100={'primary': -0.06457, 'lenient': 0.001848, 'upper_buf0': 0.00043, 'upper_buf1c': -0.12957, 'gross': 0.00331} net500={'primary': -0.067167, 'lenient': -0.000628, 'upper_buf0': -0.002167, 'upper_buf1c': -0.132167, 'gross': 0.000835} thin=False min_leg=0.001 stale_legs=7 days=76.799 ann_primary=-0.308 slug=how-many-fed-rate-cuts-in-2026
  20261016T094900Z id=106981 [crypto/full ] side=buy legs=3 S=0.98 net100={'primary': -0.013232, 'lenient': 0.012522, 'upper_buf0': 0.001768, 'upper_buf1c': -0.028232, 'gross': 0.02} net500={'primary': -0.016362, 'lenient': 0.009349, 'upper_buf0': -0.001362, 'upper_buf1c': -0.031362, 'gross': 0.01682} thin=False min_leg=0.09 stale_legs=2 days=76.799 ann_primary=-0.064 slug=bitcoin-vs-gold-vs-sp-500-in-2026
  20261016T094900Z id=200252 [economy/full ] side=buy legs=7 S=0.97299 net100={'primary': -0.034832, 'lenient': 0.019708, 'upper_buf0': 0.000168, 'upper_buf1c': -0.069832, 'gross': 0.02701} net500={'primary': -0.041628, 'lenient': 0.012936, 'upper_buf0': -0.006628, 'upper_buf1c': -0.076628, 'gross': 0.020304} thin=False min_leg=0.019798 stale_legs=2 days=441.799 ann_primary=-0.03 slug=openai-ipo-closing-market-cap-554
  20261016T094900Z id=770107 [politics/full ] side=buy legs=5 S=0.978531 net100={'primary': -0.02916, 'lenient': 0.00959, 'upper_buf0': -0.00416, 'upper_buf1c': -0.05416, 'gross': 0.021469} net500={'primary': -0.05276, 'lenient': -0.012957, 'upper_buf0': -0.02776, 'upper_buf1c': -0.07776, 'gross': -0.001043} thin=False min_leg=0.002531 stale_legs=3 days=12.59 ann_primary=-0.864 slug=reserve-bank-of-new-zealand-decision-in-october-20260729164521631
  20261016T094900Z id=803096 [sports/fuzzy augmented,placeholder] side=buy legs=5 S=0.963254 net100={'primary': -0.018676, 'lenient': 0.02609, 'upper_buf0': 0.006324, 'upper_buf1c': -0.043676, 'gross': 0.036746} net500={'primary': -0.028732, 'lenient': 0.015942, 'upper_buf0': -0.003732, 'upper_buf1c': -0.053732, 'gross': 0.026743} thin=False min_leg=0.002 stale_legs=3 days=1374.757 ann_primary=-0.005 slug=world-cup-2030-location-of-final-20260805200159829
  20261016T094900Z id=807644 [economy/full ] side=buy legs=5 S=0.982501 net100={'primary': -0.02534, 'lenient': 0.009328, 'upper_buf0': -0.00034, 'upper_buf1c': -0.05034, 'gross': 0.017499} net500={'primary': -0.035462, 'lenient': -0.000393, 'upper_buf0': -0.010462, 'upper_buf1c': -0.060462, 'gross': 0.007815} thin=False min_leg=0.003 stale_legs=0 days=20.59 ann_primary=-0.457 slug=bank-of-mexico-decision-in-november
  20261016T094900Z id=995361 [other/full ] side=buy legs=6 S=0.9654 net100={'primary': -0.024573, 'lenient': 0.027817, 'upper_buf0': 0.005427, 'upper_buf1c': -0.054573, 'gross': 0.0346} net500={'primary': -0.037746, 'lenient': 0.015062, 'upper_buf0': -0.007746, 'upper_buf1c': -0.067746, 'gross': 0.021889} thin=False min_leg=0.0334 stale_legs=6 days=441.799 ann_primary=-0.021 slug=which-millennium-prize-problem-will-ai-solve-next
  20261016T094900Z id=1177878 [politics/full ] side=buy legs=4 S=0.970931 net100={'primary': -0.015836, 'lenient': 0.021219, 'upper_buf0': 0.004164, 'upper_buf1c': -0.035836, 'gross': 0.029069} net500={'primary': -0.015894, 'lenient': 0.021163, 'upper_buf0': 0.004106, 'upper_buf1c': -0.035894, 'gross': 0.029014} thin=False min_leg=0.082931 stale_legs=0 days=18.799 ann_primary=-0.317 slug=who-wins-the-senate-pending-oil-below-85-before-the-midterms
  20261016T115800Z id=51456 [economy/full ] side=buy legs=13 S=0.997543 net100={'primary': -0.065465, 'lenient': 0.000994, 'upper_buf0': -0.000465, 'upper_buf1c': -0.130465, 'gross': 0.002457} net500={'primary': -0.067369, 'lenient': -0.00082, 'upper_buf0': -0.002369, 'upper_buf1c': -0.132369, 'gross': 0.000643} thin=False min_leg=0.001 stale_legs=6 days=76.709 ann_primary=-0.312 slug=how-many-fed-rate-cuts-in-2026
  20261016T115800Z id=106981 [crypto/full ] side=buy legs=3 S=0.98 net100={'primary': -0.013232, 'lenient': 0.012522, 'upper_buf0': 0.001768, 'upper_buf1c': -0.028232, 'gross': 0.02} net500={'primary': -0.020171, 'lenient': 0.005489, 'upper_buf0': -0.005171, 'upper_buf1c': -0.035171, 'gross': 0.012951} thin=False min_leg=0.09 stale_legs=2 days=76.709 ann_primary=-0.064 slug=bitcoin-vs-gold-vs-sp-500-in-2026
  20261016T115800Z id=200252 [economy/full ] side=buy legs=7 S=0.96799 net100={'primary': -0.029735, 'lenient': 0.024771, 'upper_buf0': 0.005265, 'upper_buf1c': -0.064735, 'gross': 0.03201} net500={'primary': -0.044361, 'lenient': 0.010368, 'upper_buf0': -0.009361, 'upper_buf1c': -0.079361, 'gross': 0.017737} thin=False min_leg=0.019798 stale_legs=2 days=441.709 ann_primary=-0.025 slug=openai-ipo-closing-market-cap-554
  20261016T115800Z id=638395 [other/full ] side=buy legs=7 S=0.994167 net100={'primary': -0.040013, 'lenient': 0.001071, 'upper_buf0': -0.005013, 'upper_buf1c': -0.075013, 'gross': 0.005833} net500={'primary': -0.146207, 'lenient': -0.102179, 'upper_buf0': -0.111207, 'upper_buf1c': -0.181207, 'gross': -0.096885} thin=False min_leg=0.001 stale_legs=0 days=76.709 ann_primary=-0.192 slug=how-many-cities-will-waymo-operate-in-by-december-31-20260626203648850
  20261016T115800Z id=770107 [politics/full ] side=buy legs=5 S=0.978531 net100={'primary': -0.02916, 'lenient': 0.00959, 'upper_buf0': -0.00416, 'upper_buf1c': -0.05416, 'gross': 0.021469} net500={'primary': -0.056145, 'lenient': -0.016411, 'upper_buf0': -0.031145, 'upper_buf1c': -0.081145, 'gross': -0.004458} thin=False min_leg=0.002531 stale_legs=3 days=12.501 ann_primary=-0.87 slug=reserve-bank-of-new-zealand-decision-in-october-20260729164521631
  20261016T115800Z id=803096 [sports/fuzzy augmented,placeholder] side=buy legs=5 S=0.964 net100={'primary': -0.019426, 'lenient': 0.025333, 'upper_buf0': 0.005574, 'upper_buf1c': -0.044426, 'gross': 0.036} net500={'primary': -0.03148, 'lenient': 0.013177, 'upper_buf0': -0.00648, 'upper_buf1c': -0.05648, 'gross': 0.024016} thin=False min_leg=0.002 stale_legs=3 days=1374.667 ann_primary=-0.005 slug=world-cup-2030-location-of-final-20260805200159829
  20261016T115800Z id=807644 [economy/full ] side=buy legs=5 S=0.983501 net100={'primary': -0.026372, 'lenient': 0.008315, 'upper_buf0': -0.001372, 'upper_buf1c': -0.051372, 'gross': 0.016499} net500={'primary': -0.040755, 'lenient': -0.005653, 'upper_buf0': -0.015755, 'upper_buf1c': -0.065755, 'gross': 0.002601} thin=False min_leg=0.003 stale_legs=0 days=20.501 ann_primary=-0.477 slug=bank-of-mexico-decision-in-november
  20261016T115800Z id=995361 [other/full ] side=buy legs=6 S=0.97215 net100={'primary': -0.031538, 'lenient': 0.021021, 'upper_buf0': -0.001538, 'upper_buf1c': -0.061538, 'gross': 0.02785} net500={'primary': -0.070954, 'lenient': -0.017462, 'upper_buf0': -0.040954, 'upper_buf1c': -0.100954, 'gross': -0.010368} thin=False min_leg=0.0334 stale_legs=6 days=441.709 ann_primary=-0.027 slug=which-millennium-prize-problem-will-ai-solve-next
  20261016T115800Z id=1177878 [politics/full ] side=buy legs=4 S=0.970931 net100={'primary': -0.015836, 'lenient': 0.021219, 'upper_buf0': 0.004164, 'upper_buf1c': -0.035836, 'gross': 0.029069} net500={'primary': -0.015894, 'lenient': 0.021163, 'upper_buf0': 0.004106, 'upper_buf1c': -0.035894, 'gross': 0.029014} thin=False min_leg=0.082931 stale_legs=0 days=18.709 ann_primary=-0.318 slug=who-wins-the-senate-pending-oil-below-85-before-the-midterms
  20261016T140728Z id=51456 [economy/full ] side=buy legs=13 S=0.996078 net100={'primary': -0.063928, 'lenient': 0.002461, 'upper_buf0': 0.001072, 'upper_buf1c': -0.128928, 'gross': 0.003922} net500={'primary': -0.065542, 'lenient': 0.000923, 'upper_buf0': -0.000542, 'upper_buf1c': -0.130542, 'gross': 0.002384} thin=False min_leg=0.001 stale_legs=6 days=76.619 ann_primary=-0.306 slug=how-many-fed-rate-cuts-in-2026
  20261016T140728Z id=106981 [crypto/full ] side=buy legs=3 S=0.99 net100={'primary': -0.023076, 'lenient': 0.002545, 'upper_buf0': -0.008076, 'upper_buf1c': -0.038076, 'gross': 0.01} net500={'primary': -0.023076, 'lenient': 0.002545, 'upper_buf0': -0.008076, 'upper_buf1c': -0.038076, 'gross': 0.01} thin=False min_leg=0.09 stale_legs=1 days=76.619 ann_primary=-0.111 slug=bitcoin-vs-gold-vs-sp-500-in-2026
  20261016T140728Z id=200252 [economy/full ] side=buy legs=7 S=0.957891 net100={'primary': -0.019634, 'lenient': 0.034984, 'upper_buf0': 0.015366, 'upper_buf1c': -0.054634, 'gross': 0.042109} net500={'primary': -0.033572, 'lenient': 0.021284, 'upper_buf0': 0.001428, 'upper_buf1c': -0.068572, 'gross': 0.028534} thin=False min_leg=0.019798 stale_legs=2 days=441.619 ann_primary=-0.017 slug=openai-ipo-closing-market-cap-554
  20261016T140728Z id=803096 [sports/fuzzy augmented,placeholder] side=buy legs=5 S=0.948 net100={'primary': -0.003328, 'lenient': 0.041573, 'upper_buf0': 0.021672, 'upper_buf1c': -0.028328, 'gross': 0.052} net500={'primary': -0.004233, 'lenient': 0.040689, 'upper_buf0': 0.020767, 'upper_buf1c': -0.029233, 'gross': 0.051126} thin=False min_leg=0.002 stale_legs=3 days=1374.577 ann_primary=-0.001 slug=world-cup-2030-location-of-final-20260805200159829
  20261016T140728Z id=995361 [other/full ] side=buy legs=6 S=0.9676 net100={'primary': -0.026851, 'lenient': 0.025608, 'upper_buf0': 0.003149, 'upper_buf1c': -0.056851, 'gross': 0.0324} net500={'primary': -0.068534, 'lenient': -0.014824, 'upper_buf0': -0.038534, 'upper_buf1c': -0.098534, 'gross': -0.007861} thin=False min_leg=0.0334 stale_legs=6 days=441.619 ann_primary=-0.023 slug=which-millennium-prize-problem-will-ai-solve-next
  20261016T140728Z id=1075157 [economy/full ] side=buy legs=5 S=0.980456 net100={'primary': -0.019938, 'lenient': 0.012415, 'upper_buf0': 0.005062, 'upper_buf1c': -0.044938, 'gross': 0.019544} net500={'primary': -0.083307, 'lenient': -0.050888, 'upper_buf0': -0.058307, 'upper_buf1c': -0.108307, 'gross': -0.044153} thin=False min_leg=0.002 stale_legs=0 days=54.411 ann_primary=-0.136 slug=swiss-national-bank-decision-in-december-2026
  20261016T140728Z id=1172100 [politics/full ] side=buy legs=3 S=0.997696 net100={'primary': -0.016901, 'lenient': 0.000251, 'upper_buf0': -0.001901, 'upper_buf1c': -0.031901, 'gross': 0.002304} net500={'primary': -0.017153, 'lenient': 8e-06, 'upper_buf0': -0.002153, 'upper_buf1c': -0.032153, 'gross': 0.002061} thin=False min_leg=0.025 stale_legs=0 days=18.619 ann_primary=-0.332 slug=who-wins-the-senate-pending-hormuz-agreement-before-the-midterms
  20261016T140728Z id=1177878 [politics/full ] side=buy legs=4 S=0.969931 net100={'primary': -0.014806, 'lenient': 0.022226, 'upper_buf0': 0.005194, 'upper_buf1c': -0.034806, 'gross': 0.030069} net500={'primary': -0.014863, 'lenient': 0.022171, 'upper_buf0': 0.005137, 'upper_buf1c': -0.034863, 'gross': 0.030014} thin=False min_leg=0.082931 stale_legs=0 days=18.619 ann_primary=-0.299 slug=who-wins-the-senate-pending-oil-below-85-before-the-midterms
  20261016T140728Z id=1177906 [politics/full ] side=buy legs=4 S=0.986 net100={'primary': -0.02729, 'lenient': 0.005284, 'upper_buf0': -0.00729, 'upper_buf1c': -0.04729, 'gross': 0.014} net500={'primary': -0.029191, 'lenient': 0.003446, 'upper_buf0': -0.009191, 'upper_buf1c': -0.049191, 'gross': 0.012167} thin=False min_leg=0.032 stale_legs=0 days=18.619 ann_primary=-0.543 slug=who-wins-the-senate-pending-hormuz-agreement-and-oil-below-80
  20261016T194340Z id=106981 [crypto/full ] side=buy legs=3 S=0.984054 net100={'primary': -0.016985, 'lenient': 0.008557, 'upper_buf0': -0.001985, 'upper_buf1c': -0.031985, 'gross': 0.015946} net500={'primary': -0.021858, 'lenient': 0.003747, 'upper_buf0': -0.006858, 'upper_buf1c': -0.036858, 'gross': 0.011189} thin=False min_leg=0.09 stale_legs=0 days=76.386 ann_primary=-0.082 slug=bitcoin-vs-gold-vs-sp-500-in-2026
  20261016T194340Z id=200252 [economy/full ] side=buy legs=7 S=0.959651 net100={'primary': -0.021437, 'lenient': 0.033206, 'upper_buf0': 0.013563, 'upper_buf1c': -0.056437, 'gross': 0.040349} net500={'primary': -0.029817, 'lenient': 0.024977, 'upper_buf0': 0.005183, 'upper_buf1c': -0.064817, 'gross': 0.032191} thin=False min_leg=0.019459 stale_legs=2 days=441.386 ann_primary=-0.018 slug=openai-ipo-closing-market-cap-554
  20261016T194340Z id=803096 [sports/fuzzy augmented,placeholder] side=buy legs=5 S=0.948 net100={'primary': -0.003328, 'lenient': 0.041573, 'upper_buf0': 0.021672, 'upper_buf1c': -0.028328, 'gross': 0.052} net500={'primary': -0.010085, 'lenient': 0.034777, 'upper_buf0': 0.014915, 'upper_buf1c': -0.035085, 'gross': 0.045303} thin=False min_leg=0.002 stale_legs=3 days=1374.344 ann_primary=-0.001 slug=world-cup-2030-location-of-final-20260805200159829
  20261016T194340Z id=1177878 [politics/full ] side=buy legs=4 S=0.967 net100={'primary': -0.011782, 'lenient': 0.025178, 'upper_buf0': 0.008218, 'upper_buf1c': -0.031782, 'gross': 0.033} net500={'primary': -0.011782, 'lenient': 0.025178, 'upper_buf0': 0.008218, 'upper_buf1c': -0.031782, 'gross': 0.033} thin=False min_leg=0.082 stale_legs=0 days=18.386 ann_primary=-0.242 slug=who-wins-the-senate-pending-oil-below-85-before-the-midterms
  20261016T194340Z id=1177906 [politics/full ] side=buy legs=4 S=0.987 net100={'primary': -0.028303, 'lenient': 0.004271, 'upper_buf0': -0.008303, 'upper_buf1c': -0.048303, 'gross': 0.013} net500={'primary': -0.028653, 'lenient': 0.003933, 'upper_buf0': -0.008653, 'upper_buf1c': -0.048653, 'gross': 0.012663} thin=False min_leg=0.032 stale_legs=0 days=18.386 ann_primary=-0.569 slug=who-wins-the-senate-pending-hormuz-agreement-and-oil-below-80

exploratory variant 'lower fee + 0.005/leg' on confirmed rows (recomputed from cached recheck books):
  20261016T094900Z id=51456 [full] buy K=100: net=-0.0632 per set, on capital -0.0634, ann=-0.301 stale_legs=7
  20261016T094900Z id=51456 [full] buy K=500: net=-0.0656 per set, on capital -0.0657, ann=-0.312 stale_legs=7
  20261016T094900Z id=106981 [full] buy K=100: net=-0.0025 per set, on capital -0.0025, ann=-0.012 stale_legs=2
  20261016T094900Z id=106981 [full] buy K=500: net=-0.0057 per set, on capital -0.0057, ann=-0.027 stale_legs=2
  20261016T094900Z id=200252 [full] buy K=100: net=-0.0153 per set, on capital -0.0157, ann=-0.013 stale_legs=2
  20261016T094900Z id=200252 [full] buy K=500: net=-0.0221 per set, on capital -0.0225, ann=-0.019 stale_legs=2
  20261016T094900Z id=770107 [full] buy K=100: net=-0.0154 per set, on capital -0.0157, ann=-0.457 stale_legs=3
  20261016T094900Z id=770107 [full] buy K=500: net=-0.0380 per set, on capital -0.0379, ann=-1.099 stale_legs=3
  20261016T094900Z id=803096 [fuzzy] buy K=100: net=+0.0011 per set, on capital +0.0011, ann=0.0 stale_legs=3
  20261016T094900Z id=803096 [fuzzy] buy K=500: net=-0.0091 per set, on capital -0.0093, ann=-0.002 stale_legs=3
  20261016T094900Z id=807644 [full] buy K=100: net=-0.0157 per set, on capital -0.0160, ann=-0.283 stale_legs=0
  20261016T094900Z id=807644 [full] buy K=500: net=-0.0254 per set, on capital -0.0256, ann=-0.454 stale_legs=0
  20261016T094900Z id=995361 [full] buy K=100: net=-0.0022 per set, on capital -0.0023, ann=-0.002 stale_legs=6
  20261016T094900Z id=995361 [full] buy K=500: net=-0.0149 per set, on capital -0.0153, ann=-0.013 stale_legs=6
  20261016T094900Z id=1177878 [full] buy K=100: net=+0.0012 per set, on capital +0.0013, ann=0.024 stale_legs=0
  20261016T094900Z id=1177878 [full] buy K=500: net=+0.0012 per set, on capital +0.0012, ann=0.023 stale_legs=0
  20261016T115800Z id=51456 [full] buy K=100: net=-0.0640 per set, on capital -0.0642, ann=-0.305 stale_legs=6
  20261016T115800Z id=51456 [full] buy K=500: net=-0.0658 per set, on capital -0.0659, ann=-0.313 stale_legs=6
  20261016T115800Z id=106981 [full] buy K=100: net=-0.0025 per set, on capital -0.0025, ann=-0.012 stale_legs=2
  20261016T115800Z id=106981 [full] buy K=500: net=-0.0095 per set, on capital -0.0096, ann=-0.046 stale_legs=2
  20261016T115800Z id=200252 [full] buy K=100: net=-0.0102 per set, on capital -0.0106, ann=-0.009 stale_legs=2
  20261016T115800Z id=200252 [full] buy K=500: net=-0.0246 per set, on capital -0.0251, ann=-0.021 stale_legs=2
  20261016T115800Z id=638395 [full] buy K=100: net=-0.0339 per set, on capital -0.0341, ann=-0.162 stale_legs=0
  20261016T115800Z id=638395 [full] buy K=500: net=-0.1372 per set, on capital -0.1251, ann=-0.595 stale_legs=0
  20261016T115800Z id=770107 [full] buy K=100: net=-0.0154 per set, on capital -0.0157, ann=-0.46 stale_legs=3
  20261016T115800Z id=770107 [full] buy K=500: net=-0.0414 per set, on capital -0.0412, ann=-1.204 stale_legs=3
  20261016T115800Z id=803096 [fuzzy] buy K=100: net=+0.0003 per set, on capital +0.0003, ann=0.0 stale_legs=3
  20261016T115800Z id=803096 [fuzzy] buy K=500: net=-0.0118 per set, on capital -0.0121, ann=-0.003 stale_legs=3
  20261016T115800Z id=807644 [full] buy K=100: net=-0.0167 per set, on capital -0.0170, ann=-0.302 stale_legs=0
  20261016T115800Z id=807644 [full] buy K=500: net=-0.0307 per set, on capital -0.0307, ann=-0.547 stale_legs=0
  20261016T115800Z id=995361 [full] buy K=100: net=-0.0090 per set, on capital -0.0092, ann=-0.008 stale_legs=6
  20261016T115800Z id=995361 [full] buy K=500: net=-0.0475 per set, on capital -0.0470, ann=-0.039 stale_legs=6
  20261016T115800Z id=1177878 [full] buy K=100: net=+0.0012 per set, on capital +0.0013, ann=0.024 stale_legs=0
  20261016T115800Z id=1177878 [full] buy K=500: net=+0.0012 per set, on capital +0.0012, ann=0.023 stale_legs=0
  20261016T140728Z id=51456 [full] buy K=100: net=-0.0625 per set, on capital -0.0628, ann=-0.299 stale_legs=6
  20261016T140728Z id=51456 [full] buy K=500: net=-0.0641 per set, on capital -0.0642, ann=-0.306 stale_legs=6
  20261016T140728Z id=106981 [full] buy K=100: net=-0.0125 per set, on capital -0.0126, ann=-0.06 stale_legs=1
  20261016T140728Z id=106981 [full] buy K=500: net=-0.0125 per set, on capital -0.0126, ann=-0.06 stale_legs=1
  20261016T140728Z id=200252 [full] buy K=100: net=-0.0000 per set, on capital -0.0000, ann=-0.0 stale_legs=2
  20261016T140728Z id=200252 [full] buy K=500: net=-0.0137 per set, on capital -0.0141, ann=-0.012 stale_legs=2
  20261016T140728Z id=803096 [fuzzy] buy K=100: net=+0.0166 per set, on capital +0.0175, ann=0.005 stale_legs=3
  20261016T140728Z id=803096 [fuzzy] buy K=500: net=+0.0157 per set, on capital +0.0165, ann=0.004 stale_legs=3
  20261016T140728Z id=995361 [full] buy K=100: net=-0.0044 per set, on capital -0.0045, ann=-0.004 stale_legs=6
  20261016T140728Z id=995361 [full] buy K=500: net=-0.0448 per set, on capital -0.0445, ann=-0.037 stale_legs=6
  20261016T140728Z id=1075157 [full] buy K=100: net=-0.0126 per set, on capital -0.0128, ann=-0.086 stale_legs=0
  20261016T140728Z id=1075157 [full] buy K=500: net=-0.0759 per set, on capital -0.0727, ann=-0.488 stale_legs=0
  20261016T140728Z id=1172100 [full] buy K=100: net=-0.0147 per set, on capital -0.0148, ann=-0.29 stale_legs=0
  20261016T140728Z id=1172100 [full] buy K=500: net=-0.0150 per set, on capital -0.0150, ann=-0.295 stale_legs=0
  20261016T140728Z id=1177878 [full] buy K=100: net=+0.0022 per set, on capital +0.0023, ann=0.045 stale_legs=0
  20261016T140728Z id=1177878 [full] buy K=500: net=+0.0022 per set, on capital +0.0022, ann=0.044 stale_legs=0
  20261016T140728Z id=1177906 [full] buy K=100: net=-0.0147 per set, on capital -0.0149, ann=-0.293 stale_legs=0
  20261016T140728Z id=1177906 [full] buy K=500: net=-0.0166 per set, on capital -0.0168, ann=-0.329 stale_legs=0
  20261016T194340Z id=106981 [full] buy K=100: net=-0.0064 per set, on capital -0.0065, ann=-0.031 stale_legs=0
  20261016T194340Z id=106981 [full] buy K=500: net=-0.0113 per set, on capital -0.0114, ann=-0.054 stale_legs=0
  20261016T194340Z id=200252 [full] buy K=100: net=-0.0018 per set, on capital -0.0019, ann=-0.002 stale_legs=2
  20261016T194340Z id=200252 [full] buy K=500: net=-0.0100 per set, on capital -0.0104, ann=-0.009 stale_legs=2
  20261016T194340Z id=803096 [fuzzy] buy K=100: net=+0.0166 per set, on capital +0.0175, ann=0.005 stale_legs=3
  20261016T194340Z id=803096 [fuzzy] buy K=500: net=+0.0098 per set, on capital +0.0102, ann=0.003 stale_legs=3
  20261016T194340Z id=1177878 [full] buy K=100: net=+0.0052 per set, on capital +0.0054, ann=0.106 stale_legs=0
  20261016T194340Z id=1177878 [full] buy K=500: net=+0.0052 per set, on capital +0.0054, ann=0.106 stale_legs=0
  20261016T194340Z id=1177906 [full] buy K=100: net=-0.0157 per set, on capital -0.0159, ann=-0.316 stale_legs=0
  20261016T194340Z id=1177906 [full] buy K=500: net=-0.0161 per set, on capital -0.0163, ann=-0.323 stale_legs=0
  full-group fresh events with net>0 at K=100 under this variant: 1 ['1177878']

full-group, fresh, confirmed: lenient events=7 primary events=0 (without sports: 0)
passes_ok=True
STEP-0 DECISION: INCONCLUSIVE: confirmed only under the lenient fee bound; check the fee formula first
19:48:01 rss=428MB done
```

## Звірка формули комісії (2026-10-16, документація Polymarket, недовірений текст)

Отримано `https://docs.polymarket.com/trading/fees.md` і `https://docs.polymarket.com/market-data/market-details.md` (httpx, копії в `~/.cache/tradedesk-agents/negrisk_feedocs/`). Ключові рядки:

```text
fee = C × feeRate × p × (1 - p)
Where **C** = number of shares traded and **p** = price of the shares.
| Politics | 0.04 | 0 | 25% |   | Economics | 0.05 | 0 | 25% |   | Crypto | 0.07 | 0 | 20% |   | Geopolitics | 0 | 0 | — |
Fee Tables (100 Shares), Finance / Politics / Mentions / Tech: $0.50 -> $1.00; $0.10 -> $0.36; $0.01 -> $0.04
Fees are rounded to 5 decimal places.
| `feeSchedule.exponent` | number | Exponent applied to the price component of the fee curve. |   (приклад: "rate": 0.04, "exponent": 1)
```

`--stage show` для подій-кандидатів: усі ноги `feesEnabled=True`, `feeSchedule={'exponent': 1, 'rate': 0.04, 'takerOnly': True, 'rebateRate': 0.25}` (1177878, 1172100, 1177906, politics) і `{'exponent': 1, 'rate': 0.05, ...}` (1075157, economics).

Ручна перевірка 1177878 о 19:43 (ask 0.082 / 0.114 / 0.262 / 0.509, S = 0.967): комісія за документацією = 0.04 · Σ p(1−p) = 0.04 · (0.07528 + 0.10100 + 0.19336 + 0.24992) = 0.024782; 1 − 0.967 − 0.024782 = **+0.008218** — збігається з `upper_buf0` у виводі. Таблиця документації (100 шейрів, politics, p = 0.50 → $1.00) = 100 · 0.04 · 0.25.
