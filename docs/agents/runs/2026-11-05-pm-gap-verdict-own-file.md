**Крок:** p, постійна картка `standing-improvements` (пункт 93): інші пункти заблоковані (8a — дані сервера), решта карток чекає дат/вікон.

**Результат:**
• `pm_gap_paper_report.py --verdict` / `--verdict-v2g` без `--out` писали в `pm_gap_REPORT.md`, який недільний таймер переписує; тепер — власні `pm_gap_VERDICT.md` / `pm_gap_VERDICT_v2g.md` поряд.
• Вердикт Polymarket (не раніше 2027-02-23) більше не загубиться; тижневий режим і розрахунки без змін.
• Новий тест; `make lint` чистий, `make test`: 4637 passed, 6 skipped.

**Критик:** approve (mode C, [рев'ю](../reviews/2026-11-05-pm-gap-verdict-own-file.md)); дрібниці виправлено (46 passed у картці).

**Далі:** `owner`. Гілка `agent/2026-11-04-library-seller-exhaustion-card` чекає власника. Питань нема.
