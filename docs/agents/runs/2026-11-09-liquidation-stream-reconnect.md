**Крок:** p — постійна картка `IDEA-20261102-standing-improvements`, перший пункт «дірки у стрімах ліквідацій» (розробник → критик C, один раунд виправлень).

**Результат:**
• Стріми ліквідацій Bybit/OKX перепідключаються всередині вікна: backoff 1 с → 60 с з jitter, повторна підписка, heartbeat (ping після 20 с тиші), помилки підписки біржі рахуються як збій.
• `system_health` → `collector[].stream`: connects, reconnects, failures, last_failure(_ts), last_message_ts, last_row_ts.
• Старий код і так відновлювався за ~5 с, тож паузи 110–168 хв Bybit схожі на тихий ринок (підписка лише BTC/ETH/SOL). Це **не виміряно**: пункт картки лишається відкритим.
• `make lint` і `make test`: 7927 passed, 3 skipped.

**Критик:** перший раунд — зміни (лічильник збоїв не скидався при вікні 60 с); виправлено; повторне рев'ю — approve ([рев'ю](../../reviews/2026-11-09-liquidation-stream-reconnect.md)).

**Далі:** `owner`. Після деплою — порівняти паузи Bybit/OKX з Binance/Coinalyze за ті самі години, читаючи `system_health.collector[].stream` (пункт 8a). Нових питань немає.
