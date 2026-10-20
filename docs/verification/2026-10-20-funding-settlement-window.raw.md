# Сирий вивід: funding settlement window (2026-10-20)

Скрипт `scripts/verify_funding_settlement.py`, кеш `~/.cache/tradedesk-agents/funding_settlement/`. Етапи count → fetch → build → stage0 → full.

## count

```
funding archive symbols 678; eligible 678; excluded {}
  settled |F| >= 0.10%: events 46692, > 0 6344 (14%), moments 11125, days 991, symbols 648
  settled |F| >= 0.20%: events 21121, > 0 940 (4%), moments 8178, days 919, symbols 562
  settled |F| >= 0.30%: events 13244, > 0 379 (3%), moments 6450, days 856, symbols 527
candidate symbol-days (eligible, settled |F| >= 0.10%): 16828 on 648 symbols; with previous days 24938
files to fetch (perp 1m + premium 1m): 49876
candidate days without a 1d bar on the day before (turnover gaps): 212
stage count finished in 51s, peak rss 153 MB
```

## build

```
  build 25/648 symbols, events 4622, 17s, rss 168 MB
  build 50/648 symbols, events 9720, 37s, rss 171 MB
  ...
  build 600/648 symbols, events 106688, 312s, rss 172 MB
  build 625/648 symbols, events 109568, 324s, rss 172 MB
events written 111966; skipped {'premium coverage < 50%': 203, 'missing entry/exit bar': 6, 'no 1m data on the day': 765}; peak rss 172 MB
stage build finished in 332s, peak rss 172 MB
```

## stage0

```
scaling mode source: {'default unscaled': 6, 'global unscaled': 13932, 'symbol unscaled': 23596, 'symbol scaled': 56420, 'global scaled': 18012}
eligible settlements in events.jsonl 101980; excluded with |pred| >= 0.10%: {'history < 7 days': 2212, '7d median turnover < $1M': 1827}; rss 442 MB

SIGNAL vs SETTLED (|pred| or |F| >= 0.10%, n=42821):
  corr 0.791; same sign 1.000
  median pred/F 1.000; median |pred-F| 0.0031pp; p90 0.0205pp
  cap-clipped  median |pred-F| 0.0031pp, corr 0.791
  no cap       median |pred-F| 0.0031pp, corr 0.718
  unweighted   median |pred-F| 0.0151pp, corr 0.783
  interval 1h mode scaled   n= 10094 median pred/F 0.990
  interval 1h mode unscaled n=  1637 median pred/F 1.001
  interval 2h mode scaled   n=     1 median pred/F 0.253
  interval 2h mode unscaled n=   232 median pred/F 1.000
  interval 4h mode scaled   n= 17906 median pred/F 1.001
  interval 4h mode unscaled n=  8360 median pred/F 1.002
  interval 8h mode scaled   n=  1692 median pred/F 1.001
  interval 8h mode unscaled n=  2899 median pred/F 0.999
  2x2 pred<=-0.30% x F<=-0.30%: both 11186, pred only 205, F only 271
  main-cell events with |pred - F| > 0.20pp: 404 of 11391 (bound on main-cell events missed by the prescreen)
  F distribution in main cell: median -0.523%, share F > -0.10%: 0.001

COUNTS (eligible, by predicted band):
  +>=1.00          29
  +[0.10,0.20)     4423
  +[0.20,0.30)     440
  +[0.30,1.00)     295
  ->=1.00          2018
  -[0.10,0.20)     18471
  -[0.20,0.30)     6696
  -[0.30,1.00)     9373

MAIN pred<=-0.30% (long): n=11391 days=823 symbols=477
  by year {'2024': 676, '2025': 4955, '2026': 5760}; by interval h {1.0: 3568, 2.0: 132, 4.0: 6611, 8.0: 1080}
  top symbols [('RIVERUSDT', 259), ('MYXUSDT', 216), ('LABUSDT', 196), ('HOMEUSDT', 184), ('PIPPINUSDT', 153), ('ORCAUSDT', 151), ('STEEMUSDT', 127), ('LAUSDT', 123), ('COAIUSDT', 121), ('HUSDT', 118)]
  slippage ladder (7d turnover) {'>=5M': 4720, '>=50M': 3815, '<5M': 2856}
  entry-minute quote volume: median $131,596, p10 $12,953
  PLACEBO power (price only, T+-30 min windows; no event outcomes): n=11391 SD/trade 1.308pp, SD of day mean 1.193pp, CR1 SE 0.0137pp
  MDE (2.8 x CR1 SE by day) = 0.0384pp; gate <= 0.10pp -> PASS

SECONDARY pred>=+0.30% (short): n=324 days=147 symbols=79
  by year {'2024': 9, '2025': 180, '2026': 135}; by interval h {1.0: 33, 4.0: 265, 8.0: 26}
  top symbols [('ESPORTSUSDT', 31), ('BROCCOLIF3BUSDT', 26), ('COAIUSDT', 14), ('AMBUSDT', 13), ('RVVUSDT', 13), ('YALAUSDT', 13), ('NEIROETHUSDT', 11), ('SKATEUSDT', 11), ('DAMUSDT', 10), ('A2ZUSDT', 10)]
  slippage ladder (7d turnover) {'>=50M': 67, '>=5M': 184, '<5M': 73}
  entry-minute quote volume: median $43,206, p10 $2,569
  PLACEBO power (price only, T+-30 min windows; no event outcomes): n=324 SD/trade 1.919pp, SD of day mean 1.328pp, CR1 SE 0.1130pp
  MDE (2.8 x CR1 SE by day) = 0.3164pp; gate <= 0.10pp -> FAIL

SANITY:
  BTCUSDT 1d close 2024-12-31 = 93548.9 (token-unlock / premium-spike reports: 93,548.9)
  1m entry price / previous-day 1d close (|pred| >= 0.30%): median 1.029, share outside [0.5, 2]: 0.0234
  funding intervals (h) of |pred| >= 0.30% events: {1.0: 3601, 2.0: 132, 4.0: 6876, 8.0: 1106}
  events with an inferred cap: 1774; cap values (top) [(0.02, 1767), (0.0075, 5), (0.03, 2)]
  settlement minute-of-hour (should be 0): {0: 101980}
stage stage0 finished in 16s, peak rss 445 MB
```

## full

```
scaling mode source: {'default unscaled': 6, 'global unscaled': 13932, 'symbol unscaled': 23596, 'symbol scaled': 56420, 'global scaled': 18012}
MAIN CELL: predicted funding <= -0.30% per interval, long perp, entry close [T-3,T-2), exit close [T+1,T+2); on notional
  (a) net, costs x2 (REGISTERED)                       n= 11391 days= 823 mean -0.653% CI95 [-0.684%, -0.622%] p(>0) 1.0000
  net, costs x1                                        n= 11391 days= 823 mean -0.336% CI95 [-0.366%, -0.307%] p(>0) 1.0000
  net, taker fees only (no slippage)                   n= 11391 days= 823 mean -0.120% CI95 [-0.149%, -0.091%] p(>0) 1.0000
  gross (price + funding)                              n= 11391 days= 823 mean -0.020% CI95 [-0.049%, +0.009%] p(>0) 0.9094
  price component only                                 n= 11391 days= 823 mean -0.714% CI95 [-0.748%, -0.680%] p(>0) 1.0000
  funding component only                               n= 11391 days= 823 mean +0.694% CI95 [+0.678%, +0.711%] p(>0) 0.0001
  mean cost x1 (fees + slippage)                       n= 11391 days= 823 mean +0.317% CI95 [+0.311%, +0.322%] p(>0) 0.0001
  worse price (1-min shift), costs x2                  n= 11391 days= 823 mean -1.074% CI95 [-1.111%, -1.038%] p(>0) 1.0000
  (b) gross - placebo (paired)                         n= 11391 days= 823 mean -0.079% CI95 [-0.116%, -0.043%] p(>0) 1.0000
  price - placebo (paired; settlement price effect)    n= 11391 days= 823 mean -0.773% CI95 [-0.814%, -0.733%] p(>0) 1.0000
  placebo price (T+-30 min)                            n= 11391 days= 823 mean +0.059% CI95 [+0.033%, +0.087%] p(>0) 0.0001
  (c) 2026-H1 net x2                                   n=  4302 days= 177 mean -0.673% CI95 [-0.717%, -0.628%] p(>0) 1.0000
  (d) without top-10 days: n=11280 mean -0.667%; without top-10 symbols: n=11310 mean -0.664%
  risk: net x2 p1 -4.944%, p5 -2.711%, min -22.476%, max +39.647%; MAE p1 -7.440%, MAE <= -10%: 44, <= -17.5%: 8
  win rate net x2 0.222; median net x2 -0.608%

REGISTERED CRITERIA: (a) False (b) False (c) False (d) False -> confirm False; reject rule True
VERDICT (main cell): rejected

SECONDARY CELL: predicted >= +0.30% (short)
  net, costs x2                                        n=   324 days= 147 mean -1.002% CI95 [-1.319%, -0.682%] p(>0) 1.0000
  net, costs x1                                        n=   324 days= 147 mean -0.677% CI95 [-0.995%, -0.355%] p(>0) 1.0000
  gross                                                n=   324 days= 147 mean -0.353% CI95 [-0.671%, -0.031%] p(>0) 0.9833
  price component                                      n=   324 days= 147 mean -0.903% CI95 [-1.241%, -0.559%] p(>0) 1.0000

DOSE-RESPONSE by predicted band (descriptive; [0.10,0.20) is incomplete at its lower edge)
  +>=1.00          n=    29 days=  18 funding +1.604% price -3.049% gross -1.445% cost1 +0.290% net x1 -1.735% net x2 -2.025%
  +[0.10,0.20)     n=  4423 days= 659 funding +0.129% price -0.142% gross -0.013% cost1 +0.334% net x1 -0.347% net x2 -0.680%
  +[0.20,0.30)     n=   440 days= 228 funding +0.232% price -0.240% gross -0.008% cost1 +0.347% net x1 -0.355% net x2 -0.701%
  +[0.30,1.00)     n=   295 days= 143 funding +0.446% price -0.692% gross -0.246% cost1 +0.328% net x1 -0.573% net x2 -0.901%
  ->=1.00          n=  2018 days= 538 funding +1.512% price -1.652% gross -0.139% cost1 +0.311% net x1 -0.451% net x2 -0.762%
  -[0.10,0.20)     n= 18471 days= 926 funding +0.140% price -0.116% gross +0.025% cost1 +0.312% net x1 -0.287% net x2 -0.599%
  -[0.20,0.30)     n=  6696 days= 788 funding +0.245% price -0.227% gross +0.018% cost1 +0.311% net x1 -0.294% net x2 -0.605%
  -[0.30,1.00)     n=  9373 days= 808 funding +0.518% price -0.512% gross +0.006% cost1 +0.318% net x1 -0.312% net x2 -0.630%

TIME SLICES (main cell, net x2)
  2024                                                 n=   676 days= 205 mean -0.483% CI95 [-0.564%, -0.400%] p(>0) 1.0000
  2025-H1                                              n=  1864 days= 172 mean -0.526% CI95 [-0.587%, -0.464%] p(>0) 1.0000
  2025-H2                                              n=  3091 days= 178 mean -0.676% CI95 [-0.747%, -0.611%] p(>0) 1.0000
  2026-H1                                              n=  4302 days= 177 mean -0.673% CI95 [-0.715%, -0.629%] p(>0) 1.0000
  2026-Q3                                              n=  1458 days=  91 mean -0.789% CI95 [-0.892%, -0.692%] p(>0) 1.0000

BY INTERVAL (main cell, net x2)
  1h                                                   n=  3568 days= 345 mean -0.534% CI95 [-0.603%, -0.463%] p(>0) 1.0000
  2h                                                   n=   132 days=  20 mean -0.355% CI95 [-0.639%, -0.053%] p(>0) 0.9930
  4h                                                   n=  6611 days= 724 mean -0.706% CI95 [-0.741%, -0.673%] p(>0) 1.0000
  8h                                                   n=  1080 days= 456 mean -0.762% CI95 [-0.847%, -0.677%] p(>0) 1.0000

BY SLIPPAGE TIER (main cell)
  <5M net x2                                           n=  2856 days= 450 mean -0.999% CI95 [-1.050%, -0.948%] p(>0) 1.0000
  <5M gross                                            n=  2856 days= 450 mean +0.001% CI95 [-0.050%, +0.052%] p(>0) 0.4833
  >=5M <50M net x2                                     n=  4720 days= 721 mean -0.620% CI95 [-0.660%, -0.579%] p(>0) 1.0000
  >=5M <50M gross                                      n=  4720 days= 721 mean -0.020% CI95 [-0.060%, +0.021%] p(>0) 0.8346
  >=50M net x2                                         n=  3815 days= 552 mean -0.436% CI95 [-0.492%, -0.380%] p(>0) 1.0000
  >=50M gross                                          n=  3815 days= 552 mean -0.036% CI95 [-0.092%, +0.020%] p(>0) 0.8926

NO-CAP SIGNAL (pred_nocap <= -0.30%)
  net x2                                               n= 11414 days= 826 mean -0.653% CI95 [-0.682%, -0.623%] p(>0) 1.0000

PER-ASSET (main cell, net x2)
  symbols with >= 20 trades: 171 of 477
    RIVERUSDT        n=  259 mean -0.579% CI(iid) [-0.753%, -0.404%]
    MYXUSDT          n=  216 mean -0.488% CI(iid) [-0.724%, -0.252%]
    LABUSDT          n=  196 mean -0.745% CI(iid) [-0.975%, -0.516%]
    HOMEUSDT         n=  184 mean -0.464% CI(iid) [-0.603%, -0.325%]
    PIPPINUSDT       n=  153 mean -0.696% CI(iid) [-0.900%, -0.493%]
    ORCAUSDT         n=  151 mean -0.820% CI(iid) [-0.974%, -0.666%]
    STEEMUSDT        n=  127 mean -0.390% CI(iid) [-0.576%, -0.205%]
    LAUSDT           n=  123 mean -0.433% CI(iid) [-0.636%, -0.230%]
    COAIUSDT         n=  121 mean -0.441% CI(iid) [-0.817%, -0.064%]
    HUSDT            n=  118 mean -0.411% CI(iid) [-0.812%, -0.010%]
    AERGOUSDT        n=  116 mean -0.687% CI(iid) [-0.823%, -0.552%]
    ONGUSDT          n=  114 mean -0.450% CI(iid) [-0.722%, -0.177%]
    0GUSDT           n=  112 mean -0.386% CI(iid) [-0.594%, -0.179%]
    DEXEUSDT         n=  108 mean -0.708% CI(iid) [-1.191%, -0.225%]
    GLMUSDT          n=  108 mean -0.517% CI(iid) [-0.684%, -0.350%]
    LSKUSDT          n=  107 mean -0.587% CI(iid) [-1.075%, -0.098%]
    API3USDT         n=  106 mean -0.454% CI(iid) [-0.673%, -0.235%]
    ENSOUSDT         n=  105 mean -0.499% CI(iid) [-0.733%, -0.265%]
    FUNUSDT          n=  105 mean -0.410% CI(iid) [-0.638%, -0.181%]
    MOVEUSDT         n=  103 mean -0.626% CI(iid) [-0.800%, -0.451%]
    SOONUSDT         n=   99 mean -0.732% CI(iid) [-0.982%, -0.482%]
    DRIFTUSDT        n=   95 mean -0.974% CI(iid) [-1.201%, -0.748%]
    ALPACAUSDT       n=   94 mean -0.295% CI(iid) [-0.781%, +0.192%]
    KAITOUSDT        n=   90 mean -0.867% CI(iid) [-1.040%, -0.694%]
    AIOTUSDT         n=   88 mean -0.504% CI(iid) [-0.667%, -0.342%]
  Cochran Q (cluster-robust by day): k=171 Q=455.7 df=170 p=5.782881174768286e-28 I2=0.627 deff=1.00
  permutation of symbol labels within days: Q=271.1 p=0.007992007992007992 null mean 199.1 p95 239.6
  persistence IS<2025-07-01: ranked 116, top 29: IS top -0.167% rest -0.623%; OOS top -0.663% (n=700) rest -0.734% (n=1411); diff +0.071% CI (-0.0005139160276978479, 0.0018913019310975207) p 0.127936031984008; Spearman rho 0.062 (n=53)

H2 (descriptive): receiver-side return T-15->T0 (pre) and T0->T+15 (post)
  control |pred|<0.01% pre                             n= 17476 days= 925 mean -0.014% CI95 [-0.038%, +0.013%] p(>0) 0.8561
  control |pred|<0.01% post                            n= 17476 days= 925 mean -0.104% CI95 [-0.138%, -0.072%] p(>0) 1.0000
  control |pred|<0.01% pre - post                      n= 17476 days= 925 mean +0.090% CI95 [+0.046%, +0.139%] p(>0) 0.0005
  neg >=0.30% pre                                      n= 11391 days= 823 mean +0.031% CI95 [-0.024%, +0.085%] p(>0) 0.1334
  neg >=0.30% post                                     n= 11391 days= 823 mean -0.712% CI95 [-0.776%, -0.643%] p(>0) 1.0000
  neg >=0.30% pre - post                               n= 11391 days= 823 mean +0.743% CI95 [+0.648%, +0.832%] p(>0) 0.0005
  neg [0.10,0.30) pre                                  n= 25167 days= 941 mean -0.001% CI95 [-0.024%, +0.020%] p(>0) 0.5057
  neg [0.10,0.30) post                                 n= 25167 days= 941 mean -0.183% CI95 [-0.210%, -0.156%] p(>0) 1.0000
  neg [0.10,0.30) pre - post                           n= 25167 days= 941 mean +0.182% CI95 [+0.142%, +0.218%] p(>0) 0.0005
  pos >=0.30% pre                                      n=   324 days= 147 mean +0.643% CI95 [+0.017%, +1.290%] p(>0) 0.0200
  pos >=0.30% post                                     n=   324 days= 147 mean -2.292% CI95 [-3.172%, -1.499%] p(>0) 1.0000
  pos >=0.30% pre - post                               n=   324 days= 147 mean +2.935% CI95 [+2.036%, +3.813%] p(>0) 0.0005
  pos [0.10,0.30) pre                                  n=  4863 days= 672 mean +0.012% CI95 [-0.055%, +0.083%] p(>0) 0.3568
  pos [0.10,0.30) post                                 n=  4863 days= 672 mean -0.150% CI95 [-0.269%, -0.034%] p(>0) 0.9950
  pos [0.10,0.30) pre - post                           n=  4863 days= 672 mean +0.162% CI95 [+0.034%, +0.309%] p(>0) 0.0075
stage full finished in 26s, peak rss 476 MB
```
