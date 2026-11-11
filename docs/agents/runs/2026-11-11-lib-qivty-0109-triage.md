**Крок:** a — матеріал бібліотеки `LIB-20261109-qivty-digest` зі статусом `new` (кроки p заблоковані: відкриті пункти чекають власника, Claude на сервері або часу).

**Результат:**
• Скаут (режим «бібліотека») карток не створив: BTC-лонг після зливу — механізм уже відхилений (`liquidation-flush` ✗, `oi-confirmed-liquidation-flush` ◯); VIX/VIX3M — покрито відхиленою `fear-spike-fade-13w` і W1.
• Заклик `QG-BTC74K-1109` уже був у `calls.csv`, дубліката немає.
• Статус матеріалу → `triaged`, README бібліотеки оновлено.

**Критик:** не потрібен (карток для тріажу немає).

**Перевірка:** `make lint` ✓; `make test`: 9475 passed, 1 failed — `test_stream_sends_heartbeat_when_quiet_and_resets_backoff_once_stable` (таймінговий, 2 пінги замість 3 під навантаженням xdist; окремо `tests/test_liquidations.py` 31 passed). Зміни лише в docs, тож флейк не пов'язаний з ними, але його варто стабілізувати.

**Далі:** `owner`. Питань немає.
