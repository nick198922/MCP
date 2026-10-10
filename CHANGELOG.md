# Changelog

## 0.1.0 — 2026-10-10
- Ядро: `Http` (ретраї, rate-limit, кеш із single-flight, класифікація помилок), `HealthRegistry`, `Envelope/Evidence`.
- Провайдери: Deribit (публічний REST), Hyperliquid (info API), ccxt-біржі (публічні дані).
- Аналітика: Black-76 + greeks, smile, RR/Fly, max pain, GEX, basis, funding-нормалізація, ймовірності (above/below/touch), odds/de-vig, метрики стакана.
- 16 MCP-інструментів (див. README).
- Режими запуску: stdio і streamable-http.
- Тести: 51; lint: ruff.
