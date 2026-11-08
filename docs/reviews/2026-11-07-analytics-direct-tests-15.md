# Рев'ю: прямі тести analytics, коло 15 (критик C, 07.11)

**Вердикт: approve.**

- 20 тестів проходять, `make lint` чистий, `src/` не змінено, мережі в тестах немає.
- Еталони незалежні: `npdf` — аналітичні значення φ(0), φ(1), φ(2), інтеграл 1; `wma`, `rolling`, `primary_early_payout` — ручні розрахунки; `atm_strike` — дельта Black-76 проти `ncdf(σ√T/2) − 1`.
- Особливості підтверджено за кодом: `OverflowError` у `dex.num` / `option_xvenue.num` на `10**400`, `ZeroDivisionError` у `expiry_pin.distance` при spot 0, `0.0` замість `None` у `smart_money.mark_price` без `value_usd`.
- Зауваження (не блокує): `OverflowError` для `option_xvenue.num` не має власного тесту; похідна `npdf` звіряється через `ncdf` того ж модуля.
