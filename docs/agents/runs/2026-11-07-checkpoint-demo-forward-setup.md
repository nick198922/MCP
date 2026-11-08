**Крок:** постійна картка `IDEA-20261102-standing-improvements`, пункт 229 (розробник → критик C).

**Результат:**
• `checkpoint.demo_forward` тепер порівнює назву сетапу через `_setup` — угода `" S12 "` потрапляє в рядок S12, як у `version_stats` і `_versioned`.
• Регресійний тест `test_demo_forward_counts_a_padded_setup_name` (n=3 з шести угод); чисті назви без змін.
• `make lint` чисто, `make test`: 7027 passed, 3 skipped.

**Критик:** approve (`docs/reviews/2026-11-07-checkpoint-demo-forward-setup.md`); червоність тесту на старому коді підтверджена лише логікою (розробник бачив збій `1 == 3`).

**Далі:** `owner` — крок завершено, нових питань немає.
